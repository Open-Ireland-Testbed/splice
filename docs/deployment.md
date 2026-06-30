# Deployment

This guide covers running Splice in production: the multi-device model, packaging with Docker / docker-compose, systemd units, secrets handling, the health endpoint, and performance tuning. For configuration keys see [Configuration](configuration.md); for the commands referenced here see the [CLI Reference](cli.md).

## Multi-device model

Splice runs **one gNMI server process per NETCONF device**. There is no single multi-device daemon — each device is an independent process with its own config file, its own gNMI port, and its own NETCONF connection pool.

- **One process per device.** Each `splice --config <device>/config.yaml run` serves exactly one device.
- **Separate gNMI ports.** The first device typically uses the default `gnmi.port: 9339`, the next `9340`, and so on. Set this per device in each `config.yaml`.
- **Shared PostgreSQL.** All devices can point `db.url` at the same PostgreSQL instance. Rows are scoped by a `device_id` foreign key, so catalogs, jobs, list instances, and telemetry from different devices coexist in one database without collision.
- **Per-device YANG directory.** Keep a dedicated `yang_dir` per device so vendor models never cross-contaminate one another.

!!! note "Why one process per device"
    Each NETCONF device has its own circuit breaker, poller throttle, and session pool. Isolating devices into separate processes means a device that locks out or goes unreachable cannot stall polling for the others, and you can restart, upgrade, or reconfigure one device without touching the rest.

### Recommended layout

```
splice/
  devices/
    <device_name>/
      config.yaml      # device connection, gnmi.port, db.url, poller, ...
      password         # NETCONF password (chmod 600, never committed)
      yang/            # device-specific YANG modules
  logs/
```

A two-device deployment differs only in `device.host` and `gnmi.port`:

```yaml
# devices/roadm_a/config.yaml
device:
  host: 192.0.2.10
  username: admin
gnmi:
  port: 9339
db:
  url: postgresql+psycopg://postgres:postgres@localhost:5432/splice
```

```yaml
# devices/roadm_b/config.yaml
device:
  host: 192.0.2.11
  username: admin
gnmi:
  port: 9340          # distinct port
db:
  url: postgresql+psycopg://postgres:postgres@localhost:5432/splice  # same DB
```

## Process management with systemd

Run **one unit per device**. Use a templated name so each device has an independently controllable service.

```ini
# /etc/systemd/system/splice-roadm_a.service
[Unit]
Description=Splice gNMI bridge (roadm_a)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/splice
# Credentials via environment file (see "Credentials & secrets" below)
EnvironmentFile=/opt/splice/devices/roadm_a/env
ExecStart=/opt/splice/.venv/bin/splice \
  --config devices/roadm_a/config.yaml run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

If you have not run `pip install -e .` (no `splice` entry point on PATH), invoke the module directly:

```ini
Environment=PYTHONPATH=src
ExecStart=/opt/splice/.venv/bin/python \
  src/splice/cli.py --config devices/roadm_a/config.yaml run
```

Create one unit per device, then enable and start them:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now splice-roadm_a
sudo systemctl enable --now splice-roadm_b
```

Follow logs with `journalctl -u splice-roadm_a -f`.

!!! tip "Run setup before run"
    `run` serves whatever is already in the database. Run the discovery pipeline first — `splice --config devices/roadm_a/config.yaml setup` — either manually or as a `ExecStartPre=` step / one-shot unit, before the long-running `run` service.

## Docker & docker-compose

The repo ships a minimal image and a full observability stack under `docs/`.

### Dockerfile

`docs/Dockerfile` builds a `python:3.12-slim` image that installs `requirements.txt`, copies `src`, sets `PYTHONPATH=src`, and uses `python src/splice/cli.py` as its entrypoint:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src src
ENV PYTHONPATH=src
ENTRYPOINT ["python", "src/splice/cli.py"]
```

Because the entrypoint is the CLI, container arguments are CLI arguments:

```bash
docker run --rm -v "$PWD/devices:/app/devices" splice \
  --config devices/roadm_a/config.yaml run
