# Deployment Notes

This document covers practical deployment choices and recommended defaults.

## One device per server
- Run one gNMI server process per device.
- Each device uses its own config file and gNMI port.
- Keep a dedicated `yang_dir` per device to avoid cross‑vendor contamination.

## Service layout

Recommended layout:

```
splice/
  devices/
    <device_name>/
      config.yaml
      password
      yang/
  logs/
```

## Process management

For systemd, run one unit per device:

```
[Unit]
Description=splice <device_name>
After=network.target

[Service]
WorkingDirectory=/opt/splice
Environment=PYTHONPATH=src
ExecStart=/opt/splice/.venv/bin/python \
  src/splice/cli.py --config devices/<device_name>/config.yaml run
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### systemd (multiple devices)
Create one unit file per device and change the port in each config:

```
/etc/systemd/system/splice-roadm_10.service
/etc/systemd/system/splice-teraflex.service
```

Example unit file:

```
[Unit]
Description=splice roadm_10
After=network.target

[Service]
WorkingDirectory=/opt/splice
Environment=PYTHONPATH=src
ExecStart=/opt/splice/.venv/bin/python \
  src/splice/cli.py --config devices/roadm_10/config.yaml run
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now splice-roadm_10
sudo systemctl enable --now splice-teraflex
```

## Docker Compose

Sample compose file lives at `docs/docker-compose.yml`.

```bash
docker compose -f docs/docker-compose.yml up -d
```

Adjust service count or add one service per device (each with its own config and port).

Note: `docs/docker-compose.yml` builds from `docs/Dockerfile` with the repo root as the build context (`build.context: ..`), so run it from anywhere without copying the Dockerfile.

## TLS termination

The gNMI server runs without TLS. Terminate TLS upstream.

### Option A: nginx (gRPC proxy)

```
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

### Option B: stunnel (TCP TLS wrapper)

```
[gnmi]
accept = 8443
connect = 127.0.0.1:9339
cert = /etc/ssl/certs/gnmi.pem
```

## Security
- Store NETCONF passwords in `devices/<device>/password` and keep them out of git.
- Use firewalls to restrict gNMI ports.
- gNMI server currently uses insecure transport; terminate TLS upstream if needed.
- If enabling `trigger_api`, protect port `9812` (or your chosen port) and set a strong bearer token.

## Performance tuning

### Polling interval
- Keep `poller.default_interval_sec` conservative (10–30s).
- Use `poller.rules` for high‑frequency paths only.

### Batching
- `batch_mode: top` for fewer RPCs per tick.
- `batch_mode: module` to reduce payload size across large models.

### List expansion
- `gnmi.expansion_mode: direct` if startup must be instant.
- `gnmi.expansion_mode: lazy` if you want auto‑expansion on first request.

### PM fan‑out
- `discovery.pm_fanout` can generate many filters.
- Use `pm_fanout_limit` to cap per‑root instance expansion.
- Use `pm_selector` to pin PM to a known interface.

## Logging
- Send stdout/stderr to `logs/` if running via shell.
- Use log rotation for long‑running services.

## Optional Kafka deployment

If Kafka publishing is enabled:

1. Install with extras: `pip install -e .[kafka]`
2. Set `kafka.enabled: true` and `kafka.brokers`.
3. Ensure bridge host/container can reach Kafka brokers.
4. Capacity plan topics:
   - `<topic_prefix>.<device>.poll`
   - `<topic_prefix>.<device>.trigger`
