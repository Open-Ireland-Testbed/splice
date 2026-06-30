# Python API Guide

The Python API lets you connect to NETCONF devices, read/write data, subscribe to updates, and optionally serve gRPC -- all programmatically, **without PostgreSQL or YAML config files**.

## Installation

```bash
python -m venv .venv
.venv/bin/pip install -e .

# Optional extras
.venv/bin/pip install -e .[kafka]    # Kafka publishing
.venv/bin/pip install -e .[search]   # Semantic path search (sentence-transformers)
```

## Quick start

```python
from splice.api import Device

device = Device("10.0.0.1", username="admin", password="secret")
device.connect()

data = device.get("/interfaces/interface")
print(data)

device.close()
```

`connect()` runs the full pipeline: SSH connect, discover YANG modules, fetch schemas, and build the telemetry catalog. This is the simplest way to get started but can take 30-120 seconds on devices with many YANG modules.

---

## The connect pipeline

When you call `connect()`, six steps happen under the hood:

| Step | Method | What it does | Time cost |
|------|--------|-------------|-----------|
| 1 | `connect_netconf()` | Establish SSH/NETCONF session | ~1-3s |
| 2 | `discover()` | Query the device for its YANG module list (RFC 8525/7895/6022) | ~1-5s |
| 3 | `fetch_yang()` | Fetch each YANG schema via `get-schema` RPC and write to disk | ~30-120s |
| 4 | `build_catalog()` | Parse YANG files with pyang, build path catalog + aliases | ~1-5s |
| 5 | _(internal)_ | Initialize default polling jobs | instant |
| 6 | _(internal)_ | Create Kafka publisher (if configured) | instant |

Step 3 (fetch YANG) is by far the slowest because it makes one NETCONF RPC per module. On a device with 100+ modules, this can take minutes.

### Skipping expensive steps

If you already have YANG files on disk (from a previous run or a vendor distribution), you can skip the expensive discovery and fetch steps:

```python
device = Device("10.0.0.1", username="admin", password="secret",
                yang_dir="/path/to/cached/yang")

# Skip module discovery and YANG fetching -- just reuse files on disk
device.connect(skip_discover=True, skip_fetch_yang=True)

# Ready in ~2 seconds instead of ~2 minutes
data = device.get("/interfaces/interface")
```

### Skip parameters for `connect()`

| Parameter | Default | Effect |
|-----------|---------|--------|
| `skip_discover` | `False` | Skip YANG module discovery (step 2) |
| `skip_fetch_yang` | `False` | Skip YANG schema fetch (step 3). Requires `yang_dir` to contain `.yang` files |
| `skip_catalog` | `False` | Skip catalog build (step 4). You must call `build_catalog()` manually later |

Common patterns:

```python
# Full pipeline (default)
device.connect()

# Reuse cached YANG directory (fastest)
device.connect(skip_discover=True, skip_fetch_yang=True)

# Skip just the slow fetch (if YANG files already on disk)
device.connect(skip_fetch_yang=True)

# Connect and discover, but build catalog later
device.connect(skip_catalog=True)
device.build_catalog()  # when you're ready
```

---

## Step-by-step connect

For maximum control, call each pipeline step individually:

```python
from splice.api import Device

device = Device("10.0.0.1", username="admin", password="secret",
                yang_dir="./yang_cache")

# Step 1: SSH connect only
device.connect_netconf()
print("Connected to NETCONF")

# Step 2: Discover YANG modules
modules = device.discover()
print(f"Found {len(modules)} modules")

# Step 3: Fetch YANG schemas (skips files that already exist)
count = device.fetch_yang(throttle_sec=0.5)
print(f"Fetched/cached {count} YANG files")

# Step 4: Build catalog from YANG files on disk
device.build_catalog()
print(f"Catalog: {len(device.catalog_paths)} paths")

# Now ready for get/set/subscribe
data = device.get("/system/hostname")
```

