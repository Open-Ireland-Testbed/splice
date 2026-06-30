# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Splice is a vendor-agnostic gNMI server that exposes telemetry data from NETCONF devices that don't natively support gNMI. It bridges NETCONF devices (port 830) to gNMI clients (gRPC, port 9339).

## Commands

### Installation
```bash
python -m venv .venv
.venv/bin/pip install -e .         # installs `splice` CLI entry point
.venv/bin/pip install -e .[kafka]  # optional Kafka support
.venv/bin/pip install -e .[search] # optional semantic path search (sentence-transformers)
.venv/bin/pip install -e .[openai] # semantic search via OpenAI embeddings instead
.venv/bin/pip install -e .[mcp]   # optional MCP server for AI agents
.venv/bin/pip install -e .[tui]    # interactive TUI console
```

### Testing
```bash
# Run tests (no database needed) — pytest auto-sets PYTHONPATH from pyproject.toml
.venv/bin/python -m pytest

# Run tests with PostgreSQL (enables _pg tests)
TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/splice' \
  .venv/bin/python -m pytest

# Single test
.venv/bin/python -m pytest tests/test_specific.py::test_function -v
```

Tests with `_pg` suffix require `TEST_DATABASE_URL`. CI runs all tests against PostgreSQL 16.

### CLI
After `pip install -e .`, use the `splice` entry point. Otherwise prefix with `PYTHONPATH=src python src/splice/cli.py`.

```bash
# Essential workflow
splice init                              # Interactive config generator
NETCONF_PASSWORD=... splice quickstart --host <ip> --username <u>  # config+setup+run in one shot
splice --config config.yaml setup        # Full pipeline: migrate + discover + catalog + jobs + dashboards
splice --config config.yaml setup --prune  # ... also delete catalog/jobs/aliases gone from this build
splice --config config.yaml run          # Start gNMI server
splice --config config.yaml status       # Health + poller sweep stats from a running bridge
splice --config config.yaml curate-jobs  # Apply metrics: curation to existing jobs

# Diagnostics
splice --config config.yaml doctor       # Pre-flight validation
splice --config config.yaml find-paths --pattern <pattern>
splice --config config.yaml list-jobs
splice --config config.yaml export --what catalog --format json --out catalog.json
splice --config config.yaml setup-smoke-test   # run setup pipeline without serving
splice --config config.yaml setup-e2e-check    # verify one expanded path is actually polled
splice --config config.yaml explain-setup      # summarize setup_report.json
splice --config config.yaml dashboards         # generate Grafana dashboards (also runs as setup step 8, best-effort)
```

Run `splice --help` for the full command list. All commands except `init` and `mcp` require `--config`.

### Simulation Mode
```bash
# Run against a simulated OpenROADM device (no real hardware needed)
splice --simulate openroadm run
splice --simulate openroadm --config config.yaml run  # use config for gNMI port settings
```

### MCP Server (AI Agent Interface)
```bash
# Start MCP server on stdio (for Claude Code, AI agents)
splice mcp
splice mcp --policy policy.yaml    # with custom set policy
splice mcp --audit-log audit.jsonl  # with audit logging
```

### TUI
After `pip install -e ".[tui]"`, use the `splice-tui` entry point:
```bash
splice-tui --devices-dir devices    # Launch interactive TUI (default: devices/)
```

### Verifying a running server
```bash
gnmic -a 127.0.0.1:9339 --insecure get --path /
gnmic -a 127.0.0.1:9339 --insecure subscribe --mode once --path '<path>'
```

## Architecture

### Three Usage Stacks

The project has three ways to use it, sharing the same NETCONF/catalog/planner core:

1. **CLI + Database stack** (`cli.py` → service layer → `db/`) — Full pipeline with PostgreSQL persistence. Config via YAML files. Multi-device support with per-device config in `devices/<name>/config.yaml`.

2. **Python API stack** (`api/device.py` → `api/_discovery.py`, `api/_catalog.py`) — Programmatic API with no database or config files. All state in `InMemoryCatalog` dataclass. See `api/device.py` for the `Device` class and its methods.

3. **MCP server stack** (`mcp_server.py` → `api/device.py`) — AI agent interface over stdio. Wraps the Python API with session management, rate limiting, set policy, and structured error responses. No database required.

