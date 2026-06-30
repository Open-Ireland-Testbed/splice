# Demo Cheat Sheet

## 3-Screen Setup
| Screen | What | Command |
|--------|------|---------|
| **Big Monitor** | Grafana | Browser: `http://100.73.255.83:3000` (admin/admin) |
| **MacBook** | Live terminal | Interactive demo commands below |
| **iPad** | Asciinema loop | `while true; do asciinema play demo/recording.cast; sleep 3; done` |

## Before Demo (5 min before)
```bash
cd ~/splice

# Verify bridges running
ss -tlnp | grep -E '934[012]'    # should show 3 ports

# If not running:
.venv/bin/splice --config devices/roadm_17/config.yaml run > demo/roadm.log 2>&1 &
.venv/bin/splice --config devices/teraflex/config.yaml run > demo/teraflex.log 2>&1 &
.venv/bin/splice --config devices/juniper_ila/config.yaml run > demo/juniper.log 2>&1 &

# Verify Grafana
curl -s localhost:3000/api/health

# Verify Docker stack
docker compose -f demo/docker-compose.yml ps
```

## Demo Flow — 7 Acts (~16 min)

### Act 1: The Problem
```bash
gnmic -a 192.0.2.17:830 --insecure capabilities
# → Fails. Device speaks NETCONF, not gRPC.
```

### Act 2: The Solution (ROADM)
```bash
splice --config devices/roadm_17/config.yaml setup    # ~2 seconds
gnmic -a localhost:9341 --insecure capabilities              # → Works!
gnmic -a localhost:9341 --insecure get --path /              # → Live data
```

### Act 3: Three Vendors
```bash
# All 3 already running
gnmic -a localhost:9341 --insecure get --path /    # ROADM
gnmic -a localhost:9340 --insecure get --path /    # Teraflex
gnmic -a localhost:9342 --insecure get --path /    # Juniper
```

### Act 4: Semantic Search ⭐
```bash
splice --config devices/teraflex/config.yaml find-paths \
  --semantic --pattern "Q-factor optical signal quality" --top 5

splice --config devices/teraflex/config.yaml find-paths \
  --semantic --pattern "FEC bit error rate" --top 5
```

### Act 5: Live PM Data ⭐
```bash
gnmic -a localhost:9340 --insecure subscribe --mode once --path '/pm'
# → Q-factor=13.8, OSNR=34.8, FEC-BER=3.5e-7
# Point to Grafana on big monitor!
```

### Act 6: Four Interfaces
```bash
# Python API
.venv/bin/python3 -c "from splice.api.device import Device; d = Device('sim://openroadm'); d.connect(); print(f'{len(d.catalog_paths)} paths')"

# TUI
splice-tui --devices-dir devices
# Select device → Press 2 for Paths → Press / for semantic search

# MCP (Claude)
claude -p "Hello Claude! I have Splice running for our Teraflex. Can you check the current Q-factor? Device: 192.0.2.92, port 830, username admin, password in devices/<device>/password."
```

### Act 7: Summary
- 3 vendors, 3 device types
- 19,000+ telemetry paths
- 504 tests, 4 interfaces
- Zero vendor-specific code

## Topology
```
Teraflex Tx → ROADM MUX (port 1) → Booster → 25km fiber → ILA → fiber → Preamp → DEMUX (port 1) → Teraflex Rx
```

## Key Numbers
| Metric | Value |
|--------|-------|
| ROADM paths | 581 |
| Teraflex paths | 13,281 |
| Juniper ILA paths | 566 |
| Q-factor | 13.8 dB |
| OSNR | 34.8 dB |
| FEC-BER | ~3.5e-7 |
| CD | -1100 ps/nm |
| Rx Power | -5.8 dBm |

## Emergency Fixes
```bash
# Bridge crashed:
.venv/bin/splice --config devices/<device>/config.yaml run > demo/<device>.log 2>&1 &

# Grafana down:
docker compose -f demo/docker-compose.yml restart grafana

# Grafana no data:
docker compose -f demo/docker-compose.yml restart    # restart everything

# Kill everything:
./demo/teardown.sh
docker compose -f demo/docker-compose.yml down
```