### Offline catalog build (no NETCONF at all)

`build_catalog()` only needs YANG files on disk. If you have them from another source (vendor SDK, previous discovery, git), you can build the catalog without any network connection:

```python
device = Device("10.0.0.1", yang_dir="/path/to/yang/files")
device.build_catalog()

# The catalog is ready for inspection
print(device.catalog_paths)
print(device.find_paths("*interface*"))
```

Note: `get()`, `set()`, and `subscribe()` still need a NETCONF connection. But catalog inspection works offline.

---

## Constructor reference

```python
Device(
    host,                             # NETCONF host (required)
    port=830,                         # NETCONF port
    username="admin",                 # NETCONF username
    password="",                      # NETCONF password
    yang_dir=None,                    # Directory for YANG files (temp dir if None)
    cache_ttl_sec=120,                # In-memory cache TTL (seconds)
    default_poll_interval_sec=30,     # Default polling interval for subscriptions
    include_config=True,              # Include config=true paths in catalog
    reconnect_delay_sec=2.0,          # Delay before reconnect after failure
    circuit_breaker_threshold=5,      # Failures before circuit opens
    circuit_breaker_cooldown_sec=60,  # Cooldown after circuit opens
    kafka_brokers=None,               # Kafka bootstrap servers (optional)
    kafka_topic_prefix="splice", # Kafka topic prefix
    batch_polling=True,               # Batch queries by top-level container
)
```

### Key parameters

**`yang_dir`** -- If provided, YANG files are stored here and reused across sessions. If `None`, a temporary directory is created and cleaned up on `close()`. Providing a persistent directory is the key to fast reconnects.

**`include_config`** -- When `True` (default), the catalog includes both operational and configuration paths. Set to `False` if you only need read-only telemetry.

**`batch_polling`** -- When `True` (default), subscription polling coalesces queries by top-level container to reduce the number of NETCONF RPCs. Set to `False` for per-path polling.

---

## Reading data: `get()`

```python
data = device.get("/interfaces/interface")
```

Returns a list of update dicts:

```python
[
    {
        "path": "/interfaces/interface/name",
        "value": "eth0",
        "timestamp_ns": 1707500000000000000,
        "meta": {"raw_path": "...", "ns_path": "...", "namespace_map": {...}}
    },
    ...
]
```

### Path formats

`get()` accepts three path formats:

| Format | Example | Description |
|--------|---------|-------------|
| Canonical | `/p1:interfaces/p1:interface` | YANG module-qualified |
| Alias | `/interfaces/interface` | Human-friendly, auto-generated |
| Keyed | `/interfaces/interface[name=eth0]` | With list instance predicates |

Results are automatically ingested into the device's shared cache.

---

## Writing data: `set()`

```python
result = device.set("/system/hostname", "new-name")
# result: {"path": "/system/hostname", "operation": "merge", "timestamp_ns": ...}
```

### Parameters

```python
device.set(
    path,                  # Target path (canonical, alias, or keyed)
    value,                 # Value to set (str, int, float, bool, bytes)
    operation="merge",     # "merge" | "replace" | "delete"
    datastore="auto",      # "auto" | "running" | "candidate"
    use_lock=False,        # Lock the datastore during edit
)
```

### Datastore selection

| Value | Behaviour |
|-------|-----------|
| `auto` | Uses `candidate` if available, falls back to `running` |
| `candidate` | Requires `:candidate` capability. Edits are committed atomically |
| `running` | Requires `:writable-running` capability. Changes apply immediately |

### Error handling

```python
from splice.api import PathNotWritableError, SetError

try:
    device.set("/system/uptime", 999)      # config=false path
except PathNotWritableError:
    print("Cannot write to operational data")

try:
    device.set("/system/hostname", "x")
except SetError as e:
    print(f"edit-config failed: {e}")
```

---

## Subscribing to updates: `subscribe()`