### Layered Design
- **MCP layer** (`mcp_server.py`) — AI agent tools, session pool, set policy, rate limiting
- **API layer** (`api/`) — Programmatic Python API (no database required)
- **CLI layer** (`cli.py`) — All commands, entry point
- **TUI layer** (`tui/`) — Textual-based interactive console (see below)
- **Service layer** (`discovery.py`, `catalog.py`, `pm.py`) — Business logic
- **Simulator layer** (`simulator/`) — Simulated NETCONF backends with device profiles
- **NETCONF layer** (`netconf/`) — Protocol handling, polling, resilience
- **Database layer** (`db/`) — SQLAlchemy ORM, repository pattern
- **gNMI server** (`gnmi_server.py`) — gRPC service, path matching, TTL caching

### Data Flow
1. **Discovery**: NETCONF → YANG modules → catalog paths (stored in DB or `InMemoryCatalog`)
2. **Runtime**: gNMI request → catalog lookup → NETCONF poll → cache → gNMI response

### Key Modules
- `netconf/client.py` — ncclient wrapper with connection pooling, circuit breaker, auth failure detection
- `netconf/poller.py` — Polling engine with throttling, circuit-breaker-aware pausing, XML→KV parsing
- `netconf/setter.py` — NETCONF edit-config for gNMI Set. `<config>` must use NETCONF base namespace
- `gnmi_server.py` — gRPC server with path matching and TTL caching
- `expander.py` — Path expansion for keyed lists with guardrails
- `planner.py` — Batched query planning for efficient NETCONF reads
- `pathing.py` — Path normalization, key extraction, prefix matching
- `path_index.py` — Path tree browser and hybrid search over catalog paths: BM25 keyword ranking (always available) blended with embedding similarity when a provider is installed (`search`/`openai` extras); modes `hybrid` (default), `bm25`, `semantic`
- `grafana_dashboards.py` — Generates per-device Grafana dashboards (live telemetry + YANG inventory) from the YANG catalog; `dashboards` CLI command and best-effort setup step 8; queries target InfluxDB (Telegraf `device_host` tag), datasource via template variable
- `pm.py` — PM data stored under synthetic `/pm/{domain}[<instance keys>]/{name}/{mon_type}` paths (not catalog paths). Instance keys are discovered from reply-XML ancestors (key leaves: name/entity-name/index/id), rendered as a single bracket with comma-joined alphabetically-sorted percent-encoded values — the canonical form `_path_to_str` produces, so client queries compare equal. Also hosts `parse_selector`/`match_selector` (shared with doctor). Empty and sentinel `mon-val` values ("", NA, n/a, -, unavailable, …) are skipped (would poison TSDB field types).
- `mcp_server.py` — MCP server with 11 tools, session pool, set policy (deny-all default), rate limiting, error taxonomy
- `simulator/` — Simulated NETCONF backends; `Device("sim://openroadm")` for hardware-free testing
- `runtime.py` — Shared pipeline steps (discover, fetch-yang, build-catalog, build-aliases, init-jobs) used by both `setup` and the individual CLI commands
- `trigger_api.py` + `netconf/trigger_executor.py` — On-demand poll HTTP API: `POST /trigger` with bearer token (default port 9812, disabled by default via `trigger_api.enabled`); can auto-onboard unknown paths as jobs
- `kafka_publisher.py` — Optional Kafka publishing of telemetry and trigger events
- `health.py` — HTTP `/health` endpoint served alongside the gNMI server (queried by the TUI)

### Connection Resilience
- **Reconnect-once**: On RPC failure, close session, wait `reconnect_delay_sec`, retry once. No retry loops.
- **Circuit breaker**: After N consecutive double-failures, refuse new RPCs for cooldown period.
- **Auth failure detection**: Auth errors immediately open circuit breaker with long cooldown to prevent device lockouts.
- **Poller throttling**: Configurable delay between tasks prevents rapid-fire RPCs.

### Multi-Device Model
- One gNMI server instance per NETCONF device
- Separate gNMI ports (9339, 9340, etc.)
- Shared PostgreSQL with `device_id` foreign keys
- Per-device config in `devices/<name>/config.yaml`

### TUI (`tui/`)

Interactive Textual console, installed with `pip install -e ".[tui]"` and launched via `splice-tui --devices-dir <path>`.

