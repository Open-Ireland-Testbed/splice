# Performance Monitoring

Splice collects vendor PM (performance-monitoring) counters — optical power, BER, error
seconds, temperature, and similar interval statistics — from NETCONF devices and publishes
them over gNMI under a dedicated synthetic path tree. This page explains the PM data model,
how to enable discovery and collection, how PM filters fan out across list instances, how
drift is detected, and how to query the results.

## Why PM is special

Vendor `pm-current-data` trees are not normal catalog leaves. The same metric name (for
example `opt-rcv-pwr`) repeats under every port, card, and managed element on the device, and
the numeric value only makes sense alongside the instance it was measured on. If Splice mapped
PM into ordinary catalog paths, per-port metrics would collide and overwrite each other.

Instead, Splice parses `pm-current-data` directly (`pm.py`) and emits **synthetic,
instance-keyed paths** that are *not* catalog paths. PM data therefore lives in its own cache
namespace and never competes with discovered telemetry.

## PM data model

Each `montype-monval` pair becomes one gNMI update at a path of the form:

```text
/pm/{domain}[<instance keys>]/{name}/{mon_type}
```

| Segment | Source | Example |
|---|---|---|
| `/pm` | `pm.path_prefix` (default `/pm`) | `/pm` |
| `{domain}` | trailing token of the `pm-current-data` element's XML namespace | `otsi`, `interface`, `card` |
| `[<instance keys>]` | identifying key leaves of ancestor list elements | `[name=1%2F2%2F1]` |
| `{name}` | the entity's own `name` leaf inside `pm-current-data` | `1/2/1` |
| `{mon_type}` | the `mon-type` leaf of each measurement | `opt-rcv-pwr-avg` |

!!! note "How instance keys are discovered"
    Splice walks the XML ancestors of each `pm-current-data` element looking for identifying
    key leaves named `name`, `entity-name`, `index`, or `id` (see `_INSTANCE_KEY_LEAVES` in
    `pm.py`). The nearest ancestor wins on a local-name collision, and the entity's own
    `name` is skipped (it is already a path segment) so it is not duplicated as a key.

### Canonical key rendering

Instance keys are rendered exactly the way `gnmi_server._path_to_str` renders an incoming
client request, so a cached PM path and a client query compare equal:

- a **single** bracket containing all keys (`[k1=v1,k2=v2]`), not one bracket per key
- keys sorted **alphabetically** by key name
- values **percent-encoded**, which keeps `/`, `:`, and `,` out of the segment

!!! example "A keyed PM path"
    A receive-power average on interface `1/2/1`, where the namespace tail is `interface`:

    ```text
    /pm/interface[name=1%2F2%2F1]/1%2F2%2F1/opt-rcv-pwr-avg
    ```

    The `/` characters in the port name become `%2F` in both the key predicate and the
    `{name}` segment.

### Metadata carried per update

Alongside `path`, `value`, and `timestamp_ns`, each update carries a `meta` block with
`domain`, `name`, `mon_type`, the `instance` key map, `bin_interval`, `elapsed_time`, and
`suspect_status` — useful context from the source `pm-current-data` element.

Values are coerced to `int` or `float` when they parse as numbers; otherwise they stay
strings (`_coerce_value` in `pm.py`).

## Enabling PM discovery

PM discovery happens during `setup` and is off by default. It is controlled by the
`discovery` section (see [Configuration](configuration.md)).

```yaml
discovery:
  pm_discovery: true     # walk PM subtrees and build PM filters (default: false)
  pm_max_paths: 25       # cap on the number of distinct PM roots to build filters for
```

| Key | Default | Meaning |
|---|---|---|
| `discovery.pm_discovery` | `false` | Enable building PM filters during discovery/`setup`. |
| `discovery.pm_max_paths` | `25` | Maximum number of distinct PM root paths to construct filters for. |

Running `splice --config config.yaml setup` with `pm_discovery: true` discovers PM roots from
the catalog, builds one NETCONF subtree filter per root (or per instance when fanout is on),
and stores them in the `pm_filters` table.

## The PM collector

At runtime the PM poller reads the stored filters, fetches `pm-current-data`, parses it, and
publishes the resulting `/pm/...` updates into the cache. It is configured under `pm` and is
also off by default:

```yaml
pm:
  enabled: true                     # turn on the PM collector at run time
  interval_sec: 30                  # seconds between PM sweeps
  live_token: interval-indefinite   # only emit bins whose bin-interval contains this token
  path_prefix: /pm                  # root segment for all synthetic PM paths
```

| Key | Default | Meaning |
|---|---|---|
| `pm.enabled` | `false` | Run the PM collector while `splice run` is serving. |
| `pm.interval_sec` | `30` | Interval between PM collection sweeps. |
| `pm.live_token` | `interval-indefinite` | Substring matched (case-insensitively) against each `pm-current-data`'s `bin-interval`; non-matching bins are skipped. |
| `pm.path_prefix` | `/pm` | Leading path segment for every synthetic PM update. |

!!! tip "live_token filters out historical bins"
    Devices typically report multiple PM bins (current 15-minute, 24-hour, and historical
    rollups). `live_token` selects only the live/current bin — by default the one whose
    `bin-interval` contains `interval-indefinite`. Set it to match whatever token your vendor
    uses for the continuously-updating bin, or to an empty string to emit every bin.

## PM fanout