```python
sub = device.subscribe("/interfaces/interface", interval=10)
for update_batch in sub:
    for u in update_batch:
        print(f"{u['path']} = {u['value']}")
    break  # or continue for continuous polling

sub.cancel()
```

Each `Subscription` runs a dedicated background poller thread that periodically fetches data via NETCONF and delivers update batches through the iterator.

### Parameters

```python
device.subscribe(
    *paths,          # One or more paths (prefix-matched against catalog)
    interval=30,     # Polling interval in seconds
    batch=True,      # Coalesce queries by top-level container
)
```

### Multiple paths

```python
sub = device.subscribe(
    "/interfaces/interface",
    "/system/state",
    interval=5,
)
```

### Context manager

```python
with device.subscribe("/interfaces/interface", interval=5) as sub:
    for batch in sub:
        process(batch)
        if done:
            break
# Subscription is automatically cancelled on exit
```

### How it works internally

1. The requested paths are matched against the catalog (prefix match)
2. A query plan is built (batched by top-level container if `batch=True`)
3. A dedicated `NetconfPoller` thread starts, issuing NETCONF GETs on the interval
4. Parsed results are ingested into both the subscription's local cache and the device's shared cache
5. `__next__()` blocks on a `threading.Condition` until new data arrives (no busy-wait)
6. `cancel()` stops the poller thread

---

## Serving gRPC: `serve()`

Expose the device's cached data as a standard gNMI server that external clients (gnmic, Telegraf, etc.) can connect to:

```python
device.serve(port=9339, blocking=False)

# External clients can now connect:
# gnmic -a 127.0.0.1:9339 --insecure get --path /
```

### Parameters

```python
device.serve(
    port=9339,          # gRPC port
    bind="0.0.0.0",     # Bind address
    heartbeat_sec=10,   # Heartbeat interval for Subscribe streams
    set_enabled=True,   # Enable gNMI Set (write) operations
    blocking=False,     # Block until server stops (True for standalone)
)
```

The gRPC server supports:
- **Capabilities** -- returns the list of discovered YANG models
- **Get** -- returns cached data matching the requested paths
- **Get with trigger extension** -- performs a fresh NETCONF read
- **Subscribe** -- SAMPLE, ON_CHANGE, and ONCE modes
- **Set** -- maps gNMI Set to NETCONF edit-config (when `set_enabled=True`)

---

## Informational methods

### `catalog_paths`

```python
all_paths = device.catalog_paths
# ["/p1:system/p1:hostname", "/p1:system/p1:uptime", ...]
```

### `find_paths(pattern)`

Glob-style pattern matching against catalog paths:

```python
device.find_paths("*interface*")
device.find_paths("*/hostname")
device.find_paths("*config*")
```

### Path index (tree browser + semantic search)

For deeper catalog exploration beyond glob matching, the `path_index` module provides a hierarchical tree view and optional embedding-based semantic search:

```python
from splice.path_index import (
    build_path_entries, build_tree, subtree, walk_tree, print_tree,
    PathIndex, is_search_available,
)

device = Device("10.0.0.1", yang_dir="./yang_cache")
device.build_catalog()

# Build entries from InMemoryCatalog
entries = build_path_entries(device.catalog)

# Tree navigation
tree = build_tree(entries)
print_tree(tree, max_depth=2)

# Drill into a subtree (alias-style paths work too)
node = subtree(tree, "/interfaces")
for depth, child in walk_tree(node):
    print("  " * depth + child.segment)

# Semantic search (requires [search] extra)
if is_search_available():
    idx = PathIndex(entries)
    idx.build()
    for entry, score in idx.search("amplifier input power"):
        print(f"{score:.3f}  {entry.path}")

    # Save/load for fast startup
    idx.save("cache/path_index")
    idx = PathIndex.load("cache/path_index")
```

### `connection_status`

