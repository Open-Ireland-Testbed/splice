# Docker + Grafana: IP to Dashboard

This is the simplest “copy‑paste” path from a device IP to a Grafana dashboard.
It assumes you already have the device credentials and can reach NETCONF.

## What you’ll get
- A local gNMI server for your device
- A Telegraf → InfluxDB → Grafana pipeline (TIG)

## 1) Start the stack

Make sure Docker + docker‑compose are installed, then run the compose file:

```bash
docker compose -f docs/docker-compose.yml up -d
```

This brings up:
- Postgres for the bridge
- One or more Splice services
- InfluxDB + Grafana (via TIG example)

## 2) Add your device

Create a device folder:

```
devices/<device_name>/
  config.yaml
  password
  yang/
```

Paste your password:

```bash
echo "<netconf-password>" > devices/<device_name>/password
chmod 600 devices/<device_name>/password
```

Minimal config example:

```yaml
device:
  host: <device-ip>
  port: 830
  username: <username>
  password_file: devices/<device_name>/password
  name: <device_name>

db:
  url: postgresql+psycopg://postgres:postgres@postgres:5432/splice

yang_dir: /app/devices/<device_name>/yang

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
```

## 3) Run discovery once

```bash
docker compose -f docs/docker-compose.yml exec bridge \
  sh -c 'PYTHONPATH=src python src/splice/cli.py --config devices/<device_name>/config.yaml migrate'
docker compose -f docs/docker-compose.yml exec bridge \
  sh -c 'PYTHONPATH=src python src/splice/cli.py --config devices/<device_name>/config.yaml discover'
docker compose -f docs/docker-compose.yml exec bridge \
  sh -c 'PYTHONPATH=src python src/splice/cli.py --config devices/<device_name>/config.yaml fetch-yang'
docker compose -f docs/docker-compose.yml exec bridge \
  sh -c 'PYTHONPATH=src python src/splice/cli.py --config devices/<device_name>/config.yaml build-catalog --include-config'
docker compose -f docs/docker-compose.yml exec bridge \
  sh -c 'PYTHONPATH=src python src/splice/cli.py --config devices/<device_name>/config.yaml build-aliases'
```

## 4) Start the gNMI server

```bash
docker compose -f docs/docker-compose.yml exec bridge \
  sh -c 'PYTHONPATH=src python src/splice/cli.py --config devices/<device_name>/config.yaml run'
```

## 5) Connect Telegraf to gNMI

In `telegraf.conf`, add:

```toml
[[inputs.gnmi]]
  addresses = ["bridge:9339"]
  insecure = true
  encoding = "json_ietf"
  subscription_mode = "sample"
  sample_interval = "10s"
  paths = [
    "/",
  ]
```

## 6) Grafana

Open Grafana, add InfluxDB data source, and import any dashboard or explore metrics by measurement.

That’s it.

If you see no metrics, check:
- `troubleshooting.md`
- `gnmic -a 127.0.0.1:9339 --insecure get --path /`
