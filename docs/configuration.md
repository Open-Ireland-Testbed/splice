# Configuration Reference

Configuration is YAML-based with one file per device at `devices/<device_name>/config.yaml`.
A complete starter template ships as [`config.example.yaml`](../config.example.yaml) at the repo
root — copy it to `devices/<device_name>/config.yaml` and edit, or generate one with `splice init`.

## Full config reference

```yaml
# NETCONF device
device:
  host: 192.0.2.15
  port: 830
  username: <username>
  password:                # use NETCONF_PASSWORD env var or password_file instead
  password_file: /path/to/password-file
  name: roadm_10

# PostgreSQL
db:
  url: postgresql+psycopg://user:pass@host:5432/dbname

# Local YANG repository (per-device)
yang_dir: /path/to/yang

# gNMI server
gnmi:
  bind: 0.0.0.0
  port: 9339
  heartbeat_sec: 10
  max_runtime_sec:         # optional
  expansion_mode: direct   # full|direct|lazy
  set_enabled: false       # enable gNMI Set -> NETCONF edit-config
  set_datastore: auto      # auto|running|candidate
  set_auto_refresh: false  # best-effort read-back after successful Set
  set_use_lock: false      # optional NETCONF lock/unlock around Set transaction

# Trigger reads (optional)
trigger:
  enabled: false
  auto_onboard: true           # create/update poller jobs for triggered paths
  default_interval_sec: 30     # interval used when auto_onboard=true
  max_paths_per_request: 50
  netconf_timeout_sec: 10.0    # reserved for future per-trigger timeout control

# HTTP trigger endpoint (optional)
trigger_api:
  enabled: false
  bind: 0.0.0.0
  port: 9812
  bearer_token: ""             # required when enabled

# Kafka publisher (optional)
kafka:
  enabled: false
  brokers: localhost:9092      # required when enabled
  client_id: splice
  topic_prefix: splice
  publish_poll_updates: true
  publish_trigger_events: true
  acks: all
  compression: lz4
  linger_ms: 20

# Poller
poller:
  default_interval_sec: 30
  batch_enabled: true
  batch_mode: top                     # top|module
  task_throttle_ms: 50                # delay between tasks (ms)
  preserve_namespaces: false
  list_refresh_sec: 0                 # set >0 to auto-refresh keyed list instances
  rules:
    - pattern: "/edfas"
      interval: 5
  reconnect_delay_sec: 2.0            # delay before reconnect after session drop
  circuit_breaker_threshold: 5        # consecutive failures before circuit opens
  circuit_breaker_cooldown_sec: 60.0  # seconds to pause after threshold
  auth_failure_cooldown_sec: 300.0    # long cooldown for auth failures (prevents lockout)

# Discovery (optional)
discovery:
  inspect_components: false
  component_limit: 20
  component_pattern:
  resolve_all_lists: false
  list_batch_size: 50
  list_sleep_sec: 0.5
  pm_discovery: false
  pm_max_paths: 25
  pm_fanout: false
  pm_fanout_limit: 0
  pm_selector_list_path:
  pm_selector:
  pm_adapter_modules: []

# PM collector (optional)
pm:
  enabled: false
  interval_sec: 30
  live_token: "interval-indefinite"
  path_prefix: "/pm"

# NETCONF notifications (optional)
notifications:
  enabled: false
  poll_sec: 1
  paths: []

# In-memory cache
cache:
  ttl_sec: 60
  warm_start: true

# Telemetry history storage
history:
  enabled: false
  storage: db       # db|file
  file_path:        # JSONL path for NAS

# Health endpoint
health:
  enabled: true
  bind: 0.0.0.0
  port: 9809
```

## Credentials

Never commit passwords. Use one of:

```bash
# Option 1: environment variable
export NETCONF_PASSWORD="<netconf-password>"

# Option 2: password file (recommended for multi-device)
echo "<netconf-password>" > devices/<device_name>/password
chmod 600 devices/<device_name>/password
```

Then reference it in config:

```yaml
device:
  password_file: devices/<device_name>/password
```

## Expansion modes

| Mode | Behaviour |
|------|-----------|
| `direct` | Use cached list instances only (default, safe) |
| `full` | Fresh NETCONF discovery on every `run` start |
| `lazy` | Like direct, but expand on first gNMI request |

## Batching modes

| Mode | Behaviour |
|------|-----------|
| `top` | One GET per top-level container (fewer RPCs) |
| `module` | Coalesce by module + top-level (smallest payloads) |

## Curated metrics (`metrics`)

```yaml
metrics:
  enabled: true
  default_interval_sec: 10
  disable_uncurated: true      # disable (never delete) jobs matching no pattern
  include:
    - pattern: "fec-ber"                                  # substring, case-insensitive
    - { pattern: "opt-(rcv|trmt)-pwr", regex: true, interval_sec: 5 }
```

When enabled, `setup`/`init-jobs` create jobs only for catalog paths matching
an include pattern (first match wins its interval). Patterns are tested
against both the module-qualified and prefix-stripped path forms. Apply to an
existing database with `splice --config ... curate-jobs`. PM paths are
produced by the PM poller and are not affected.

Manually disabled jobs stay disabled across `setup`/`init-jobs` re-runs; they
are re-enabled only by explicit intent (`set-job`, curation `include` match,
or trigger auto-onboarding).

## Poller task failure limit (`poller.task_failure_limit`)

After N consecutive failures (default 5) a polling task is dropped from the
rotation for the lifetime of the process and reported in `/health` and
`splice status`. Circuit-breaker pauses never count toward the limit,
and a sweep that ends in a breaker pause discards that sweep's failure
counts (a device-wide outage must not disable individual paths). Set 0 to
disable. Tasks are re-enabled by restart or any job/filter refresh.

## PM drift refresh (`discovery.pm_refresh_on_run`)

Default `true`: at `run` start the bridge re-resolves the PM fanout list
(`discovery.pm_selector_list_path`) against the device, refreshes
`list_instances`, and rebuilds `pm_filters` when instances changed — so a
re-provisioned port (e.g. ot200 → ot400) is picked up automatically instead
of polling ghost instances forever. `doctor` has a matching `pm_filters`
check. Transient failures keep the cached filters.

## Trigger extension contract

When `trigger.enabled: true`, gNMI `Get` accepts registered extension `id=61001`
with JSON payload:

```json
{"mode":"TRIGGER","publish_kafka":true,"auto_onboard":false,"interval_sec":10}
```

Rules:
- `mode` must be `"TRIGGER"`.
- `publish_kafka` and `auto_onboard` are optional booleans.
- `interval_sec` is optional integer `> 0`.
- Invalid payload returns `INVALID_ARGUMENT`.
- If trigger is disabled, request returns `UNIMPLEMENTED`.

## Kafka topic naming

When Kafka publishing is enabled, events are sent to:

- `<topic_prefix>.<device>.poll`
- `<topic_prefix>.<device>.trigger`

`<device>` and `<topic_prefix>` are sanitized to `[a-zA-Z0-9_.-]`.

> The default `topic_prefix` and `client_id` are both `splice`, so topics
> default to `splice.<device>.poll` / `splice.<device>.trigger`. Set
> `topic_prefix` explicitly to route events to a different namespace.

## Multi-device layout

One gNMI server instance per NETCONF device, each with its own config and port:

```
devices/
  roadm_10/
    config.yaml      # gnmi.port: 9339
    password
    yang/
  teraflex/
    config.yaml      # gnmi.port: 9340
    password
    yang/
```