```python
status = device.connection_status
# {
#     "connected": True,
#     "consecutive_failures": 0,
#     "auth_failure_count": 0,
#     "circuit_breaker_open": False,
#     "circuit_breaker_seconds_remaining": 0.0,
# }
```

### `cached_data()`

Returns the current cache snapshot (all data from recent `get()` calls and active subscriptions):

```python
cached = device.cached_data()
for item in cached:
    print(f"{item['path']} = {item['value']}")
```

---

## Context manager

The `Device` class supports context manager usage for automatic cleanup:

```python
with Device("10.0.0.1", username="admin", password="secret") as device:
    data = device.get("/system/hostname")
    # subscriptions, gRPC server, NETCONF session all cleaned up on exit
```

The context manager calls `connect()` on entry and `close()` on exit.

---

## Error handling

All exceptions inherit from `DeviceError`:

```
DeviceError (base)
  +-- ConnectionError      NETCONF connection failure
  +-- DiscoveryError       Module discovery / catalog build failure
  +-- PathNotFoundError    Requested path not in catalog
  +-- PathNotWritableError Path is config=false (read-only)
  +-- SetError             edit-config RPC failure
  +-- SubscriptionError    Subscription setup failure
```

### Import all exceptions

```python
from splice.api import (
    Device,
    DeviceError,
    ConnectionError,
    DiscoveryError,
    PathNotFoundError,
    PathNotWritableError,
    SetError,
    SubscriptionError,
)
```

### Catch-all pattern

```python
try:
    device.connect()
    data = device.get("/system/hostname")
except ConnectionError:
    print("Cannot reach device")
except DiscoveryError:
    print("YANG discovery failed")
except DeviceError as e:
    print(f"Something else went wrong: {e}")
```

---

## Kafka publishing

Enable Kafka to publish all poll and trigger updates to Kafka topics:

```python
device = Device(
    "10.0.0.1",
    username="admin",
    password="secret",
    kafka_brokers="localhost:9092",
    kafka_topic_prefix="splice",
)
device.connect()

# All get() and subscribe() results are now also published to Kafka
# Topics: splice.<device>.poll, splice.<device>.trigger
```

Requires the `confluent-kafka` package:

```bash
.venv/bin/pip install -e .[kafka]
```

---

## Connection resilience

The API reuses the same resilience mechanisms as the CLI:

### Reconnect-once semantics

If a NETCONF RPC fails, the pool closes the dead session, waits `reconnect_delay_sec`, and retries once. No retry loops.

### Circuit breaker

After `circuit_breaker_threshold` consecutive failures, the pool refuses new RPCs for `circuit_breaker_cooldown_sec`. This prevents session storms that overwhelm devices.

### Auth failure protection

Authentication errors immediately open the circuit breaker with a long cooldown to prevent account lockouts.

### Tuning

```python
device = Device(
    "10.0.0.1",
    reconnect_delay_sec=5.0,            # Wait 5s before reconnect
    circuit_breaker_threshold=3,         # Open after 3 failures
    circuit_breaker_cooldown_sec=120.0,  # Cool down for 2 minutes
)
```

---

## Thread safety

- `NetconfClientPool` serializes all RPCs through a lock (single NETCONF session)
- `MemoryCache` uses `RLock` + `Condition` for thread-safe reads/writes
- Each `Subscription` has its own poller thread and local cache
- `InMemoryCatalog` fields are set once during `build_catalog()` and read-only after

It is safe to call `get()`, `set()`, and `subscribe()` from multiple threads simultaneously.

---

## Recipes

### Quick read from a device

```python
from splice.api import Device

with Device("10.0.0.1", username="admin", password="secret") as dev:
    for item in dev.get("/system"):
        print(f"{item['path']} = {item['value']}")
```

### Fast reconnect with cached YANG

