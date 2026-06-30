# Observability

Splice serves telemetry over gNMI, but most operators want that data in a time-series database with dashboards on top. This page covers the full path from device to chart: shipping gNMI telemetry into InfluxDB with Telegraf, generating Grafana dashboards from a device's YANG catalog, watching the bridge's own `/health` endpoint, and optionally fanning poll/trigger events out to Kafka.

For the end-to-end stack (containers, ports, volumes) see [Deployment](deployment.md). For the config keys referenced here, see [Configuration](configuration.md).

## The telemetry pipeline

The reference observability stack is four moving parts:

```
NETCONF device ──► Splice (gNMI :9339) ──► Telegraf ──► InfluxDB ──► Grafana
```

1. **Splice** polls the device over NETCONF and serves the results over gNMI/gRPC.
2. **Telegraf** subscribes to the gNMI server and writes points to InfluxDB.
3. **InfluxDB** stores the time series.
4. **Grafana** queries InfluxDB (via Flux) using dashboards Splice generates from the YANG catalog.

A ready-to-run Compose file for this stack lives at [`docs/docker-compose.yml`](https://github.com/Open-Ireland-Testbed/splice/blob/main/docs/docker-compose.yml) and the matching Telegraf config at [`docs/telegraf.conf`](https://github.com/Open-Ireland-Testbed/splice/blob/main/docs/telegraf.conf).

## Telegraf: gNMI → InfluxDB

Telegraf's `inputs.gnmi` plugin subscribes to the Splice gNMI server and its `outputs.influxdb_v2` plugin writes to InfluxDB. The shipped [`docs/telegraf.conf`](https://github.com/Open-Ireland-Testbed/splice/blob/main/docs/telegraf.conf) is the canonical example:

```toml
[agent]
  interval = "10s"
  flush_interval = "10s"

[[outputs.influxdb_v2]]
  urls = ["http://influxdb:8086"]
  token = "adminadmin"
  organization = "gnmi"
  bucket = "gnmi"

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

A few things to know about this configuration:

- **`paths = ["/"]`** subscribes to the entire served tree. Narrow it to specific subtrees (for example `"/components"`) to reduce write volume on large devices.
- **`insecure = true`** matches Splice's default plaintext gRPC. If you put TLS in front of the gNMI port (see [Deployment](deployment.md)), point Telegraf at the TLS endpoint instead.
- **`encoding = "json_ietf"`** is the encoding Splice serves.
- The hostnames `bridge` and `influxdb` are Docker Compose service names; replace them with reachable host/IP addresses outside Compose.

!!! warning "The `device_host` tag is load-bearing"
    Generated Grafana dashboards filter every query by a `device_host` tag whose value is the device's `host` from its `config.yaml`. Telegraf tags each gNMI series with the source address it subscribed to, which is what populates `device_host`. If your Telegraf setup tags series differently, the dashboard's device filter will not match and panels will read empty. Confirm the tag name and value in InfluxDB before assuming a dashboard is broken.

The bucket (`gnmi`) and measurement that Telegraf writes under are what the dashboard generator references — keep them aligned with the `--bucket` / `--measurement` flags described below.

## Grafana dashboard generation

Splice generates **two dashboards per device**, derived from that device's parsed YANG catalog — no hand-authoring of panels required:

| File | Contents |
|------|----------|
| `<slug>-live.json` | Live telemetry panels grouped into metric *families* (power, temperature, optical quality, traffic, errors, frequency/timing, …) inferred from YANG path keywords. Auto-refreshes every 10s. |
| `<slug>-inventory.json` | A schema inventory: text panels summarizing modules, top-level subtrees, candidate operational metrics, and per-module path tables. |

`<slug>` is a filesystem-safe form of the device name (lowercased, non-alphanumerics replaced with `-`).

### Running it

```bash
# Standalone command
splice --config devices/<name>/config.yaml dashboards

# With custom output dir / InfluxDB bucket / measurement
splice --config devices/<name>/config.yaml dashboards \
  --out dashboards \
  --bucket gnmi \
  --measurement telemetry
```

Flag defaults (from the CLI):

| Flag | Default | Meaning |
|------|---------|---------|
| `--out` | `dashboards` | Output directory (created if missing) |
| `--bucket` | `gnmi` | InfluxDB bucket referenced by the Flux queries |
| `--measurement` | `telemetry` | InfluxDB measurement referenced by the queries |

The command prints the path of each written file:

```text
dashboards/myrouter-live.json
dashboards/myrouter-inventory.json
```

Generation **also runs automatically as setup step 8** (`[8/8] Generating Grafana dashboards...`). That step is best-effort: a failure there does not fail `setup`, and the CLI prints a retry hint (`Retry later with: splice --config <config> dashboards`).

!!! note "Generation needs YANG files, not a database"
    The generator reads the device's YANG directory directly (resolved from `yang_dir` in the config, or `./yang`) and parses the catalog in-process. It deliberately does **not** call the full config loader, so it works even when `device.password_file` is absent. If no YANG files are found, the live dashboard still renders an overview panel and the inventory dashboard explains that discovery/setup must run first.

### How the live dashboard is built

The generator walks the catalog and keeps entries that look like numeric telemetry: non-config `leaf`/`leaf-list` nodes whose YANG type resolves to a numeric kind (int, uint, decimal, counter, gauge, power, ratio, etc.), excluding identity/config leaves (`name`, `description`, `enabled`, `admin`, …). Those candidates are bucketed into metric families by substring-matching keywords against the path:

| Family | Example keywords |
|--------|------------------|
| Power, Gain, and Loss | `power`, `gain`, `loss`, `atten`, `voa` |
| Temperature | `temp`, `temperature`, `thermal` |
| Current, Voltage, and Bias | `current`, `volt`, `bias` |
| Optical Quality | `ber`, `fec`, `osnr`, `snr`, `q-factor`, `quality` |
| Traffic and Utilization | `octet`, `packet`, `bytes`, `util`, `throughput`, `rate` |
| Errors and Discards | `error`, `discard`, `drop` |
| Frequency and Timing | `freq`, `wavelength`, `offset`, `delay`, `latency`, `dgd`, `dispersion` |

Each detected family gets a stat summary, a time-series trend panel, a bar-gauge of latest values, and a detail table. Queries are Flux, scoped to the device via `device_host` and the bucket/measurement.

!!! tip "Dashboards are a starting point"
    Metric-family detection is heuristic keyword matching on YANG paths, so panels may include near-misses or omit a metric whose name doesn't contain a known keyword. Treat the generated dashboards as a curated baseline and adjust panels per device.

### Portable datasource

Dashboards reference InfluxDB through a **`datasource` template variable** (`${datasource}`) rather than a hardcoded datasource UID. That means a generated JSON file imports cleanly into any Grafana instance: on import, Grafana prompts you to pick the InfluxDB datasource and binds the variable. To import, use Grafana's **Dashboards → New → Import** and upload the JSON (or drop the files into a provisioning directory).

!!! note "Large devices are capped"
    The inventory dashboard caps per-module path tables (240 rows per module, paged into chunks) so the JSON stays within Grafana's import limits. When a module is truncated, the panel says so and points you at `splice export --what catalog` for the complete list.

## The `/health` endpoint

When `health.enabled` is true (the **default**), `splice run` serves an HTTP health endpoint alongside the gNMI server. It is the data source behind `splice status` and the TUI Status tab.

`HealthConfig` defaults:

| Key | Default | Meaning |
|-----|---------|---------|
| `health.enabled` | `true` | Serve the `/health` endpoint |
| `health.bind` | `0.0.0.0` | Bind address |
| `health.port` | `9809` | HTTP port |

The endpoint responds only to `GET /health` (any other path returns 404). It returns **HTTP 200** when healthy and **HTTP 503** when degraded — the JSON body is present in both cases, so a 503 still carries detail.

```bash
curl -s http://192.0.2.10:9809/health | jq
```

The JSON body includes:

- `db`, `netconf`, `gnmi` — boolean health flags. `db` is a live `SELECT 1`; `netconf` reflects whether the circuit breaker is open (a passive check, no network calls); `gnmi` indicates the server is up.
- `netconf_status` — the connection pool status, including `circuit_breaker_open`.
- `poller` (and `pm_poller`, when PM polling is on) — sweep statistics: `task_count`, `disabled_count`, `failure_limit`, a `sweeps` map keyed by interval (each with `ok` / `fail` / `disabled` counts, `duration_sec`, and `completed_at`), and a `disabled` list of `{path, error}` for tasks dropped from rotation.

The friendlier way to read the same data is:

```bash
splice --config devices/<name>/config.yaml status
splice --config devices/<name>/config.yaml status --json   # raw payload
```

`status` probes the loopback address of the configured bind (mapping `0.0.0.0` → `127.0.0.1`, `::` → `::1`) and falls back to reporting database freshness if the endpoint is unreachable. Example human-readable output:

```text
source: http://127.0.0.1:9809/health
  db: OK
  netconf: OK
  gnmi: OK
  circuit_breaker_open: False
  poller: tasks=128 disabled=0 failure_limit=5
    every 30s: 126 ok / 2 fail / 0 skipped, sweep took 4.2s (3s ago)
```

!!! tip "Wire it into orchestration"
    Because `/health` returns 200/503, it slots straight into a container `HEALTHCHECK`, a Kubernetes liveness/readiness probe, or a load-balancer health check. Use 503 to mean "degraded but reporting" — the body still tells you *why* (DB down vs. circuit breaker open).

## Optional Kafka publishing

Splice can publish telemetry as a side-channel to Kafka in addition to (not instead of) serving gNMI. This is **off by default** and requires the `confluent-kafka` package — `kafka.enabled: true` raises a clear error if it isn't installed (`pip install -e .[kafka]`).

`KafkaConfig` keys and defaults:

| Key | Default | Meaning |
|-----|---------|---------|
| `kafka.enabled` | `false` | Master switch |
| `kafka.brokers` | `""` | Bootstrap servers (`host:port[,host:port]`) |
| `kafka.client_id` | `splice` | Producer `client.id` |
| `kafka.topic_prefix` | `splice` | Prefix for generated topic names |
| `kafka.publish_poll_updates` | `true` | Publish poller sweep updates |
| `kafka.publish_trigger_events` | `true` | Publish on-demand trigger updates |
| `kafka.acks` | `all` | Producer acknowledgement level |
| `kafka.compression` | `lz4` | Compression codec (set null to disable) |
| `kafka.linger_ms` | `20` | Producer batching linger |

### Topic naming

Topics are derived from the prefix, the device name, and the event type:

```
<topic_prefix>.<device>.poll      # poller sweep updates
<topic_prefix>.<device>.trigger   # on-demand trigger updates
```

With defaults and a device named `myrouter`, that's `splice.myrouter.poll` and `splice.myrouter.trigger`. The prefix and device name are sanitized (non-`[a-zA-Z0-9_.-]` characters collapse to `_`).

### Payload shape

Each message is a JSON object. A poll update looks like:

```json
{
  "event_type": "poll_update",
  "source": "poller",
  "device": "myrouter",
  "path": "/components/component[name=osc1]/state/temperature",
  "value": 41.2,
  "timestamp_ns": 1730000000000000000,
  "event_time_unix_ns": 1730000000123456789,
  "meta": null
}
```

Trigger updates use `event_type: "trigger_update"`, `source: "trigger"`, and add a `request_id` field tying the values back to the originating `POST /trigger` call (see the trigger API in [Configuration](configuration.md)).

!!! note "Best-effort by design"
    The publisher is best-effort: produce failures are logged and swallowed so a Kafka outage never blocks polling or stalls the gNMI server. Delivery uses `acks=all` with batching (`linger_ms=20`) and an `lz4`-compressed producer by default. Configure the brokers and confirm the bridge host can reach them — see the Kafka section of [Deployment](deployment.md).

## See also

- [Deployment](deployment.md) — the full Compose stack, TLS termination, systemd, and Kafka deployment notes.
- [Configuration](configuration.md) — every `health`, `kafka`, `poller`, and `trigger` config key.
- [CLI Reference](cli.md) — `dashboards`, `status`, and the rest of the command surface.