```
tui/
├── __init__.py              # CLI entry point (argparse → SpliceApp)
├── app.py                   # SpliceApp — TabbedContent (6 tabs), reactive selected_device
├── styles.tcss              # Global TCSS styles
├── screens/
│   ├── devices.py           # DevicesScreen — DataTable of devices, add/delete/test modals
│   ├── paths.py             # PathsScreen — lazy-loading Tree, detail panel, search/filter
│   ├── telemetry.py         # TelemetryScreen — live-updating watched path values
│   ├── status.py            # StatusDashboard — health cards + resource counts
│   ├── operations.py        # OperationsScreen — subprocess control + log viewer + doctor
│   ├── jobs.py              # JobsScreen — polling job management, edit/bulk modals
│   └── config_viewer.py     # ConfigViewerModal + EditSectionModal — full config browser/editor
├── widgets/
│   ├── status_bar.py        # StatusBar — selected device display, tab-aware keybinding hints
│   └── status_card.py       # StatusCard — reusable health indicator card
└── services/
    ├── _db.py               # Shared DB helpers — cached session factory, device lookup
    ├── device_registry.py   # DeviceRegistry — scans devices/*/config.yaml, CRUD operations
    ├── catalog_loader.py    # CatalogLoader — DB-first catalog loading with YANG fallback, caching
    ├── telemetry_service.py # TelemetryService — gNMI Get + DB last_value polling
    ├── health_checker.py    # HealthChecker — health endpoint, TCP probes, DB counts
    ├── process_manager.py   # ProcessManager — manages splice setup/run subprocesses
    └── job_service.py       # JobService — DB CRUD for polling jobs
```

- **Tab 1 (Devices)**: DataTable listing all devices from `devices/` directory. Actions: add (`a`), edit (`e`), view config (`v`), delete (`d`), test connectivity (`t`), refresh (`r`), select (`Enter`).
- **Tab 2 (Paths)**: Tree browser of catalog paths for the selected device. Lazy-loads children on expand. Detail panel shows path metadata (module, keyword, type, config flag). Search with substring filter or `/query` for semantic search (requires `sentence-transformers`). Actions: search (`/`), refresh (`r`), copy path (`c`), config-only filter (`f`), watch path (`w`).
- **Tab 3 (Telemetry)**: Live-updating DataTable of watched path values. Polls via gNMI Get (preferred) or DB `last_value` fallback every 3s. Shows value, type, age, and trend indicators. Sparkline detail panel with min/max/avg stats. Threshold alerts (red/yellow values). DB history modal. CSV/JSON export. Cross-tab: press `w` in Paths tab to add a path. Actions: add (`a`), remove (`d`), threshold (`t`), history (`h`), export (`e`), refresh (`r`), pause (`p`), clear all (`x`).
- **Tab 4 (Status)**: Health dashboard with StatusCards for gNMI server, NETCONF, and Database. Queries health HTTP endpoint, TCP probes, and DB resource counts. Auto-refreshes every 10s. Shows circuit breaker state, connection health, and catalog/job/module counts.
- **CatalogLoader** tries DB first (`fetch_catalog_map_with_module`), falls back to YANG parsing (`build_catalog`), caches per device.
- **TelemetryService** tries gNMI Get via gRPC to `localhost:{gnmi_port}`, falls back to DB `last_value` table. Tracks value trends.
- **Tab 5 (Operations)**: Server control panel. Pre-flight doctor checks (`d`). Run `splice setup` or `splice run` as managed subprocesses. Live log output via `RichLog`. Shows process state (PID, uptime, setup step progress). Auto-refreshes Paths tab after setup completes. Actions: doctor (`d`), setup (`s`), run (`r`), stop (`x`), clear log (`c`).
- **Tab 6 (Jobs)**: Polling job management for the selected device. DataTable of jobs from DB with path, interval, enabled, mode, updated. Edit modal with interval + enabled switch. Bulk operations: enable all (`E`), disable all (`D`), set interval (`i`). Actions: edit (`e`), refresh (`r`).
- **HealthChecker** queries `/health` HTTP endpoint, runs TCP probes, and counts DB resources (catalog paths, jobs, modules, list instances).
- **ProcessManager** manages one subprocess at a time via `Popen`. Uses `sys.executable -m splice.cli` to ensure same venv. Background reader thread parses output for `[N/8]` setup progress and "gNMI server listening" readiness. Graceful SIGTERM shutdown with SIGKILL fallback.

## Configuration

