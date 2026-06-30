# Quickstart

This guide gets a single device online quickly using the vendor-agnostic pipeline.

## Prerequisites

- Python 3.10+ with a virtual environment (`.venv`)
- PostgreSQL running locally (the database itself is created automatically)
- NETCONF reachability to the target device

### Install

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

## 1) Create device folder

```
devices/<device_name>/
  config.yaml
  password
  yang/          # created automatically by fetch-yang
```

Store your NETCONF password:

```bash
mkdir -p devices/<device_name>
echo "<netconf-password>" > devices/<device_name>/password
chmod 600 devices/<device_name>/password
```

## 2) Write the config

Create `devices/<device_name>/config.yaml`:

```yaml
device:
  host: <device-ip>
  port: 830
  username: <username>
  password_file: devices/<device_name>/password
  name: <device_name>

db:
  url: postgresql+psycopg://postgres:postgres@localhost:5432/splice

yang_dir: /absolute/path/to/devices/<device_name>/yang

gnmi:
  bind: 0.0.0.0
  port: 9339
  heartbeat_sec: 10
  expansion_mode: direct   # recommended; 'full' runs fresh NETCONF discovery (slow)

poller:
  default_interval_sec: 30
  batch_enabled: true
  batch_mode: top
  task_throttle_ms: 50
  # Lockout-safety settings (safe defaults)
  reconnect_delay_sec: 2.0
  circuit_breaker_threshold: 5
  circuit_breaker_cooldown_sec: 60.0
  auth_failure_cooldown_sec: 300.0  # Long cooldown for auth failures

cache:
  ttl_sec: 60
  warm_start: false
```

> **Tip:** Each device gets its own config, password file, and gNMI port.
> For a second device use port `9340`, etc.

## 3) Run setup

A single command creates the database (if needed), runs migrations, discovers
YANG modules, fetches schemas, builds the telemetry catalog, initialises
polling jobs, and generates Grafana dashboards:

```bash
PYTHONPATH=src .venv/bin/python src/splice/cli.py \
  --config devices/<device_name>/config.yaml setup
```

You will see progress for each step:

```
[1/8] Checking database...           created     # or "OK" if it already exists
[2/8] Checking device connectivity...  OK
[3/8] Running migrations...          7 applied   # or "up to date"
[4/8] Discovering YANG modules...    42 modules
[5/8] Fetching YANG schemas...       42 fetched
[6/8] Building catalog + aliases...  1284 paths
[7/8] Initializing polling jobs...   1284 jobs
[8/8] Generating Grafana dashboards... 12 dashboards
```

> **No manual `createdb` or SQL scripts needed.** The `setup` command
> auto-creates the PostgreSQL database and applies all migrations.

### Setup options

| Flag | Effect |
|------|--------|
| `--include-config` | Include YANG `config true` leaves in the catalog |
| `--interval 60` | Override the default polling interval (seconds) |
| `--skip-migrate` | Skip the migration step (if already up to date) |
| `--skip-list-instances` | Skip keyed list instance discovery |

## 4) Start the gNMI server

```bash
PYTHONPATH=src .venv/bin/python src/splice/cli.py \
  --config devices/<device_name>/config.yaml run
```

## 5) Verify with gnmic

```bash
gnmic -a 127.0.0.1:9339 --insecure capabilities
gnmic -a 127.0.0.1:9339 --insecure get --path /
```

If you get values back, your bridge is live.

## Running individual pipeline steps

If you prefer to run the discovery pipeline step-by-step instead of `setup`:

```bash
CFG="--config devices/<device_name>/config.yaml"

# Database (auto-created) + migrations
PYTHONPATH=src .venv/bin/python src/splice/cli.py $CFG migrate

# Discovery pipeline
PYTHONPATH=src .venv/bin/python src/splice/cli.py $CFG discover
PYTHONPATH=src .venv/bin/python src/splice/cli.py $CFG fetch-yang
PYTHONPATH=src .venv/bin/python src/splice/cli.py $CFG build-catalog --include-config
PYTHONPATH=src .venv/bin/python src/splice/cli.py $CFG build-aliases
PYTHONPATH=src .venv/bin/python src/splice/cli.py $CFG init-jobs --interval 30
```

> The `migrate` command also auto-creates the database if it doesn't exist.

## Expansion modes

The `expansion_mode` setting controls how keyed list instances are resolved:

| Mode | Behaviour |
|------|-----------|
| `direct` | Uses cached list instances from the database. Fast and safe. **Recommended.** |
| `full` | Runs a fresh NETCONF discovery to enumerate all list instances. Generates many GETs and can overwhelm devices with session limits. Use only when you need a complete refresh. |
| `lazy` | Resolves list instances on first gNMI request. |

## PM / FEC-BER (optional)

> **Note:** Standard OpenConfig transceiver paths may not expose FEC BER on all
> devices. Vendor-specific PM data (e.g. ADVA QuadFlex FEC-BER) is only available
> through PM discovery paths. Enable the `discovery.pm_discovery` config section
> below to access this data.

Enable PM discovery for vendor PM paths by adding to your config:

```yaml
discovery:
  pm_discovery: true
  pm_max_paths: 200
  pm_fanout: true
  pm_selector_list_path: /p1:bulk-data/p2:managed-element/p3:interface
  pm_selector: "name='1/2/n2/ot300'"

pm:
  enabled: true
  interval_sec: 5
  live_token: "interval-indefinite"
  path_prefix: "/pm"
```

Then rebuild the catalog and restart:

```bash
PYTHONPATH=src .venv/bin/python src/splice/cli.py \
  --config devices/<device_name>/config.yaml build-catalog --include-config

PYTHONPATH=src .venv/bin/python src/splice/cli.py \
  --config devices/<device_name>/config.yaml run

# PM paths are keyed by their originating instance:
gnmic -a 127.0.0.1:9339 --insecure get --path "/pm/otnpm-data"
gnmic -a 127.0.0.1:9339 --insecure get --path '/pm/otnpm-data[interface=1/2/n1/ot400,managed-element=1]/FEC/acor-factt:fec-ber'
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `setup` step 1 fails | Check PostgreSQL is running and `db.url` is correct |
| `setup` step 2 fails | Check device is reachable: `ssh <user>@<host> -p 830 -s netconf` |
| Empty `gnmic get` response | The poller may not have completed a cycle yet — wait one interval |
| `NETCONF session limit` errors | Increase `poller.task_throttle_ms`, reduce jobs, and use longer intervals |

See [troubleshooting.md](troubleshooting.md) for more.

## Trigger reads and Kafka (optional)

If you want on-demand reads (fresh NETCONF reads outside poll cadence), enable:

```yaml
trigger:
  enabled: true

trigger_api:
  enabled: true
  bind: 127.0.0.1
  port: 9812
  bearer_token: "replace-me"
```

Then call:

```bash
curl -sS -X POST http://127.0.0.1:9812/trigger \
  -H "Authorization: Bearer replace-me" \
  -H "Content-Type: application/json" \
  -d '{"paths":["/system-state/platform/machine"],"publish_kafka":false}'
```

Optional Kafka publishing:

```yaml
kafka:
  enabled: true
  brokers: "localhost:9092"
  topic_prefix: "splice"
  publish_poll_updates: true
  publish_trigger_events: true
```

Install extra dependency before enabling Kafka:

```bash
.venv/bin/pip install -e .[kafka]
```
