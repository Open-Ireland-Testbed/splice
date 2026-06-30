<div align="center">

# 🔌 Splice

**Point at a NETCONF device's IP, get a working gNMI server.**

*Vendor-agnostic telemetry for optical and network gear that never spoke gRPC.*

[![CI](https://github.com/Open-Ireland-Testbed/splice/actions/workflows/ci.yml/badge.svg)](https://github.com/Open-Ireland-Testbed/splice/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10+-3776ab?logo=python&logoColor=white)](https://www.python.org)
[![gNMI](https://img.shields.io/badge/gNMI-gRPC-0a7e8c?logo=grpc&logoColor=white)](https://github.com/openconfig/gnmi)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169e1?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![License](https://img.shields.io/badge/License-AGPL_v3_%2F_Commercial-f46800)](#licensing)

</div>

> **Dual-licensed.** Splice is available under the **GNU AGPL v3.0-or-later** (free, copyleft — see [`LICENSE`](LICENSE)) **or** a **commercial licence** for proprietary / closed-source use (see [`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md), contact `infoknex@tcd.ie`). [Details ↓](#licensing)

---

## The Problem

Modern telemetry collectors — gnmic, Telegraf, OpenConfig pipelines — expect **gNMI** over gRPC. But a huge amount of deployed optical and transport gear only speaks **NETCONF**: ROADMs, transponders, amplifiers, line systems. Each vendor ships its own YANG models, its own list keys, its own quirks. Bolting a collector onto them usually means writing per-device glue and re-writing it every firmware bump.

**This bridge fixes that.** Give it an IP and credentials; it discovers the device's YANG models, builds a telemetry catalog, polls NETCONF under the hood, and serves standard gNMI to any client — no per-vendor code, no changes to the device.

<div align="center">

![Multi-vendor optical power in Grafana](docs/images/grafana_optical_power.png)
*Three different vendors — Lumentum ROADM, Adtran Teraflex, Juniper ILA — streaming optical power into one Grafana dashboard through the bridge.*

</div>

## How It Works

```mermaid
graph LR
    D["NETCONF device<br/>:830 (SSH)"] -->|YANG discovery| C[Catalog]
    C --> P[Poller]
    P -->|NETCONF get| D
    P --> M[(TTL cache)]
    M --> G["gNMI server<br/>:9339 (gRPC)"]
    G -->|gNMI Get / Subscribe| K["gnmic · Telegraf<br/>AI agents"]
    DB[(PostgreSQL)] -.-> C
```

Discovery walks the device's YANG library into module-qualified catalog paths, the planner batches them into efficient NETCONF reads, and a resilient poller fills a TTL cache that the gRPC server answers from. Performance-monitoring data is published under synthetic, instance-keyed paths so per-port metrics never collide.

## ✨ Key Features

| | Feature | Description |
|---|---|---|
| 🧭 | **Zero-touch onboarding** | `quickstart --host <ip>` discovers YANG, builds the catalog, and serves gNMI in one command |
| 🏷️ | **Vendor-agnostic** | Works across any NETCONF/YANG device — proven live on Lumentum, Adtran, and Juniper gear |
| 🔑 | **Instance-keyed telemetry** | PM data carries its originating port as gNMI keys, so multi-line metrics stay distinct |
| 🔁 | **Drift self-healing** | Re-resolves list instances at startup and rebuilds filters when a device is re-provisioned |
| 🩺 | **Lockout-safe resilience** | Reconnect-once, circuit breaker, and auth/session-limit detection protect fragile devices |
| 🎯 | **Curated polling** | Poll only the metrics you care about with include patterns and per-pattern intervals |
| 📊 | **Grafana dashboards out of the box** | Setup generates per-device live-telemetry and YANG-inventory dashboards from the discovered catalog |
| 🔎 | **Hybrid path search** | Find paths in huge YANG catalogs by keyword (BM25, no extra deps) blended with semantic embeddings when installed |
| 🧪 | **Hardware-free simulation** | `--simulate openroadm` runs the full pipeline against a simulated device |
| 🤖 | **AI-agent ready** | Built-in MCP server exposes the bridge as tools for Claude and other agents |

Resilience is the load-bearing design choice: optical devices frequently limit concurrent NETCONF sessions and lock accounts on repeated auth failures, so every RPC path is built to back off rather than hammer.

## Quick Start

### 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .            # core CLI + gNMI server
pip install -e ".[tui]"     # optional: interactive console
pip install -e ".[mcp]"     # optional: AI-agent interface
```

### 2. Try it with no hardware

```bash
splice --simulate openroadm run
```

In another terminal, it answers gNMI like any real server:

```bash
gnmic -a localhost:9339 --insecure capabilities
# gNMI version: 0.8.0
# supported models:
#   - org-openroadm-device, 2023-05-26  ...
```

Or read live values straight from the simulated device via the Python API:

```python
from splice.api.device import Device

with Device("sim://openroadm") as dev:
    print(dev.get("/org-openroadm-device/node-id"))
    # [{'path': '/org-openroadm-device/node-id', 'value': 'ROADM-SIM-01'}]
```

### 3. Point it at a real device

```bash
NETCONF_PASSWORD='...' splice quickstart --host 10.0.0.1 --username admin
```

This generates a per-device config, runs full discovery, writes Grafana dashboards for the device, and starts the gNMI server. That's the whole promise — an IP in, a gNMI endpoint out.

## Four Ways to Use It

| Interface | Entry point | Best for |
|---|---|---|
| **CLI + PostgreSQL** | `splice --config <cfg> setup \| run` | Production: persistent catalog, multi-device, history |
| **Python API** | `Device("sim://openroadm")` | Scripting and notebooks — no database or config files |
| **MCP server** | `splice mcp` | AI agents — connect, query, and set config as tools |
| **Interactive TUI** | `splice-tui --devices-dir devices` | Operators — browse paths, watch live telemetry, run setup |

<div align="center">

![Interactive TUI status dashboard](docs/images/tui_status.png)
*The Textual console managing a live Adtran Teraflex — health cards, circuit-breaker state, and catalog counts at a glance.*

</div>

## Configuration

YAML-based, one file per device. Credentials come from the `NETCONF_PASSWORD` env var or a `password_file` — never committed.

```yaml
device:
  host: 10.0.0.1
  username: admin
  password_file: devices/my_device/password
gnmi:
  port: 9339
  expansion_mode: direct      # use cached list instances (safe default)
poller:
  default_interval_sec: 30
  task_failure_limit: 5       # drop a path after N consecutive failures
metrics:                      # optional: poll only what you care about
  enabled: true
  include:
    - { pattern: "opt-rcv-pwr|fec-ber", regex: true, interval_sec: 5 }
```

See **[docs/configuration.md](docs/configuration.md)** for the full reference.

## Documentation

| Document | Description |
|---|---|
| **[Quickstart](docs/quickstart.md)** | Step-by-step first device |
| **[Configuration](docs/configuration.md)** | Every config section, with examples |
| **[Python API](docs/python-api.md)** | The `Device` class, no DB required |
| **[TUI Guide](docs/tui.md)** | Interactive console walkthrough |
| **[Troubleshooting](docs/troubleshooting.md)** | Common failure modes and fixes |
| **[CLAUDE.md](CLAUDE.md)** | Architecture overview for contributors and agents |

## Development

```bash
git clone https://github.com/Open-Ireland-Testbed/splice.git
cd splice
python -m venv .venv && source .venv/bin/activate
pip install -e ".[tui,mcp,search]"

python -m pytest                                    # ~640 tests, no DB needed
TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/splice' \
  python -m pytest                                  # include PostgreSQL-backed tests
```

CI runs the migration-safety, no-database, and full PostgreSQL 16 suites on every push.

## Licensing

Splice is **dual-licensed**:

1. **GNU AGPL v3.0-or-later** (see [`LICENSE`](LICENSE)) — free for open-source
   and AGPL-compliant use. If you modify Splice or run it as a network service,
   the AGPL requires you to make your corresponding source available to its
   users.
2. **Commercial licence** — for proprietary, embedded, or closed-source use
   without the AGPL's copyleft obligations. See
   [`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md) for a summary.

If you use Splice in a closed-source or commercial product, you must obtain a
commercial licence. To request one, contact Trinity College Dublin (Trinity
Innovation & Enterprise) at **infoknex@tcd.ie**.

Copyright (c) 2026 Trinity College Dublin. Splice bundles and depends on
third-party open-source components (gRPC, ncclient, SQLAlchemy, pyang, paramiko,
the OpenConfig gNMI definitions, and others) under their own permissive or LGPL
licences; none of those impose copyleft on Splice or constrain this dual-license
choice.

---

<div align="center">

*Built for the [Open Ireland](https://openireland.eu) optical testbed — so any NETCONF device can join a modern telemetry pipeline.*

</div>