YAML-based. Key sections: `device` (NETCONF connection), `db.url` (PostgreSQL), `gnmi` (server settings), `poller` (intervals, resilience, `task_failure_limit` — tasks auto-drop from rotation after N consecutive failures), `metrics` (curated polling: only paths matching `include` patterns get jobs; `disable_uncurated` disables the rest), `cache` (TTL), `discovery` (module filters, PM discovery, `pm_refresh_on_run` drift refresh), `trigger`/`trigger_api` (on-demand polling, off by default), `kafka` (publishing, off by default), `health` (HTTP health endpoint, includes poller sweep stats), `history` (telemetry history retention). See `config.py` for dataclass definitions and README.md for a full example config.

### PM fanout and drift
- PM filters collapse at `discovery.pm_selector_list_path`: one subtree filter per (list, instance) returns ALL PM domains below that instance in a single RPC. Roots outside that list keep narrow per-root filters. Never emit an unkeyed filter at a list element (it would fetch all instances at once).
- `run` re-resolves the fanout list at startup (`pm_refresh_on_run`) and rebuilds filters on drift; `doctor`'s `pm_filters` check fails when filters reference instances the device no longer has (devices return empty data — not errors — for ghost instance keys).

Credentials: Use `NETCONF_PASSWORD` env var or `device.password_file` (never commit passwords).

### Expansion Modes
| Mode     | Behaviour |
|----------|-----------|
| `direct` | Use cached list instances only (default, safe) |
| `full`   | Fresh discovery on every `run` start |
| `lazy`   | Like direct, but expand on first gNMI request |

## gNMI Path Conventions

- **Canonical paths**: YANG module-qualified (`/p1:components/p1:component`)
- **Alias paths**: Without namespace prefixes
- **Keyed paths**: List predicates normalized by key order (`[name=cpu0]`)

## Database

PostgreSQL required for CLI stack. Migrations in `src/splice/migrations/` (001-007) applied by `setup` or `migrate` command.

Tables: `devices`, `modules`, `yang_files`, `catalog_paths`, `path_aliases`, `jobs`, `list_instances`, `pm_filters`, `last_value`, `telemetry_history`

## Simulation Mode

Simulated NETCONF backends allow running the full pipeline without real hardware. Useful for development, testing, and AI agent experimentation.

- **Python API**: `Device("sim://openroadm")` or `Device(host, simulate="openroadm")`
- **CLI**: `splice --simulate openroadm run`
- **Available profiles**: `openroadm` (13 YANG modules, optical power/frequency data, alarms, LLDP)
- **Adding profiles**: Implement `DeviceProfile` protocol in `simulator/profiles/`, register in `simulator/__init__.py`

## MCP Server (AI Agent Interface)

Exposes the bridge as MCP tools over stdio for AI agents. Built on top of the Python API — no database required.

### Tools
| Tool | Description |
|---|---|
| `connect_device` | Connect to a NETCONF device (or simulated), returns device_id handle |
| `disconnect_device` | Close a device session |
| `get_telemetry` | Read telemetry values for a path |
| `set_config` | Push config (subject to set policy) |
| `find_paths` | Search catalog paths by pattern |
| `search_paths` | Semantic search over catalog using embeddings |
| `subscribe_once` | One-shot subscription (current values for a path) |
| `list_devices` | Show connected devices and status |
| `device_health` | Connection state, circuit breaker |
| `doctor` | Diagnostic checks (connection, catalog, circuit breaker) |
| `list_capabilities` | Readable/writable paths for a device |

### Safety
- **Set policy**: Deny-all by default. Configure via `--policy policy.yaml` with `allowed_paths`, `forbidden_paths`, `value_constraints`
- **Rate limiting**: 30 RPCs/min per device (configurable)
- **Error taxonomy**: Structured errors with `error_code`, `retryable`, `retry_after_sec`, `remediation_hint`

### Error Codes
`AUTH_LOCKOUT`, `SESSION_LIMIT`, `DEVICE_UNREACHABLE`, `CIRCUIT_BREAKER_OPEN`, `PATH_NOT_FOUND`, `SET_REJECTED`, `SET_POLICY_DENIED`, `DISCOVERY_TIMEOUT`, `DEVICE_NOT_CONNECTED`, `RATE_LIMITED`

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and PR to main:
1. Migration safety tests (`test_migrator.py`, `test_doctor.py`, `test_cli_migration_hints.py`)
2. Python API tests (no database)
3. Full test suite with PostgreSQL 16