```

### Compose stack

`docs/docker-compose.yml` brings up the bridge plus the surrounding telemetry stack — PostgreSQL (catalog/jobs), InfluxDB + Telegraf (TSDB ingest), and Grafana (dashboards):

| Service    | Image                | Port  | Role                                   |
|------------|----------------------|-------|----------------------------------------|
| `postgres` | `postgres:15`        | 5432  | Catalog, jobs, list instances, history |
| `bridge`   | built from `docs/Dockerfile` | 9339  | The Splice gNMI server          |
| `influxdb` | `influxdb:2.7`       | 8086  | Time-series storage                    |
| `grafana`  | `grafana/grafana:10.3.1` | 3000 | Dashboards                          |
| `telegraf` | `telegraf:1.30`      | —     | Scrapes/forwards telemetry to InfluxDB |

The `bridge` service builds with the **repo root as build context** (`build.context: ..`, `dockerfile: docs/Dockerfile`), so the stack works without copying the Dockerfile elsewhere:

```bash
docker compose -f docs/docker-compose.yml up -d
```

Edit the `bridge` service `command` to point at your device config:

```yaml
command: ["--config", "devices/roadm_a/config.yaml", "run"]
```

The compose file mounts `../devices` and `../logs` into the container, so device configs, passwords, and YANG live on the host.

!!! warning "Replace the sample credentials"
    The bundled compose file uses development passwords (`POSTGRES_PASSWORD: postgres`, InfluxDB `adminadmin`). Change every credential before exposing this stack beyond a local machine.

### One bridge service per device

To serve multiple devices under compose, add one `bridge_<name>` service per device — each with its own `command` (config path) and its own published gNMI port (`9340:9340`, `9341:9341`, …) matching that device's `gnmi.port`.

## Credentials & secrets

Never commit NETCONF passwords. Splice reads the device password from, in order of preference:

- **`NETCONF_PASSWORD` environment variable** — convenient for systemd `EnvironmentFile=` or container `environment:`.
- **`device.password_file`** — a path to a file containing the password (e.g. `devices/<device>/password`), kept out of git and `chmod 600`.

```bash
# Interactive / shell
export NETCONF_PASSWORD='...'
splice --config devices/roadm_a/config.yaml run
```

```ini
# /opt/splice/devices/roadm_a/env  (referenced by EnvironmentFile= in the unit)
NETCONF_PASSWORD=...
```

!!! warning "ADVA / ConfD lockouts"
    Devices such as ADVA Teraflex/Quadflex lock out after repeated auth failures. Splice's auth-failure detection opens the circuit breaker immediately with a long cooldown to avoid hammering a device into lockout — but a wrong password in a restart-looping service can still trip it. Verify credentials with `splice ... doctor` before enabling `Restart=on-failure`.

Other secret-bearing settings to protect:

- **`trigger_api`** (on-demand polling): disabled by default. If you enable it, restrict the port (default `9812`) by firewall and set a strong `trigger_api.bearer_token`.
- **`db.url`**: contains the PostgreSQL password; keep config files out of public repos.

## Health endpoint & monitoring

When `health.enabled` is true (**the default**), each `run` process serves an HTTP `/health` endpoint alongside the gNMI server.

| Key             | Default     |
|-----------------|-------------|
| `health.enabled`| `true`      |
| `health.bind`   | `0.0.0.0`   |
| `health.port`   | `9809`      |

The handler responds only to `GET /health`:

- **HTTP 200** when healthy, **HTTP 503** when degraded (both carry a JSON body).
- The JSON payload includes `db` (a `SELECT 1` probe), `netconf` (true unless the circuit breaker is open), `gnmi`, a `netconf_status` object with `circuit_breaker_open`, and live poller stats under `poller` (`task_count`, `disabled_count`, `disabled`, `failure_limit`, and per-sweep `sweeps`). When PM polling is active a `pm_poller` block is added.

```bash
curl -s http://192.0.2.10:9809/health | jq .
```

The `db` and `netconf` checks are **passive** — the DB probe is a lightweight query and the NETCONF check reads cached pool status (no network call), so polling `/health` frequently is cheap and won't generate device RPCs.

The `status` command queries this same endpoint and prints a human summary (or `--json` for the raw payload):

```bash
splice --config devices/roadm_a/config.yaml status
splice --config devices/roadm_a/config.yaml status --json
```

!!! note "Loopback probe semantics"
    When `health.bind` is `0.0.0.0`, `status` probes `127.0.0.1`; when it is `::` it probes `::1`; otherwise it probes the configured bind address.

### Wiring into a monitor

Point any HTTP health checker at `/health` and treat non-200 as down:

```ini
# systemd or external monitor — any tool that understands 200 vs 503
GET http://127.0.0.1:9809/health   # 200 OK = healthy, 503 = degraded
```

For per-device dashboards, give each device a distinct `health.port` (mirroring its distinct `gnmi.port`).

## Performance & throughput tuning

Polling load on the device is the main scaling constraint. Tune it through the `poller`, `gnmi`, and `discovery` sections (see [Configuration](configuration.md)).

### Polling interval

- Keep `poller.default_interval_sec` conservative (10–30s).
- Reserve high-frequency intervals (via `poller.rules`) for the few paths that truly need them.
- Tasks that fail `poller.task_failure_limit` times in a row are auto-dropped from rotation (surfaced in `/health` under `poller.disabled`).

### Batching

- `batch_mode: top` — fewer RPCs per tick.
- `batch_mode: module` — smaller payloads across large models.

### List expansion

`gnmi.expansion_mode` defaults to `direct` (cached list instances — fast, safe startup):

| Mode     | Behaviour                                            |
|----------|------------------------------------------------------|
| `direct` | Use cached instances only (default)                  |
| `full`   | Fresh discovery on every `run` start (can be slow)   |
| `lazy`   | Like `direct`, but expand on first gNMI request      |

Guardrails `gnmi.max_expanded_paths_per_base` (default `10000`) and `gnmi.max_total_expanded_paths` (default `100000`) cap runaway expansion.

### PM fan-out

PM discovery can generate many per-instance filters. Cap and pin it:

- `discovery.pm_fanout_limit` — cap per-root instance expansion.
- `discovery.pm_selector` — pin PM collection to a known instance/interface.

### Connection resilience

These knobs protect the device from rapid-fire RPCs and lockouts:

- **Reconnect-once** on RPC failure (no retry loops), governed by `reconnect_delay_sec`.
- **Circuit breaker** opens after N consecutive double-failures and refuses new RPCs for a cooldown.
- **Poller throttle** inserts a delay between tasks.

## TLS termination

The gNMI server runs **without TLS**. Terminate TLS upstream and restrict the plaintext gNMI port to localhost / a trusted network.

### Option A — nginx (gRPC proxy)

```nginx
server {
  listen 8443 http2;
  server_name gnmi.example.com;

  ssl_certificate     /etc/ssl/certs/gnmi.crt;
  ssl_certificate_key /etc/ssl/private/gnmi.key;

  location / {
    grpc_pass grpcs://127.0.0.1:9339;
  }
}
```

### Option B — stunnel (TCP TLS wrapper)

```ini
[gnmi]
accept = 8443
connect = 127.0.0.1:9339
cert = /etc/ssl/certs/gnmi.pem
```

## Security checklist

- Store NETCONF passwords in `device.password_file` (or `NETCONF_PASSWORD`); keep them out of git.
- Firewall the gNMI ports (`9339`, `9340`, …); the transport is insecure — terminate TLS upstream.
- If enabling `trigger_api`, protect its port (default `9812`) and set a strong `trigger_api.bearer_token`.
- Replace all sample passwords in `docs/docker-compose.yml` before any non-local use.

## Logging

- When running via shell, redirect stdout/stderr into `logs/`; under systemd use `journalctl`.
- Set `PYTHONUNBUFFERED=1` when piping through `tee` so `print()` output appears in real time.
- Use log rotation for long-running services.

## Optional Kafka deployment

If Kafka publishing is enabled (off by default):

1. Install the extra: `pip install -e .[kafka]`.
2. Set `kafka.enabled: true` and `kafka.brokers`.
3. Ensure the bridge host/container can reach the Kafka brokers.
4. Capacity-plan the topics:
    - `<topic_prefix>.<device>.poll`
    - `<topic_prefix>.<device>.trigger`