```python
# First time: full discovery (slow)
device = Device("10.0.0.1", username="admin", password="secret",
                yang_dir="./yang_cache/device1")
device.connect()
device.close()

# Second time: skip discovery (fast)
device = Device("10.0.0.1", username="admin", password="secret",
                yang_dir="./yang_cache/device1")
device.connect(skip_discover=True, skip_fetch_yang=True)
data = device.get("/system/hostname")
device.close()
```

### Subscribe with timeout

```python
import time

device = Device("10.0.0.1", username="admin", password="secret",
                yang_dir="./yang_cache/device1")
device.connect(skip_discover=True, skip_fetch_yang=True)

deadline = time.time() + 60  # run for 60 seconds
with device.subscribe("/interfaces/interface", interval=5) as sub:
    for batch in sub:
        for u in batch:
            print(f"{u['path']} = {u['value']}")
        if time.time() > deadline:
            break

device.close()
```

### Expose as gNMI server (standalone)

```python
from splice.api import Device

device = Device("10.0.0.1", username="admin", password="secret",
                yang_dir="./yang_cache/device1")
device.connect(skip_discover=True, skip_fetch_yang=True)

# Start background subscription to keep cache warm
device.subscribe("/", interval=30)

# Serve gNMI (blocks forever)
device.serve(port=9339, blocking=True)
```

### Multi-device setup

```python
from splice.api import Device

devices = [
    Device("10.0.0.1", username="admin", password="secret",
           yang_dir="./yang/dev1"),
    Device("10.0.0.2", username="admin", password="secret",
           yang_dir="./yang/dev2"),
]

for dev in devices:
    dev.connect(skip_discover=True, skip_fetch_yang=True)
    dev.subscribe("/interfaces/interface", interval=10)
    dev.serve(port=9339 + devices.index(dev), blocking=False)

# All devices are now being polled and serving gNMI
# Device 1 on :9339, Device 2 on :9340
input("Press Enter to stop...")

for dev in devices:
    dev.close()
```

### Inspect catalog without connecting

```python
from splice.api import Device

device = Device("10.0.0.1", yang_dir="/path/to/yang/files")
device.build_catalog()

# Explore available paths
for path in device.find_paths("*interface*"):
    print(path)
```

---

## Comparison: CLI vs Python API

| Feature | CLI (`cli.py`) | Python API |
|---------|---------------|------------|
| Database | PostgreSQL required | None (in-memory) |
| Config | YAML file required | Constructor args |
| Discovery | Full pipeline always | Granular, skippable steps |
| Startup time | 30-120s | 1-3s (with cached YANG) |
| Get/Set | Via gNMI client (gnmic) | Direct Python calls |
| Subscribe | Via gNMI Subscribe | Python iterator |
| Kafka | Config-driven | Constructor arg |
| Multi-device | Separate processes | Multiple `Device` objects |

---

## API reference summary

### Device methods

| Method | Description |
|--------|-------------|
| `connect(**skip_opts)` | Full pipeline with optional skips |
| `connect_netconf()` | SSH/NETCONF connect only |
| `discover()` | Discover YANG modules from device |
| `fetch_yang(throttle_sec=0.3)` | Fetch YANG schemas to disk |
| `build_catalog()` | Build catalog from YANG files on disk |
| `get(path)` | Direct NETCONF read |
| `set(path, value, ...)` | NETCONF edit-config write |
| `subscribe(*paths, interval, batch)` | Polling subscription iterator |
| `serve(port, bind, ...)` | Start gRPC gNMI server |
| `close()` | Cleanup all resources |
| `find_paths(pattern)` | Glob match against catalog |
| `cached_data()` | Current cache snapshot |
| `catalog` | `InMemoryCatalog` (use with `path_index`) |

### Device properties

| Property | Type | Description |
|----------|------|-------------|
| `catalog_paths` | `list[str]` | All discovered canonical paths |
| `connection_status` | `dict` | Circuit breaker state |

### Subscription methods

| Method | Description |
|--------|-------------|
| `__iter__()` / `__next__()` | Iterate over update batches |
| `cancel()` | Stop the poller thread |
