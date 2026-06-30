# Writing Config (gNMI Set)

Splice can push configuration back to a device by translating a gNMI `Set` request into a NETCONF `<edit-config>` RPC. This page covers how to enable writes, how the datastore is selected, the value-typing rules clients must follow, and the safety layers that keep Set disabled until you explicitly opt in.

!!! warning "Writes are off by default — and for good reason"
    Set is disabled unless you turn it on (`gnmi.set_enabled: false` by default). Pushing config to production optical gear can disrupt live traffic. Validate against a lab device or the simulator first, scope writes narrowly, and read [Safety](#safety-deny-by-default) before enabling on real hardware.

## How it works

A gNMI `Set` request carries `update`, `replace`, and `delete` operations. Splice maps each to a NETCONF edit operation, merges them into a single `<config>` subtree, and applies it with one `<edit-config>`:

| gNMI operation | NETCONF `nc:operation` | `UpdateResult.op` |
|----------------|------------------------|-------------------|
| `update`       | `merge`                | `UPDATE`          |
| `replace`      | `replace`              | `REPLACE`         |
| `delete`       | `delete`               | `DELETE`          |

The pipeline (see `netconf/set_request.py` → `netconf/setter.py` → `netconf/set_executor.py`):

1. **Validate & normalize** (`validate_set_request`) — each path is resolved against the catalog and alias map, checked for `config: true` (writable) and complete list keys, and its typed value decoded into a Python scalar.
2. **Build XML** (`build_config_xml`) — shared ancestors are merged into one subtree; keyed list entries render their key leaves; each leaf gets the right `nc:operation`.
3. **Select datastore** (`_select_datastore`) — based on `set_datastore` and the device's advertised capabilities.
4. **Apply** (`_apply_edit_config`) — optional lock, `edit-config`, and (for candidate) `commit`, with `discard-changes` rollback on failure.

## Enabling Set

Set is wired up only when `gnmi.set_enabled` is true. Add a `gnmi` block to your config:

```yaml
gnmi:
  bind: 0.0.0.0
  port: 9339
  set_enabled: true        # enable gNMI Set -> NETCONF edit-config
  set_datastore: auto      # auto | running | candidate
  set_use_lock: false      # lock/unlock the datastore around the transaction
  set_auto_refresh: false  # best-effort cache read-back after a successful Set
```

The real defaults from `config.py` (`GNMIConfig`) are:

| Key | Default | Meaning |
|-----|---------|---------|
| `set_enabled` | `false` | When false, `Set` returns gRPC `UNIMPLEMENTED` ("Set is disabled"). |
| `set_datastore` | `auto` | Datastore preference: `auto`, `running`, or `candidate`. |
| `set_use_lock` | `false` | Acquire a NETCONF `<lock>` on the target datastore for the duration of the write. |
| `set_auto_refresh` | `false` | After a successful Set, best-effort invalidate/read-back affected paths so cached telemetry reflects the change sooner. |

When Set is enabled, `splice run` primes and caches the device's capabilities at startup and logs a line like:

```text
gNMI Set enabled (datastore=candidate, auto_refresh=False, capabilities=42)
```

See [Configuration](configuration.md) for the full config reference.

### Datastore selection (`set_datastore`)

`_select_datastore()` inspects the device's NETCONF capabilities:

- **`auto`** — prefer `:candidate` if advertised; otherwise fall back to `:writable-running`. If the device advertises neither, the request fails with `FAILED_PRECONDITION` ("device does not advertise :candidate or :writable-running capability").
- **`candidate`** — require `:candidate`. Splice writes to the candidate datastore and then issues `<commit>`. On any failure it calls `<discard-changes>` to roll back.
- **`running`** — require `:writable-running`. Splice writes directly to the running datastore (no commit step).

!!! note "Candidate + commit vs. writable-running"
    Candidate-capable devices (e.g. many ConfD-based platforms) get a transactional write: edit the candidate, then commit, with automatic discard on error. Writable-running devices apply the edit immediately to the live config — there is no commit boundary and no automatic rollback, so changes take effect the instant the `edit-config` succeeds.

### Locking (`set_use_lock`)

With `set_use_lock: true`, the executor wraps the transaction in `<lock>` / `<unlock>` on the target datastore. The unlock runs in a `finally` block so the datastore is released even if the edit or commit fails. Use this when other clients may be writing concurrently and you need exclusive access for the duration of the change.

### Auto-refresh (`set_auto_refresh`)

By default, the cache and DB only update on the next poll cycle, so a freshly written value may read back stale for a few seconds. With `set_auto_refresh: true`, after a successful Set Splice performs a best-effort refresh:

- For `delete` mutations, it evicts the affected paths (and descendants) from the cache and `last_value` table.
- For `merge`/`replace` mutations, it issues a targeted `get-config` read-back of just the written paths and ingests the result.

!!! note "Best-effort only"
    The refresh never fails a successful Set transaction — errors are logged and swallowed. The background poller can still race and briefly overwrite the read-back with slightly older data.

## Value typing rules

`validate_set_request` decodes the gNMI `TypedValue` on each `update`/`replace`. Only **scalar** values are supported:

| TypedValue oneof | Result |
|------------------|--------|
| `string_val` | string |
| `int_val` | integer |
| `uint_val` | integer |
| `double_val` / `float_val` | float |
| `bool_val` | boolean (rendered as `true`/`false` in XML) |
| `json_ietf_val` | scalar JSON only (string/number/bool); objects, arrays, and `null` are rejected |

Anything else (including `union_replace`) is rejected with gRPC `UNIMPLEMENTED`. A `delete` must not carry a value.

!!! tip "gnmic typing: use `:::string:::`"
    `gnmic set` defaults to a JSON value encoding that this server does not accept for arbitrary leaves. Pass an explicit type with the `path:::type:::value` form so gnmic sends a `string_val` (or other scalar) instead:

    ```bash
    gnmic -a 127.0.0.1:9339 --insecure set \
      --update '/p1:interfaces/p1:interface[name=eth0]/p1:description:::string:::lab-uplink'
    ```

## gnmic examples

!!! example "Merge (update) a writable leaf"
    ```bash
    gnmic -a 127.0.0.1:9339 --insecure set \
      --update '/p1:interfaces/p1:interface[name=eth0]/p1:enabled:::bool:::true'
    ```

!!! example "Replace a leaf value"
    ```bash
    gnmic -a 127.0.0.1:9339 --insecure set \
      --replace '/p1:interfaces/p1:interface[name=eth0]/p1:mtu:::int:::9000'
    ```

!!! example "Delete a leaf"
    ```bash
    gnmic -a 127.0.0.1:9339 --insecure set \
      --delete '/p1:interfaces/p1:interface[name=eth0]/p1:description'
    ```

Keyed list entries must include their full set of keys — Splice rejects a write whose predicate omits a required list key ("missing list keys for ...").

## The NETCONF base-namespace requirement

`build_config_xml` always emits the `<config>` element in the NETCONF base namespace:

```xml
<config xmlns="urn:ietf:params:xml:ns:netconf:base:1.0" xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0" ...>
  ...
  <p1:enabled nc:operation="merge">true</p1:enabled>
</config>
```

!!! warning "Why this matters"
    Without the default `xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"` on `<config>`, ncclient/lxml serializes a bare `<config>` with no namespace, and ConfD-based devices reject the RPC with "Missing element's namespace". The base namespace also scopes the `nc:operation` attributes. Each YANG module prefix used in the paths is declared as an `xmlns:<prefix>` on `<config>`; if a path uses a prefix with no known namespace mapping, the build fails ("missing namespace mappings for prefixes: ...") rather than emitting ambiguous XML.

Element and key names are validated as XML QNames before being interpolated into tags (values are XML-escaped), so a crafted path or key cannot break out of the tag structure.

## Error responses

The gRPC `Set` handler (`gnmi_server.py`) maps internal errors to status codes:

| Condition | gRPC status |
|-----------|-------------|
| Set disabled (`set_enabled: false`) | `UNIMPLEMENTED` ("Set is disabled") |
| Path not writable / not in catalog / bad predicate / missing key | `INVALID_ARGUMENT` |
| Unsupported value type or `union_replace` | `UNIMPLEMENTED` |
| Datastore capability missing / empty request | `FAILED_PRECONDITION` |
| Any other failure | `INTERNAL` ("Set failed") |

## Safety: deny-by-default

Several layers gate writes:

- **Server-wide off switch** — `set_enabled` defaults to `false`. No write reaches the device unless you flip it.
- **Catalog writability check** — a path must be `config: true` in the catalog. Read-only (state) paths are rejected before any device contact.
- **List-key completeness** — keyed list writes must specify every key for each list ancestor.
- **Capability preconditions** — the chosen datastore must actually be advertised by the device.
- **Candidate rollback** — for candidate writes, any edit/commit failure triggers `discard-changes`.

## MCP `set_config` tool and set policy

The [MCP server](index.md) exposes a `set_config` tool for AI agents, built on `Device.set()`. It adds a second, independent policy gate on top of everything above.

```bash
splice mcp --policy policy.yaml --audit-log audit.jsonl
```

The tool signature:

```text
set_config(device_id, path, value, operation="merge", dry_run=False)
```

- **`dry_run=True`** validates the request against the policy and returns `"policy_check": "passed"` without touching the device.
- Denials return a structured `SET_POLICY_DENIED` error.

### Set policy (`SetPolicy`)

Policy is **deny-all by default**: with no `allowed_paths`, every write is rejected ("no paths are allowed (default deny-all policy)"). Define it under a `set_policy` block in the policy YAML:

```yaml
set_policy:
  allowed_paths:
    - "/p1:interfaces/p1:interface[*]/p1:description"
    - "/p1:interfaces/p1:interface[*]/p1:enabled"
  forbidden_paths:
    - "*/admin-state"
  value_constraints:
    "*/mtu":
      min: 1500
      max: 9216
```

Evaluation rules (`SetPolicy.check`):

- Patterns are shell-style globs (`fnmatch`).
- **Forbidden wins**: if *any* canonical form of the path matches a `forbidden_paths` pattern, it is denied.
- **Allow requires all forms**: the write is allowed only if *every* equivalent form (raw, predicate-stripped, alias-resolved) matches at least one `allowed_paths` pattern — a fail-closed design that prevents bypass via alternate path canonicalizations.
- **Value constraints**: optional numeric `min`/`max` bounds keyed by a glob pattern; the first matching constraint applies.

!!! warning "Writing to production gear"
    Even with a policy in place, treat enabling Set on live optical equipment as a privileged operation:

    - Start on a lab device or `splice --simulate openroadm` before any real target.
    - Keep `allowed_paths` as narrow as possible and pair it with `forbidden_paths` for anything destructive.
    - Use `dry_run=True` (MCP) to confirm a write is permitted before applying it.
    - Prefer `set_datastore: candidate` where supported so changes are transactional.
    - Never commit device credentials — supply `NETCONF_PASSWORD` via env or `device.password_file`.

## See also

- [Configuration](configuration.md) — full `gnmi` config reference.
- [Python API](python-api.md) — `Device.set()` and `device.serve(set_enabled=True)`.
- [Troubleshooting](troubleshooting.md) — diagnosing rejected writes.
