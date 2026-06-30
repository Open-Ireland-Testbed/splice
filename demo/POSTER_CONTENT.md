# Splice — Poster Content

## Title
**Splice: Vendor-Agnostic Telemetry for Optical Networks with AI-Powered Path Discovery**

## Authors
Agastya Raj, Trinity College Dublin (TCD), Open Ireland Testbed

## One-Line Summary
A vendor-agnostic gNMI server that bridges NETCONF optical devices to modern telemetry ecosystems, with semantic search over 18,000+ YANG paths using OpenAI embeddings.

## Problem
- Optical network devices (ROADMs, transponders, amplifiers) from different vendors speak **NETCONF** (SSH, port 830)
- Modern telemetry collectors and orchestrators expect **gNMI** (gRPC)
- Each vendor has hundreds of YANG modules with thousands of unique paths
- No standard way to discover and query performance monitoring data across vendors
- Manual path lookup in 18,000+ YANG paths is impractical

## Solution
Splice automatically:
1. **Discovers** YANG models from any NETCONF device
2. **Builds** a telemetry catalog from the YANG schema
3. **Serves** standard gNMI to any client (gnmic, Telegraf, custom)
4. **Searches** paths semantically using OpenAI embeddings

## Architecture
```
NETCONF Device (port 830)
    ↓ SSH/NETCONF
NetconfClient → NetconfPoller → parse_xml_to_kv
    ↓
MemoryCache → GNMIServicer (gRPC, port 9339+)
    ↓
gNMI Client (gnmic, Telegraf, Grafana)
```

### Four Parallel Interfaces
| Interface | Use Case | DB Required |
|-----------|----------|-------------|
| CLI | Operators, automation | Yes (PostgreSQL) |
| Python API | Scripts, notebooks | No |
| TUI | Interactive management | Yes |
| MCP Server | AI agents (Claude) | No |

## Testbed Deployment

### Devices
| Device | Vendor | Role | YANG Modules | Telemetry Paths |
|--------|--------|------|-------------|-----------------|
| ROADM | Lumentum | Optical switching | 43 | 581 |
| Teraflex | Adtran (ADVA) | Coherent transponder (200G) | 330 | 13,281 |
| ILA | Juniper (Oplink) | Inline amplifier | 30 | 566 |

### Optical Topology
```
Teraflex Tx → ROADM MUX → Booster → 25km SMF → ILA → Preamp → DEMUX → Teraflex Rx
```

### Live Performance Monitoring Data
| Parameter | Value | Source |
|-----------|-------|--------|
| Q-factor | 13.8 dB | Teraflex OTSI PM |
| OSNR | 34.8 dB | Teraflex OTSI PM |
| FEC BER | ~3.5×10⁻⁷ | Teraflex OTU FEC PM |
| SNR | 21 dB | Teraflex OTSI PM |
| Rx Power | -5.8 dBm | Teraflex physical PM |
| Chromatic Dispersion | -1100 ps/nm | Teraflex OTSI PM |
| DGD | 3 ps | Teraflex OTSI PM |
| Carrier Freq Offset | -0.01 GHz | Teraflex OTSI PM |

## Key Innovation: Semantic Path Discovery

### The Problem
The Teraflex has 18,261 YANG paths. Finding "Q-factor" requires knowing the vendor-specific path:
```
/p1:bulk-data/p2:managed-element/p3:interface/p3:logical-interface/
p4:otsia/p4:otsi/p4:pm-monitored-entity/p4:pm-current-data/
p4:montype-monval[mon-type=adom-oduckpat:q-factor]
```

### The Solution
Semantic search using OpenAI `text-embedding-3-large`:
```bash
$ splice find-paths --semantic --pattern "Q-factor optical quality"

  0.544  /terminal-device/logical-channels/channel/otn/state/q-value
  0.503  /components/component/optical-channel
  0.486  /terminal-device/logical-channels/channel/otn/state/q-value
```

- Embeds all 18,261 path descriptions into vector space (one API call)
- Cached on disk for instant subsequent searches (~3 seconds)
- Pluggable providers: OpenAI or local sentence-transformers
- Works for any vendor's YANG schema — no vendor-specific configuration

## Observability Stack
```
Splice → Telegraf (gNMI subscribe) → InfluxDB → Grafana
```
- Real-time dashboards: Q-factor, OSNR, FEC-BER, optical power, temperature
- All three vendors in one unified dashboard
- Standard gNMI protocol — no custom integrations

## AI Agent Interface (MCP)
Splice exposes 11 MCP tools for AI agents:
- `connect_device` — connect to real or simulated devices
- `search_paths` — semantic search over catalog
- `get_telemetry` — read live values
- `set_config` — push configuration (with policy enforcement)
- `doctor` — diagnostic checks
- Rate limiting, set policy (deny-all default), structured errors

Example: Claude Code autonomously connecting to a Teraflex, searching for Q-factor, and reading live PM data through Splice.

## Results
| Metric | Value |
|--------|-------|
| Vendors supported | 3 (Lumentum, Adtran, Juniper) |
| Total telemetry paths | 19,428 |
| Test suite | 504 tests |
| Interfaces | 4 (CLI, API, TUI, MCP) |
| PM poll cycle | ~15 seconds |
| Semantic search | 3 seconds (cached) |
| Overnight stability | 24+ hours continuous |
| Code | ~18,000 lines Python |

## Technology Stack
- **Language**: Python 3.12
- **NETCONF**: ncclient, defusedxml
- **gNMI**: gRPC (protobuf)
- **YANG**: pyang
- **Database**: PostgreSQL (optional)
- **Embeddings**: OpenAI text-embedding-3-large / sentence-transformers
- **TUI**: Textual
- **AI Agent**: MCP protocol
- **Observability**: Telegraf, InfluxDB, Grafana

## Open Source
GitHub: Open-Ireland-Testbed/splice
License: AGPL-3.0-or-later (or commercial — see COMMERCIAL-LICENSE.md)

## Acknowledgments
Open Ireland Testbed, Trinity College Dublin
