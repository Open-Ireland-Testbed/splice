# Splice: Technical Report

A vendor-agnostic gNMI server that exposes telemetry from NETCONF-only devices. This document is the authoritative technical reference for understanding every design decision, data flow, and implementation detail in the codebase.

---

## Table of Contents

1. [Architecture Deep Dive](#1-architecture-deep-dive)
2. [YANG Discovery Pipeline](#2-yang-discovery-pipeline)
3. [gNMI Server Implementation](#3-gnmi-server-implementation)
4. [Connection Resilience](#4-connection-resilience)
5. [Multi-Vendor Reality](#5-multi-vendor-reality)
6. [Trigger Mode](#6-trigger-mode)
7. [Performance & Scale](#7-performance--scale)
8. [Testing Strategy](#8-testing-strategy)
9. [Key Design Decisions & Trade-offs](#9-key-design-decisions--trade-offs)
10. [Comparison with Alternatives](#10-comparison-with-alternatives)

---

## 1. Architecture Deep Dive

### 1.1 Full Data Flow

The complete path from a NETCONF device to a gNMI client response:

```
NETCONF Device (port 830)
     |
     |  [SSH/NETCONF 1.1]
     v
NetconfClient (netconf/client.py)          -- ncclient wrapper, thread-safe via RLock
     |
     v
NetconfClientPool (netconf/client.py:190)  -- reconnect-once + circuit breaker
     |
     |  pool.get(filter_xml) -> RPC reply XML
     v
NetconfPoller (netconf/poller.py:14)       -- daemon thread, interval-grouped tasks
     |
     |  fetch(filter_xml) -> raw XML string
     |  parse(xml) -> List[{path, value, timestamp_ns}]
     v
parse_xml_to_kv (netconf/poller.py:145)    -- XML -> flat key-value pairs
     |
     |  apply_list_keys() for key normalization
     v
ingest() callback in cli.py:1778           -- fan-out to multiple sinks
     |
     +---> MemoryCache.ingest()            -- TTL-aware in-process cache
     +---> upsert_last_values()            -- PostgreSQL persistence
     +---> insert_history()                -- optional time-series history
     +---> archive_to_file()               -- optional file archival
     +---> KafkaPublisher.publish_poll_updates()  -- optional Kafka stream
     |
     v
LiveCache (cli.py:1995)                    -- CacheView adapter for gNMI server
     |
     |  match_prefix(path) -> records
     v
GNMIServicer (gnmi_server.py:117)          -- gRPC service on port 9339
     |
     |  gRPC response (protobuf)
     v
gNMI Client (gnmic, custom controller, etc.)
```

Each step is explicit and synchronous within its thread. There are no message queues or async frameworks -- coordination happens through thread-safe data structures.

### 1.2 Two Parallel Stacks

The project offers two usage modes that share the same core engine but diverge in state management:

**CLI + Database Stack** (`cli.py` -> service layer -> `db/`)
- State lives in PostgreSQL (10 tables, 7 migrations)
- Configuration via YAML files
- Multi-device support with per-device directories
- Full pipeline: `init` -> `setup` -> `run`
- Designed for production deployments

**Python API Stack** (`api/device.py` -> `api/_discovery.py`, `api/_catalog.py`)
- State lives in `InMemoryCatalog` dataclass (no DB, no config files)
- Programmatic usage: `Device("10.0.0.1", username="admin", password="secret")`
- Context manager support: `with Device(...) as dev: dev.get("/interfaces")`
- Designed for scripting, notebooks, integration into larger systems

**Shared code** (used by both stacks):
| Module | Purpose |
|--------|---------|
| `catalog.py` | pyang-based YANG parsing, catalog building |
| `discovery.py` | YANG library / netconf-monitoring XML parsing |
| `netconf/client.py` | `NetconfClient` + `NetconfClientPool` |
| `netconf/poller.py` | `NetconfPoller` + `parse_xml_to_kv` |
| `netconf/setter.py` | `build_config_xml` for edit-config |
| `netconf/set_executor.py` | `NetconfSetExecutor` |
| `netconf/trigger_executor.py` | `NetconfTriggerExecutor` |
| `gnmi_server.py` | gRPC servicer + `serve()` |
| `cache_store.py` | `MemoryCache` |
| `planner.py` | Query task planning and batching |
| `expander.py` | Path expansion with guardrails |
| `pathing.py` | Path normalization, key encoding |
| `alias.py` | Alias generation (strip namespace prefixes) |
| `pm.py` | PM telemetry parser |

**Where they diverge:**

| Concern | CLI Stack | API Stack |
|---------|-----------|-----------|
| Discovery storage | `db/repository.py` -> `modules` table | `InMemoryCatalog.modules` list |
| Catalog storage | `db/catalog_repository.py` -> `catalog_paths` table | `InMemoryCatalog.catalog_map` dict |
| Job intervals | `db/job_repository.py` -> `jobs` table | `InMemoryCatalog.job_intervals` dict |
| Alias map | `db/alias_repository.py` -> `path_aliases` table | `InMemoryCatalog.alias_map` dict |
| List instances | `db/list_instance_repository.py` -> `list_instances` table | Not yet supported |
| YANG file fetch | `netconf/yang_fetcher.py` + DB tracking | `api/_discovery.py` + filesystem only |
| Cache fallback | Falls back to `last_value` table when cache empty | No fallback |

### 1.3 Thread Model

During `run`, the bridge starts these threads:

| Thread | Name | Source | Purpose |
|--------|------|--------|---------|
| Main thread | `MainThread` | `cli.py` | Setup, then `server.wait_for_termination()` |
| gRPC thread pool | (8 workers) | `gnmi_server.py:378` | Handle gNMI Get/Subscribe/Set RPCs |
| Poller thread | `netconf-poller` | `netconf/poller.py:103` | Periodic NETCONF polling |
| PM poller thread | `netconf-poller` | `cli.py:1830` | Separate poller for PM data |
| List refresh thread | `list-instance-refresh` | `cli.py:1888` | Periodic list instance re-discovery |
| Health server thread | (daemon) | `health.py:31` | HTTP `/health` endpoint |
| Trigger API thread | (daemon) | `trigger_api.py:81` | HTTP `/trigger` endpoint |
| Notification listener | `netconf-notify` | `netconf/notifications.py:34` | NETCONF notification polling |

**Thread coordination:**
- `MemoryCache` uses `threading.RLock` + `threading.Condition` for thread-safe reads/writes and `wait_for_update()` (used by ON_CHANGE subscriptions)
- `NetconfClient` uses `threading.RLock` to serialize all NETCONF RPCs through a single SSH session
- `NetconfClientPool` uses `threading.Lock` for circuit breaker state
- `task_update_lock` (a `threading.Lock` in `cli.py:1847`) protects catalog/jobs/tasks during list instance refresh
- `expand_lock` (a `threading.Lock` in `cli.py:1740`) protects lazy expansion state
- The poller's `_stop` event (`threading.Event`) enables clean shutdown
- Signal handlers (`SIGINT`/`SIGTERM`) trigger orderly teardown of all threads

### 1.4 The Cache Layer

The cache is a two-tier system:

**Tier 1: `MemoryCache`** (`cache_store.py`)
```python
class MemoryCache:
    def __init__(self, ttl_sec: int = 60):
        self._store: Dict[str, dict] = {}  # path -> {path, value, timestamp_ns, _ts}
        self._version = 0                   # monotonic counter for ON_CHANGE
```

- **Ingest**: Poller calls `cache.ingest(updates)`. Each update is stored with `_ts = time.time()`. Version increments; waiting subscribers are notified via `Condition.notify_all()`.
- **Read**: `match_prefix(prefix)` iterates all entries, checking if path starts with prefix. Entries older than `ttl_sec` are lazily evicted during reads.
- **Eviction**: `evict_paths(paths)` explicitly removes entries (used by Set auto-refresh for delete operations).
- **Version tracking**: `get_version()` / `wait_for_update(last_version, timeout)` supports ON_CHANGE subscriptions.

**Tier 2: `LiveCache`** (CLI stack only, `cli.py:1995`)

The `LiveCache` class extends `CacheView` and adds:
1. **DB fallback**: If `MemoryCache.snapshot()` is empty, it reads from the `last_value` DB table
2. **Lazy expansion**: First query triggers full path expansion if in `lazy` mode
3. **PM root awareness**: When a query overlaps with PM root paths, it also includes `/pm/...` cache entries

**TTL mechanics**: The `ttl_sec` configuration (default 60s) controls how stale data can be. If the poller is slower than the TTL, entries expire and gNMI clients see gaps. In practice, the poller interval should be less than the TTL.

**Warm start**: When `cache.warm_start: true` (default), the bridge immediately fetches all tasks at startup before accepting gNMI connections, so the first client query sees data instead of empty results:

```python
# cli.py:1895
if cfg.cache.warm_start:
    for t in tasks:
        xml = fetch(t.filter_xml)
        ingest(parse(xml))
```

---

## 2. YANG Discovery Pipeline

### 2.1 Step by Step: What Happens During `setup`

The `setup` command (`cli.py`) runs an 8-step pipeline:

```
[1/8] Checking database...           -- ensure_database(url) creates DB if missing
[2/8] Checking device connectivity... -- TCP socket connect to host:port
[3/8] Running migrations...          -- apply SQL from src/splice/migrations/001-007
[4/8] Discovering YANG modules...    -- NETCONF RPCs to learn what modules exist
[5/8] Fetching YANG schemas...       -- get-schema RPC for each module -> .yang files
[6/8] Building catalog + aliases...  -- pyang parses .yang -> catalog entries in DB
[7/8] Initializing polling jobs...   -- one job per catalog path, default interval
[8/8] Generating Grafana dashboards... -- best-effort per-device dashboards from the catalog
```

### 2.2 NETCONF RPCs During Discovery

**Step 4 - Module Discovery** (`netconf/discovery_client.py` -> `discovery.py`):

Two RPCs are attempted in order of preference:

1. **RFC 8525 yang-library** (preferred):
   ```xml
   <get>
     <filter>
       <yang-library xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library"/>
     </filter>
   </get>
   ```
   Parses `<module-set>` -> `<module>` elements for name, revision, namespace, conformance type.

2. **RFC 7895 modules-state** (fallback within same response):
   ```xml
   <!-- Often in the same response as yang-library -->
   <modules-state xmlns="urn:ietf:params:xml:ns:yang:ietf-yang-library"/>
   ```

3. **RFC 6022 netconf-monitoring** (final fallback):
   ```xml
   <get>
     <filter>
       <netconf-state xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring"/>
     </filter>
   </get>
   ```
   Parses `<schemas>` -> `<schema>` elements for identifier, version, namespace, format, location.

The selection logic is in `discovery.py:148`:
```python
def select_supported_modules(xml):
    yl = parse_yang_library(xml)     # Try RFC 8525 / 7895 first
    if yl is not None:
        return yl["source"], yl["modules"]
    nm = parse_netconf_monitoring(xml)  # Fall back to RFC 6022
    if nm is not None:
        return nm["source"], nm["modules"]
    return None, []
```

**Step 5 - YANG Schema Fetch** (`runtime.py:52`, `netconf/yang_fetcher.py`):

For each discovered module, sends an RFC 6022 `<get-schema>` RPC:
```xml
<get-schema xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring">
  <identifier>openconfig-interfaces</identifier>
  <version>2023-02-06</version>
  <format>yang</format>
</get-schema>
```

The response contains the raw YANG schema text, extracted and written to `yang_dir/openconfig-interfaces@2023-02-06.yang`.

**Resilience features during fetch:**
- **Skip existing**: Files already on disk are skipped (`os.path.exists(path)`)
- **Throttle**: 0.3s delay between RPCs to avoid overwhelming the device
- **Reconnect**: On failure, waits 2s, reconnects, retries once
- **Auth failure fast-fail**: Authentication errors immediately stop fetching
- **Consecutive failure cap**: After 10 consecutive failures, stops (reconnect storm protection)

### 2.3 How YANG Modules Are Stored

**CLI stack**: Modules are stored in the `modules` DB table with columns: `id`, `device_id`, `name`, `revision`, `namespace`, `conformance`, `format`. YANG file content is stored in the `yang_files` table, linked to `modules.id`.

**API stack**: Module metadata is stored in `InMemoryCatalog.modules` (a list of dicts). YANG files exist only on the filesystem in `yang_dir`.

**Deduplication**: During fetch, `fname = f"{name}@{revision}.yang"`. If the file already exists with size > 0, it's skipped. In the DB, `upsert` semantics prevent duplicate entries.

### 2.4 Building the Catalog from YANG

The catalog is built by `catalog.py:104` using **pyang** (a Python YANG parser):

```python
def build_catalog(yang_dir, include_config=True):
    ctx = build_pyang_context(yang_dir)  # Parse all .yang files
    for mod in ctx.modules.values():
        if mod.keyword != "module":
            continue
        nodes = []
        iter_data_nodes(mod, [], nodes, root_ns)  # Walk the YANG data tree
```

`iter_data_nodes` recursively walks YANG statements, collecting:
- **container**, **list**, **leaf**, **leaf-list**, **anyxml**, **anydata** nodes
- Skips into **choice**/**case** nodes without adding them to the path
- For each node: path elements (name + namespace + keyword), config flag, data type, list keys

Each catalog entry looks like:
```python
{
    "module": "openconfig-platform",
    "root_namespace": "http://openconfig.net/yang/platform",
    "path": "/p1:components/p1:component/p1:state/p1:temperature/p1:instant",
    "path_elems": [
        {"name": "components", "namespace": "http://openconfig.net/yang/platform", "keyword": "container"},
        {"name": "component", "namespace": "http://openconfig.net/yang/platform", "keyword": "list"},
        {"name": "state", "namespace": "http://openconfig.net/yang/platform", "keyword": "container"},
        {"name": "temperature", "namespace": "http://openconfig.net/yang/platform", "keyword": "container"},
        {"name": "instant", "namespace": "http://openconfig.net/yang/platform", "keyword": "leaf"},
    ],
    "keyword": "leaf",
    "config": False,
    "data_type": "decimal64",
    "list_keys": None,       # ["name"] for list nodes
    "filter_xml": "<filter xmlns=...>...</filter>",
    "namespace_map": {"http://openconfig.net/yang/platform": "p1"},
}
```

The `filter_xml` is a pre-built NETCONF subtree filter that can be used directly with `<get>`:
```xml
<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">
  <p1:components xmlns:p1="http://openconfig.net/yang/platform">
    <p1:component>
      <p1:state>
        <p1:temperature>
          <p1:instant>
          </p1:instant>
        </p1:temperature>
      </p1:state>
    </p1:component>
  </p1:components>
</filter>
```

**Canonical paths** are always namespace-qualified: `/p1:components/p1:component/p1:state/p1:name`

**Alias paths** strip namespace prefixes: `/components/component/state/name`. Aliases are generated by `alias.py:ensure_unique_aliases()` -- when two modules have the same local-name path (collision), the aliases keep the prefix to disambiguate.

### 2.5 First-Time Discovery vs. Cached Reconnect

| Phase | First Time | Cached |
|-------|-----------|--------|
| Module discovery | ~2-5 RPCs, parses response | Reads from DB |
| YANG schema fetch | N RPCs (one per module), 0.3s throttle | Skipped (files exist on disk) |
| Catalog build | pyang parses all .yang files | Reads from DB |
| List instance discovery | M RPCs (depends on list count) | Reads from `list_instances` table |
| **Total time** | **~2 min** (Teraflex: 148 modules) | **~2-5 sec** (DB reads only) |

The `run` command always reads from the DB (it assumes `setup` was already run). With `expansion_mode: direct`, it uses cached list instances. With `expansion_mode: full`, it re-discovers list instances on every start.

---

## 3. gNMI Server Implementation

### 3.1 Supported RPCs

The `GNMIServicer` class (`gnmi_server.py:117`) implements all four gNMI RPCs:

| RPC | Status | Notes |
|-----|--------|-------|
| **Capabilities** | Full | Returns discovered models, JSON_IETF + PROTO encodings, gNMI 0.8.0 |
| **Get** | Full | Prefix + path resolution, trigger mode support |
| **Subscribe** | Full | ONCE, STREAM (SAMPLE + ON_CHANGE), heartbeats |
| **Set** | Full | merge/replace/delete, candidate/running datastores |

### 3.2 Path Matching: gNMI Path to NETCONF XPath

gNMI paths arrive as protobuf `Path` messages with `PathElem` elements. The resolution chain:

1. **Protobuf to string** (`_path_to_str`, `gnmi_server.py:61`):
   ```python
   # Input: Path(elem=[PathElem(name="components"), PathElem(name="component", key={"name": "cpu0"})])
   # Output: "/components/component[name=cpu0]"
   ```
   Key ordering is normalized using `list_keys` from the catalog.

2. **Prefix + path concatenation**: If `request.prefix` has elements, they're prepended to each path.

3. **Alias resolution**: `LiveCache.match_prefix()` checks `self._alias_map.get(prefix, prefix)` to resolve alias paths to canonical paths.

4. **Prefix matching**: All cache entries whose normalized path starts with the query prefix are returned:
   ```python
   for p, rec in data.items():
       if normalize_path(p).startswith(normalize_path(canonical_prefix)):
           out.append(rec)
   ```

5. **PM root path awareness**: If the query overlaps with any PM root path (e.g., `/p1:components/p1:component/p1:pm-current-data`), entries under the synthetic `/pm/` prefix are also included.

**Key encoding** (`pathing.py`): Key values containing special characters are encoded/decoded:
```python
def encode_key_value(value: str) -> str:
    # Escapes =, [, ], , with %HH encoding
```

### 3.3 Subscribe Modes

**ONCE** (`gnmi_server.py:313`):
```python
if mode == gnmi_pb2.SubscriptionList.ONCE:
    yield make_update_notif()  # Single snapshot
    return
```
Returns the current cache snapshot and closes the stream. This is the most common mode for gnmic.

**STREAM - SAMPLE** (`gnmi_server.py:345`):
```python
if stream_mode == SubscriptionMode.SAMPLE and now >= next_emit:
    yield make_update_notif()
    next_emit = now + (sample_interval_ns / 1e9)
```
Sends periodic snapshots at the requested `sample_interval` (default 5s). Between emissions, the thread sleeps 200ms.

**STREAM - ON_CHANGE** (`gnmi_server.py:348`):
```python
elif stream_mode == SubscriptionMode.ON_CHANGE:
    new_version = self.cache.wait_for_update(last_version, timeout=0.2)
    if new_version != last_version:
        yield make_update_notif()
        last_version = new_version
```
Uses `MemoryCache._cond` (a `threading.Condition`) to block until the cache version changes. When the poller ingests new data, it increments the version and notifies all waiters.

**Heartbeats**: Every `heartbeat_sec` (default 10s), an empty notification is sent to keep the gRPC stream alive:
```python
if now - last_hb > self.heartbeat_sec:
    yield SubscribeResponse(update=Notification(timestamp=...))
    last_hb = now
```

### 3.4 Set: gNMI to NETCONF edit-config

The Set flow is:

```
gNMI SetRequest
     |
     v
GNMIServicer.Set() -> set_handler (cli.py:2211)
     |
     v
NetconfSetExecutor.execute() (set_executor.py:121)
     |
     +-- validate_set_request()    -> List[SetMutation]
     +-- build_config_xml()        -> NETCONF <config> XML
     +-- _select_datastore()       -> "candidate" or "running"
     +-- _apply_edit_config()      -> NETCONF RPC
     |       |
     |       +-- pool.edit_config(datastore, config_xml)
     |       +-- pool.commit()       [if candidate datastore]
     |       +-- pool.discard_changes()  [on failure]
     |
     v
gnmi_pb2.SetResponse with UpdateResult per mutation
```

**Datastore selection** (`set_executor.py:66`):
- `auto` (default): Prefers `:candidate` if advertised, else `:writable-running`
- `candidate`: Lock -> edit-config -> commit -> unlock. On failure: discard-changes
- `running`: Direct edit-config (no commit needed)

**Config XML generation** (`setter.py:170`):
The critical fix is at line 240 -- the `<config>` element uses the NETCONF base namespace:
```python
lines = [f'<config xmlns="{NETCONF_BASE_NS}" {" ".join(attrs)}>']
```
Without this, ncclient/lxml serializes bare `<config>` (no namespace), which ConfD devices reject with "Missing element's namespace".

**Mutation merging**: Multiple Set operations in a single request are merged into one `<config>` subtree. Shared ancestors become a single element:
```python
# Two mutations:
#   /p1:interfaces/p1:interface[name=eth0]/p1:config/p1:mtu = 9000
#   /p1:interfaces/p1:interface[name=eth0]/p1:config/p1:enabled = true
# Become one merged <config> with shared <interface> ancestor
```

**Auto-refresh** (`set_auto_refresh: true`): After a successful Set, the bridge:
1. For `delete` mutations: evicts paths from cache + DB
2. For `merge`/`replace` mutations: reads back the path from the device and ingests the updated value

---

## 4. Connection Resilience

### 4.1 Circuit Breaker

The circuit breaker in `NetconfClientPool` (`netconf/client.py:190`) protects against session storms that could trigger device auth lockouts.

**State machine:**

```
                    success
  CLOSED ----[any RPC]----> CLOSED
     |                        ^
     | N consecutive          |
     | failures               | cooldown expires
     v                        |
   OPEN  ----[timer]----> HALF-OPEN ----[success]----> CLOSED
     |                        |
     | any call               | failure
     | during cooldown        |
     v                        v
  RuntimeError             OPEN (reset cooldown)
```

**Thresholds** (all configurable via `poller` config):

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `circuit_breaker_threshold` | 5 | Consecutive failures before OPEN |
| `circuit_breaker_cooldown_sec` | 60 | Seconds to wait before HALF-OPEN |
| `auth_failure_cooldown_sec` | 300 | Longer cooldown for auth failures |

**Implementation** (`client.py:233-272`):
```python
def _check_circuit_breaker(self):
    now = time.time()
    if now < self._cb_open_until:
        raise RuntimeError(f"Circuit breaker open -- {remaining:.0f}s more")
    if self._cb_open_until > 0 and self._consecutive_failures > 0:
        # Cooldown expired -- allow one attempt
        self._consecutive_failures = 0
        self._cb_open_until = 0.0

def _record_failure(self, exc, is_auth=False):
    self._consecutive_failures += 1
    if is_auth:
        cooldown = self._auth_cooldown  # 300s for auth
    elif self._consecutive_failures >= self._cb_threshold:
        cooldown = self._cb_cooldown    # 60s for other failures
    else:
        return  # Not enough failures yet
    self._cb_open_until = time.time() + cooldown
```

### 4.2 Reconnect-Once Semantics

Every RPC through `NetconfClientPool._call_rpc()` follows this pattern:

```python
def _call_rpc(self, op_name, fn):
    with self._lock:
        self._check_circuit_breaker()
        try:
            result = fn()              # Attempt 1
            self._record_success()
            return result
        except Exception as e:
            self.client.close(graceful=False)
            if self._is_auth_error(e):
                self._record_failure(e, is_auth=True)
                raise                   # Never retry auth failures
    # Release lock during sleep
    time.sleep(self.reconnect_delay_sec)
    with self._lock:
        self._check_circuit_breaker()
        try:
            result = fn()              # Attempt 2 (retry)
            self._record_success()
            return result
        except Exception as e2:
            self.client.close(graceful=False)
            self._record_failure(e2)
            raise                       # Propagate to caller
```

**Why not retry loops?** NETCONF devices (especially ADVA Teraflex/Quadflex) have strict session limits and will lock out accounts after failed auth attempts. A retry loop of N attempts means N SSH connections in rapid succession, which:
1. Exhausts device session limits
2. Triggers auth lockout on password failures
3. Creates a "reconnect storm" where every poller task tries to reconnect simultaneously

The reconnect-once design ensures at most 2 connection attempts per failure, and the circuit breaker prevents further attempts once the pattern is detected.

### 4.3 Auth Failure Detection

```python
@staticmethod
def _is_auth_error(exc):
    if isinstance(exc, AuthenticationError):
        return True
    msg = str(exc).lower()
    return "authentication" in msg and "failed" in msg
```

When an auth failure is detected:
1. The RPC is **not retried** (immediate raise)
2. Circuit breaker opens with `auth_failure_cooldown_sec` (300s, not the normal 60s)
3. All subsequent RPCs are refused for 5 minutes

This is critical for ADVA devices that lock out accounts after a few failed attempts. The 5-minute cooldown matches the typical device lockout window.

### 4.4 Poller Behavior When Circuit Breaker Is Open

The poller (`netconf/poller.py:72-90`) detects circuit breaker errors by parsing the error message:

```python
except Exception as e:
    msg = str(e)
    m = re.search(r"cooling down for (\d+)s more", msg)
    if m:
        cooldown = max(1, int(m.group(1)))
        pause_until = now + cooldown
        break  # Stop processing this interval group
```

When the circuit breaker is open:
1. The poller extracts the remaining cooldown seconds from the error message
2. It sets `next_run[interval] = now + cooldown` for the entire interval group
3. All tasks in that group are paused until the cooldown expires
4. The poller continues its 200ms sleep loop but skips the paused group

This means the poller doesn't generate a burst of failures when the circuit breaker reopens -- it waits for the exact cooldown period.

---

## 5. Multi-Vendor Reality

### 5.1 What Makes This Genuinely Vendor-Agnostic

The bridge assumes **only RFC-standard NETCONF capabilities**:

1. **RFC 6241**: Base NETCONF protocol (get, edit-config, lock, unlock, commit)
2. **RFC 6022**: NETCONF monitoring (get-schema for YANG retrieval)
3. **RFC 7895/8525**: YANG library (module discovery)
4. **YANG models**: Standard pyang parsing -- any valid YANG is supported

**No assumptions about**:
- Specific YANG modules (not OpenConfig-only, not vendor-specific)
- XML response structure beyond what YANG defines
- Device CLI or proprietary APIs
- Specific NETCONF extensions

### 5.2 Where Vendor Differences Bite

**1. YANG module structure varies wildly:**
- **Lumentum ROADM**: Uses OpenConfig models (`openconfig-platform`, `openconfig-terminal-device`). ~1,017 catalog paths from ~86 modules.
- **ADVA Teraflex/Quadflex**: Uses proprietary `adva-` YANG modules with deep nesting. ~20,124 catalog paths from ~330 modules.
- **Juniper**: Mix of OpenConfig and Juniper-specific modules. ~566 paths.

**2. List instance discovery is the hardest vendor-specific challenge:**

Different devices respond differently to subtree filters for list nodes. The `discover_list_instances_adaptive()` function (`netconf/list_instance_resolver.py`) tries multiple strategies:
- `build_list_instance_filter_xml()` -- filter with key leaves only
- `build_list_entry_filter_xml()` -- shallow list entry filter
- `build_xpath_filter_xml()` -- XPath filter (if device supports `:xpath`)
- `get-config` instead of `get` (some devices only expose list instances in config datastore)

**3. PM data format varies by vendor:**

The `pm.py` module uses heuristics to parse PM telemetry:
```python
def parse_pm_current_data(xml, live_token=None, path_prefix="/pm"):
    # Find elements named "pm-current-data"
    # Filter by bin-interval containing live_token
    # Emit records for montype-monval pairs
```

This works for ADVA-style PM data. For other vendors, the PM adapter mechanism (`pm_adapters/`) allows custom parsers.

**4. Namespace handling:**

Different devices may use different namespace URIs for the same module. The bridge stores the full `namespace_map` in each catalog entry and uses it to build correct NETCONF filters:
```python
# Catalog entry stores:
"namespace_map": {"http://openconfig.net/yang/platform": "p1"}

# Filter XML uses the namespace:
'<p1:components xmlns:p1="http://openconfig.net/yang/platform">'
```

**5. edit-config namespace bug:**

The `<config>` element in NETCONF edit-config must use the NETCONF base namespace. Without this, ConfD devices (ADVA Quadflex) reject with "Missing element's namespace". This was fixed in `setter.py:240`:
```python
lines = [f'<config xmlns="{NETCONF_BASE_NS}" {" ".join(attrs)}>']
```
Lumentum ROADM (writable-running) doesn't need this but doesn't break with it.

### 5.3 PM Adapters

PM (Performance Monitoring) data follows a different structure than regular telemetry. The adapter mechanism allows vendor-specific PM parsers:

```python
# config.yaml
discovery:
  pm_adapter_modules:
    - splice.pm_adapters.example  # or your custom module
```

Each adapter module must expose:
```python
def discover_pm_filters(cfg, session, dev) -> Dict[str, str]:
    # Returns mapping of root_path -> filter_xml
```

The built-in `pm.py` parser handles ADVA-style `pm-current-data` structures:
- Finds `<pm-current-data>` elements in XML
- Filters by `<bin-interval>` containing the live token (e.g., "interval-indefinite")
- Extracts `<mon-type>` / `<mon-val>` pairs
- Stores under synthetic paths: `/pm/{domain}/{name}/{mon_type}`

Example: `/pm/och/1-2-n2-otu4/pm-rx-opt` = -12.5

The domain is derived from the XML namespace (e.g., `aos-core-pm-current` -> `core-pm-current`).

### 5.4 Concrete Examples: Same Query, Different Structures

**Query**: `gnmic get --path /components/component`

**Lumentum ROADM** (OpenConfig):
```json
{
  "path": "/p1:components/p1:component[name=CHASSIS-1-1]/p1:state/p1:temperature/p1:instant",
  "value": 35.2
}
```
Paths follow OpenConfig naming. `component` is keyed by `name`. Subtree is shallow.

**ADVA Teraflex** (proprietary):
```json
{
  "path": "/p1:ains/p1:ains-list[entity=/ne/shelf[shelf-id=1]/slot[slot-id=1/2]/subslot[subslot-id=n2]/port[port-id=network]]/p1:ains-control",
  "value": "released"
}
```
Deeply nested paths with complex key values containing `/` characters (requiring URL-style encoding). The `entity` key is itself an XPath-like string.

**Juniper** (mix of OpenConfig + Juniper extensions):
```json
{
  "path": "/jnx:junos/jnx:system/jnx:host-name",
  "value": "vmx01"
}
```
Uses `jnx:` namespace prefix for Juniper-specific modules, with OpenConfig modules under `oc:` prefix.

---

## 6. Trigger Mode

### 6.1 How Trigger Differs from Polling

Regular polling (`NetconfPoller`) reads all registered paths at fixed intervals, regardless of whether anyone is asking for the data. Trigger mode performs **on-demand reads** -- a gNMI client or HTTP client requests specific paths, and the bridge fetches fresh data from the device immediately.

| Aspect | Polling | Trigger |
|--------|---------|---------|
| Initiation | Timer-based | Client request |
| Data freshness | Up to `interval_sec` old | Real-time |
| Device load | Constant (periodic RPCs) | Proportional to requests |
| Cache interaction | Writes to cache for later reads | Writes to cache + returns directly |
| Auto-onboard | N/A | Can add paths to polling schedule |

### 6.2 The gNMI Extension Mechanism (id=61001)

Trigger mode is invoked through a gNMI extension on a standard `Get` request. The extension ID is `61001` (a registered extension number in the gNMI extension space):

```python
# gnmi_server.py:16
TRIGGER_EXTENSION_ID = 61001
```

The extension payload is a JSON object:
```json
{
  "mode": "TRIGGER",
  "publish_kafka": true,      // optional: publish results to Kafka
  "auto_onboard": true,        // optional: add paths to polling schedule
  "interval_sec": 30           // optional: polling interval for onboarded paths
}
```

**Detection** (`gnmi_server.py:206-253`):
```python
def _extract_trigger_options(self, request):
    for ext in request.extension:
        if ext.registered_ext.id == TRIGGER_EXTENSION_ID:
            payload = json.loads(ext.registered_ext.msg)
            if payload["mode"] != "TRIGGER":
                raise TriggerValidationError(...)
            return {
                "publish_kafka": payload.get("publish_kafka"),
                "auto_onboard": payload.get("auto_onboard"),
                "interval_sec": payload.get("interval_sec"),
            }
    return None  # Not a trigger request
```

When detected, instead of reading from cache, the server calls `trigger_handler()` which:
1. Resolves paths to canonical catalog paths (`trigger_executor.py`)
2. Builds NETCONF filter XML for each path
3. Sends `<get>` or `<get-config>` RPC
4. Parses response XML into key-value updates
5. Ingests results into cache (so subsequent Get/Subscribe see fresh data)
6. Returns results directly in the GetResponse

### 6.3 HTTP Trigger API

The trigger API (`trigger_api.py`) provides an HTTP alternative to gNMI extensions:

**Endpoint**: `POST /trigger`

**Authentication**: Bearer token (configured via `trigger_api.bearer_token`)

**Request**:
```json
{
  "paths": ["/components/component[name=CHASSIS-1-1]/state/temperature"],
  "publish_kafka": true,
  "auto_onboard": true,
  "interval_sec": 30
}
```

**Response**:
```json
{
  "request_id": "uuid",
  "device": "teraflex",
  "path_count": 1,
  "updates": [{"path": "...", "value": 35.2, "timestamp_ns": ...}],
  "auto_onboarded_paths": ["/p1:components/p1:component[name=CHASSIS-1-1]/p1:state/p1:temperature"],
  "kafka_published_count": 1,
  "errors": []
}
```

The HTTP server (`http.server.HTTPServer`) runs in a daemon thread. It validates the bearer token, parses the JSON body, calls the same `trigger_handler` as the gNMI extension, and returns the result as JSON.

### 6.4 Auto-Onboard

When `auto_onboard: true` (or not specified, as it defaults to `true` in `TriggerConfig`):

1. After the trigger read succeeds, all requested paths are added to the `jobs` DB table with the specified `interval_sec`
2. The runtime poller tasks are refreshed from the DB: `_refresh_runtime_tasks_from_db_jobs()`
3. From that point forward, the paths are polled continuously

This means a gNMI client can request a path it hasn't seen before, and that path automatically becomes part of the polling schedule. The bridge discovers what the client needs and adapts.

```python
# cli.py:2098
if do_auto_onboard and result.requested_paths:
    onboard_jobs = {p: interval for p in result.requested_paths}
    with SessionFactory() as s:
        upsert_jobs(s, dev.id, onboard_jobs)
    _refresh_runtime_tasks_from_db_jobs()
```

---

## 7. Performance & Scale

### 7.1 Numbers from Real Devices

| Device | Vendor | Catalog Paths | List Nodes | YANG Modules | Setup Time |
|--------|--------|--------------|------------|--------------|------------|
| ROADM (192.0.2.15) | Lumentum | ~1,017 | ~15 | ~86 | ~2 min |
| Teraflex (192.0.2.92) | ADVA | ~20,124 | ~200+ | ~330 | ~30 min (full discovery) |
| Juniper vMX | Juniper | ~566 | ~20 | ~60 | ~5 min |

The Teraflex is by far the most demanding due to its deeply nested YANG models and large number of list instances. Full list instance discovery on Teraflex can take 30+ minutes due to device session limits and throttling.

### 7.2 Poller Throttling

**Why it exists**: Without throttling, the poller sends NETCONF RPCs as fast as possible. For a device with 200 polling tasks, this means 200 RPCs with no delay between them. ADVA devices can't handle this -- they drop sessions or return errors.

**How `task_throttle_ms` works** (`poller.py:65`):
```python
for i, task in enumerate(task_list):
    if i > 0 and self._task_throttle_sec > 0:
        time.sleep(self._task_throttle_sec)  # 50ms default
    xml = self.fetch(task.filter_xml)
    updates = self.parse(xml)
    self.ingest(updates)
```

Between each task in an interval group, the poller sleeps for `task_throttle_ms / 1000` seconds (default 50ms). This limits the RPC rate to ~20 RPCs/second.

**Inter-interval sleep**: The poller's main loop sleeps 200ms between iterations (`self._stop.wait(0.2)`). This prevents busy-waiting but means the minimum poll response time is ~200ms.

### 7.3 Batch Mode

Batch mode coalesces multiple catalog paths into fewer NETCONF RPCs:

**Without batching** (`plan_queries`):
- 581 catalog paths = 581 NETCONF RPCs per interval
- Each RPC fetches one leaf/container

**With "top" batching** (`plan_batched_queries`, `planner.py:58`):
- Groups paths by top-level container
- All paths under `/p1:components/...` become one RPC for `/p1:components`
- 581 paths might collapse to ~10 RPCs

```python
def plan_batched_queries(catalog, job_intervals, default_interval_sec=30):
    batch = {}
    for path, info in catalog.items():
        top = _top_level_path(path)  # "/p1:components"
        target = top if top in catalog else path
        key = (target, interval)
        if key not in batch:
            batch[key] = QueryTask(canonical_path=target, filter_xml=..., interval_sec=interval)
    return list(batch.values())
```

**With "module" batching** (`plan_module_batched_queries`, `planner.py:76`):
- Groups by module + top-level container
- Avoids cross-module batching (some devices handle this poorly)

### 7.4 Actual Polling Overhead

For a typical setup (ROADM, 30s interval, top batching):
- ~10 NETCONF RPCs per cycle
- 50ms throttle between RPCs = ~0.5s per cycle
- One cycle every 30s = ~1.7% of the time spent polling
- Each RPC returns ~5-50 leaf values

For Teraflex (13k paths, direct mode with specific jobs):
- The number of RPCs depends on how many jobs are configured
- With selective jobs (e.g., only temperature sensors), might be 5-10 RPCs
- Full batched polling would be ~50 RPCs per cycle

---

## 8. Testing Strategy

### 8.1 Test Overview

**200 tests** across 42 test files. The split:

| Category | Count | Files |
|----------|-------|-------|
| Unit (no DB, no network) | ~170 | Most `test_*.py` files |
| PostgreSQL integration | ~14 | Files ending in `_pg.py` |
| CLI smoke/migration | ~4 | `test_cli_smoke_pg.py`, `test_cli_migration_hints.py` |
| Doctor/migrator | ~10 | `test_doctor.py`, `test_migrator.py`, `test_schema_sql.py` |

### 8.2 How NETCONF Interactions Are Mocked

Tests use direct function-level mocking -- no NETCONF server is started:

**Pattern 1: Lambda fetch functions**
```python
# test_poller.py
def test_poller_basic():
    fetch = lambda fx: '<data><components><component><name>cpu</name></component></components></data>'
    parse = lambda xml: parse_xml_to_kv(xml)
    ingest = Mock()
    poller = NetconfPoller(fetch, parse, ingest, tasks=[...])
```

**Pattern 2: Mock managers**
```python
# test_discovery_client.py
mock_mgr = Mock()
mock_mgr.get.return_value = Mock(data_xml='<rpc-reply>...</rpc-reply>')
```

**Pattern 3: XML string inputs**
```python
# test_catalog.py -- uses actual YANG files on disk
# test_discovery.py -- uses XML string literals for yang-library/monitoring responses
```

**Pattern 4: Set executor testing**
```python
# test_set_executor.py
pool = Mock()
pool.get_server_capabilities.return_value = [":candidate:1.0", ":writable-running:1.0"]
pool.edit_config.return_value = None
pool.commit.return_value = None
executor = NetconfSetExecutor(pool=pool, catalog_map=..., ...)
```

### 8.3 What `_pg` Tests Validate

The 16 `_pg` tests require `TEST_DATABASE_URL` and validate:

| Test File | What It Tests |
|-----------|--------------|
| `test_repository_pg.py` | Module CRUD in `modules` table |
| `test_catalog_repository_pg.py` | Catalog path upsert/query |
| `test_alias_query_pg.py` | Alias map fetch |
| `test_alias_repository_pg.py` | Alias upsert |
| `test_job_repository_pg.py` | Job interval CRUD |
| `test_last_value_repository_pg.py` | Last value upsert/query/delete |
| `test_history_repository_pg.py` | Telemetry history insert/query |
| `test_model_query_pg.py` | Model data query for Capabilities |
| `test_models_pg.py` | SQLAlchemy model relationships |
| `test_yang_fetcher_store_pg.py` | YANG file storage in DB |
| `test_cli_smoke_pg.py` | CLI command smoke tests against real DB |

These tests create real tables (via migrations), insert data, and verify queries. They use transaction rollback for cleanup.

### 8.4 Key Test Areas

- **`test_client_resilience.py`**: Circuit breaker state transitions, reconnect-once, auth failure handling
- **`test_gnmi_server.py`**: Get, Subscribe (ONCE, STREAM), Set, Capabilities, trigger extensions
- **`test_setter.py`**: Config XML generation, namespace handling, predicate parsing
- **`test_set_request.py`**: gNMI SetRequest validation, path resolution
- **`test_set_executor.py`**: Datastore selection, lock/unlock, commit/discard
- **`test_trigger_executor.py`**: Path resolution, alias handling, execution
- **`test_trigger_api.py`**: HTTP endpoint, bearer auth, JSON parsing
- **`test_kafka_publisher.py`**: Topic naming, payload format, error handling
- **`test_poller.py`**: Interval grouping, throttling, circuit breaker pause
- **`test_fetch_yang_resilience.py`**: Auth failure fast-fail, consecutive failure cap
- **`test_migrator.py`**: Migration ordering, lock detection
- **`test_doctor.py`**: Pre-flight check validation
- **`test_pathing.py`** + **`test_pathing_keys_end_to_end.py`**: Path normalization, key encoding
- **`test_storage.py`** + **`test_storage_concurrency.py`**: File archival, concurrent writes

---

## 9. Key Design Decisions & Trade-offs

### 9.1 Why Polling Instead of NETCONF Notifications?

NETCONF notifications (RFC 5277) are supported as an optional feature (`notifications.enabled: true`), but polling is the primary data collection mechanism. Here's why:

1. **Notification support is inconsistent**: Many devices don't implement RFC 5277 or only support it for specific events (e.g., alarms), not arbitrary telemetry paths.

2. **No standard telemetry notification**: There's no widely-adopted NETCONF notification for "state changed at path X". OpenConfig's gNMI defines this, but that's what we're implementing -- the devices don't have it.

3. **Predictable load**: Polling at fixed intervals creates a predictable, controllable load on the device. Notifications can burst unpredictably.

4. **Completeness guarantee**: Polling fetches the full current state. Notifications only report changes, so you need an initial full read anyway, and you can miss notifications during reconnects.

The `NotificationListener` (`netconf/notifications.py`) is available as a supplement -- it ingests NETCONF notifications into the same cache, so both mechanisms can contribute data.

### 9.2 Why PostgreSQL for CLI Stack but In-Memory for API?

**CLI stack uses PostgreSQL because:**
- Multi-device deployments need shared state
- Setup (discovery) and runtime (polling) are separate processes
- Catalog and list instances survive restarts
- Last-value persistence means gNMI clients get data immediately on startup
- Migration system enables schema evolution

**API stack uses in-memory because:**
- Programmatic usage shouldn't require DB infrastructure
- Single-device, single-process use case
- All state can be reconstructed from YANG files on disk + device
- Simpler dependency chain for library consumers

The trade-off is clear: CLI users get persistence and multi-device support; API users get zero infrastructure requirements.

### 9.3 Why One Bridge Instance Per Device?

Each NETCONF device gets its own bridge process with its own gNMI port:

```
Device A (192.0.2.92) -> splice process -> port 9339
Device B (192.0.2.120) -> splice process -> port 9340
Device C (192.0.2.15) -> splice process -> port 9341
```

**Reasons:**

1. **Isolation**: A misbehaving device (slow responses, auth lockout) affects only its own bridge, not other devices.

2. **Simplicity**: Each process has one NETCONF connection, one poller, one cache. No need for multiplexing logic.

3. **gNMI semantics**: In gNMI, a target represents one device. A multi-device server would need to demux by target in the path prefix, which isn't standard gNMI behavior.

4. **Scaling**: Each bridge process can be independently restarted, upgraded, or moved to a different host.

5. **Resource control**: Per-device throttling and circuit breaker settings can be tuned independently.

The DB is shared via `device_id` foreign keys, so a single PostgreSQL instance serves all bridges.

### 9.4 Why gNMI Specifically?

**vs. REST API**: gNMI provides a standard, structured way to query network device state. REST APIs require custom endpoint design and don't have standardized path semantics. gNMI's path model maps directly to YANG paths.

**vs. MQTT**: MQTT is publish-subscribe with no standard schema. gNMI provides typed data (TypedValue), path-based queries, and the Subscribe RPC for streaming. MQTT would require inventing all of this.

**vs. Direct NETCONF proxy**: A NETCONF-to-NETCONF proxy doesn't solve the problem -- the goal is to expose gNMI to controllers that speak gNMI (like OpenConfig-based SDN controllers). The bridge translates protocols.

**Why gNMI wins for this use case**: OpenConfig/gNMI is the emerging standard for network telemetry. SDN controllers (ONOS, ODL, custom controllers in the Open Ireland testbed) increasingly expect gNMI. This bridge lets legacy NETCONF devices participate in a gNMI-based ecosystem without firmware upgrades.

---

## 10. Comparison with Alternatives

### 10.1 OpenConfig gNMI Targets

OpenConfig provides reference gNMI target implementations (e.g., `gnmi_target` in the `openconfig/gnmi` repo).

| Aspect | OpenConfig Reference | This Bridge |
|--------|---------------------|-------------|
| Purpose | gNMI server for devices that support gNMI natively | gNMI server for devices that only support NETCONF |
| Data source | Local device state (in-process) | Remote NETCONF device (over SSH) |
| YANG discovery | Assumes models are known | Discovers models from device |
| Vendor support | Device must implement gNMI | Any RFC-compliant NETCONF device |
| PM data | Not applicable | Specialized PM parser + adapter mechanism |

### 10.2 Vendor-Provided gNMI

Some vendors (Cisco, Arista, Juniper) ship gNMI support in their NOS.

| Aspect | Vendor gNMI | This Bridge |
|--------|-------------|-------------|
| Device support | Only that vendor's latest firmware | Any NETCONF device, any vendor |
| Update required | Firmware upgrade | No device changes needed |
| Feature parity | Full native gNMI | Subset (polling-based Subscribe, no streaming telemetry) |
| PM data | Vendor-specific | Normalized across vendors |
| Operational cost | Per-device enablement | One bridge per device |

This bridge is most valuable when: (a) the device doesn't support gNMI at all, (b) firmware upgrades aren't possible, or (c) you need vendor-agnostic PM normalization.

### 10.3 Related Open-Source Projects

**ygot** (OpenConfig YANG Go Tools):
- Go library for YANG-based data structures
- Generates Go structs from YANG models
- This bridge uses pyang (Python) instead, and doesn't generate code -- it builds a dynamic catalog at runtime

**gNMIc as a collector**:
- gNMIc can collect from gNMI-speaking devices
- This bridge makes NETCONF devices speak gNMI, so gNMIc can then collect from them
- They're complementary: NETCONF device -> this bridge -> gNMIc -> Prometheus/Kafka/etc.

**Telegraf NETCONF input plugin**:
- Telegraf can read from NETCONF devices directly
- Produces metrics, not gNMI
- No YANG-based discovery -- requires manual path configuration
- This bridge provides: automatic YANG discovery, gNMI protocol support, PM normalization, trigger mode

**NETCONF-to-gNMI translators** (various research projects):
- Often proof-of-concept or thesis projects
- Typically handle only OpenConfig models
- This bridge handles arbitrary vendor YANG, has production resilience (circuit breaker, auth protection), and supports the full gNMI service (Get, Subscribe, Set, Capabilities)

### Summary of Differentiation

This bridge occupies a unique niche: it's the **only open-source tool that provides full gNMI service (including Set and triggered reads) for arbitrary NETCONF devices with automatic YANG discovery and vendor-agnostic PM normalization**. The closest alternatives either require gNMI support on the device, only handle specific vendors, or only provide collection (not a gNMI server).

---

## Appendix A: Configuration Reference

All configuration is in YAML. Key sections from `config.py`:

```yaml
device:
  host: "192.0.2.92"
  port: 830
  username: "admin"
  password_file: "./password"  # or NETCONF_PASSWORD env var

db:
  url: "postgresql+psycopg://postgres:postgres@localhost:5432/splice"

yang_dir: "./yang"

gnmi:
  bind: "0.0.0.0"
  port: 9339
  heartbeat_sec: 10
  expansion_mode: "direct"     # direct|full|lazy
  max_expanded_paths_per_base: 10000
  max_total_expanded_paths: 100000
  set_enabled: false
  set_datastore: "auto"        # auto|running|candidate
  set_auto_refresh: false
  set_use_lock: false

poller:
  default_interval_sec: 30
  batch_enabled: true
  batch_mode: "top"            # top|module
  preserve_namespaces: false
  task_throttle_ms: 50
  list_refresh_sec: 0          # 0 = disabled
  reconnect_delay_sec: 2.0
  circuit_breaker_threshold: 5
  circuit_breaker_cooldown_sec: 60.0
  auth_failure_cooldown_sec: 300.0

trigger:
  enabled: false
  auto_onboard: true
  default_interval_sec: 30
  max_paths_per_request: 50

trigger_api:
  enabled: false
  bind: "0.0.0.0"
  port: 9812
  bearer_token: "your-secret-token"

kafka:
  enabled: false
  brokers: "kafka:9092"
  topic_prefix: "splice"
  publish_poll_updates: true
  publish_trigger_events: true

pm:
  enabled: false
  interval_sec: 30
  live_token: "interval-indefinite"
  path_prefix: "/pm"

cache:
  ttl_sec: 60
  warm_start: true

health:
  enabled: true
  bind: "0.0.0.0"
  port: 9809
```

## Appendix B: Database Schema

10 tables, 7 migrations (`src/splice/migrations/001-007`):

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `devices` | Device registry | `id`, `host`, `port`, `username`, `name` |
| `modules` | Discovered YANG modules | `id`, `device_id`, `name`, `revision`, `namespace` |
| `yang_files` | YANG schema content | `module_id`, `content` |
| `catalog_paths` | Full path catalog | `device_id`, `canonical_path`, `module`, `config`, `data_type`, `list_keys`, `filter_xml`, `namespace_map` |
| `path_aliases` | Alias -> canonical mapping | `device_id`, `alias_path`, `canonical_path` |
| `jobs` | Polling job schedule | `device_id`, `canonical_path`, `interval_sec` |
| `list_instances` | Resolved list key values | `device_id`, `list_path`, `key_values` |
| `pm_filters` | PM data filter definitions | `device_id`, `root_path`, `filter_xml`, `list_path`, `key_values` |
| `last_value` | Most recent value per path | `device_id`, `path`, `value`, `timestamp_ns` |
| `telemetry_history` | Time-series history | `device_id`, `path`, `value`, `timestamp_ns` |

## Appendix C: Source File Map

```
src/splice/
  cli.py                      -- All CLI commands, main entry point (2400+ lines)
  config.py                   -- Dataclass configuration model
  gnmi_server.py              -- gRPC gNMI servicer
  gnmi_pb2.py / gnmi_pb2_grpc.py  -- Generated protobuf code
  cache_store.py              -- MemoryCache with TTL and versioning
  catalog.py                  -- pyang YANG parsing, catalog building
  discovery.py                -- YANG library / monitoring XML parsing
  planner.py                  -- Query task planning and batching
  expander.py                 -- Path expansion with guardrails
  pathing.py                  -- Path normalization, key encoding
  alias.py                    -- Alias generation
  pm.py                       -- PM telemetry parser
  jobs.py                     -- Job rule application
  runtime.py                  -- Orchestration: discovery -> fetch -> catalog -> jobs
  setup.py                    -- Setup report generation
  doctor.py                   -- Pre-flight validation checks
  exporter.py                 -- JSON/CSV export
  storage.py                  -- File archival
  health.py                   -- HTTP health check server
  trigger_api.py              -- HTTP trigger API server
  kafka_publisher.py          -- Kafka event publishing

  netconf/
    client.py                 -- NetconfClient + NetconfClientPool (circuit breaker)
    poller.py                 -- NetconfPoller + parse_xml_to_kv
    setter.py                 -- NETCONF edit-config XML builder
    set_executor.py           -- NetconfSetExecutor (datastore selection, lock/commit)
    set_request.py            -- gNMI SetRequest validation
    trigger_executor.py       -- NetconfTriggerExecutor (on-demand reads)
    list_instance_resolver.py -- Adaptive list instance discovery
    yang_fetcher.py           -- get-schema RPC + YANG file storage
    discovery_client.py       -- Module discovery RPC wrappers
    notifications.py          -- NETCONF notification listener
    debug.py                  -- Discovery debug output writer

  api/
    device.py                 -- Device class (Python API entry point)
    _discovery.py             -- API-stack discovery functions
    _catalog.py               -- InMemoryCatalog dataclass
    _errors.py                -- API-specific exception types
    _subscription.py          -- Subscription iterator for API stack

  db/
    models.py                 -- SQLAlchemy ORM models
    session.py                -- Engine + SessionFactory
    migrator.py               -- SQL migration runner with advisory locks
    device_repository.py      -- Device CRUD
    repository.py             -- Module CRUD
    catalog_repository.py     -- Catalog path upsert
    catalog_query.py          -- Catalog read queries
    alias_repository.py       -- Alias upsert
    alias_query.py            -- Alias read queries
    job_repository.py         -- Job CRUD
    list_instance_repository.py -- List instance CRUD
    pm_filter_repository.py   -- PM filter CRUD
    last_value_repository.py  -- Last value CRUD
    history_repository.py     -- History insert/query
    model_query.py            -- Model data for gNMI Capabilities

  pm_adapters/
    example.py                -- Template PM adapter
```