A single `pm-current-data` filter cut at a keyed list element (for example one interface)
returns **every PM domain below that instance in one RPC**. Without fanout, Splice would issue
one narrow RPC per (root × instance) — hundreds of round trips per sweep on
session-throttled devices. Fanout collapses those into one subtree filter per instance.

```yaml
discovery:
  pm_fanout: true
  pm_fanout_limit: 0                                  # 0 = no cap; else first N instances
  pm_selector_list_path: /if:interfaces/if:interface  # the list to fan out over
  pm_selector: "name~'1/2/'"                           # optional: which instances to keep
```

| Key | Default | Meaning |
|---|---|---|
| `discovery.pm_fanout` | `false` | When no selector is set, fan out over **all** instances of the fanout list instead of just the first. |
| `discovery.pm_fanout_limit` | `0` | Cap the number of fanned-out instances (0 = unlimited). |
| `discovery.pm_selector_list_path` | `None` | The list path whose instances collapse into one filter each. PM roots under this list use the fat per-instance filter; roots elsewhere keep narrow per-root filters. |
| `discovery.pm_selector` | `None` | Selector expression restricting which instances of the fanout list get filters. Applies **only** to the fanout list. |

### How filters collapse at the list path

Splice picks the fanout list from `pm_selector_list_path` and, for each selected instance,
builds **one** subtree filter keyed to that instance — pinning ancestor lists (such as
`managed-element`) to their first instance. PM roots that are **not** under the fanout list
each get a single narrow per-root filter, with ancestor lists pinned to their first instance.
Because `parse_pm_current_data` re-keys results from the reply XML itself, the instances the
device actually returns are always distinguishable in the output paths regardless of how the
filter was cut.

!!! warning "Never emit an unkeyed filter at a list element"
    A subtree filter cut at an unkeyed list element would fetch **all** instances at once.
    Splice always keys the fanout list to a specific instance for exactly this reason. Keep
    `pm_selector_list_path` pointed at the list element itself (for example the `interface`
    list), and let fanout/selector decide which instances to include.

### Selector grammar

`pm_selector` uses a small `and`/`or` grammar with `=` (exact) and `~` (regex) operators,
matched against the instance's key leaves (`parse_selector`/`match_selector` in `pm.py`):

```text
name~'1/2/' and type='transceiver'
```

If a selector matches zero current instances, Splice logs a warning and builds no
instance-keyed PM filters — usually a sign the device was re-provisioned and the selector no
longer matches. Compare it against `splice --config config.yaml refresh-list-instances`.

## Drift refresh at run start

Re-provisioning a device (for example re-cabling line ports to different interface instances)
leaves the stored PM filters pointing at instances that no longer exist. Devices return
**empty data — not errors** — for ghost instance keys, so this fails silently unless caught.

```yaml
discovery:
  pm_refresh_on_run: true   # default: true
```

When `pm_refresh_on_run` is enabled (the default), `splice run` re-resolves the fanout list
instances at startup, compares them against the stored keyed filters, and rebuilds the filters
if they drifted. With it disabled, filters are used as stored.

### The `doctor` pm_filters check

`splice --config config.yaml doctor` includes a `pm_filters` check that connects to the device,
resolves the current list instances, and flags:

- **Ghost instances** — stored PM filters whose `key_values` are no longer present on the
  device under their `list_path`.
- **Stale selectors** — a `pm_selector` that matches 0 of the device's current instances.

```bash
splice --config config.yaml doctor
```

A failing check recommends re-running `setup` to rebuild PM filters, or starting `run` with
`discovery.pm_refresh_on_run` enabled (which rebuilds them automatically). A passing check
reports how many instance-keyed PM filters match live device instances.

## Empty and sentinel value skipping

PM `mon-val`s are numeric measurements. Unlit lines and unprovisioned ports report **no
measurement** — either an empty string or a placeholder like `NA`, `n/a`, `-`, `--`,
`unavailable`, `not-available`, `none`, or `null` (`_NO_MEASUREMENT_VALUES` in `pm.py`).
Splice **skips** these updates entirely.

!!! warning "Why sentinels are dropped, not published"
    Publishing a sentinel string for a numeric field would create a string-typed field in a
    downstream TSDB (for example InfluxDB), which then **rejects later numeric writes** for the
    same field. The skip list is kept tight so genuine string-valued telemetry is not silently
    discarded.

## Querying PM data over gNMI

PM paths are served like any other gNMI path. Use the full synthetic path, including the
percent-encoded keys. With [`gnmic`](https://gnmic.openconfig.net):

```bash
# Subscribe-once: current value of one PM metric on a specific port
gnmic -a 192.0.2.15:9339 --insecure subscribe --mode once \
  --path '/pm/interface[name=1%2F2%2F1]/1%2F2%2F1/opt-rcv-pwr-avg'

# Get everything under the PM tree
gnmic -a 192.0.2.15:9339 --insecure get --path '/pm'

# All PM metrics for one interface instance
gnmic -a 192.0.2.15:9339 --insecure get \
  --path '/pm/interface[name=1%2F2%2F1]'
```

!!! tip "Finding PM paths"
    PM paths are synthetic and are not in the catalog, so `find-paths` will not list them. The
    quickest way to discover what is being published is a `get` against the `/pm` prefix (or
    `pm.path_prefix` if you changed it) on a running server, then narrow down from there.

## Related pages

- [Configuration](configuration.md) — the full `discovery` and `pm` config reference.
- [Quickstart](quickstart.md) — running `setup`, `run`, and `doctor`.
- [Troubleshooting](troubleshooting.md) — empty responses, auth lockouts, and session limits.
