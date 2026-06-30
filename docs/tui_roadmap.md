# TUI Roadmap

High-level plan for the Splice interactive TUI, broken into phases.

---

## Phase 1: App Shell + Device Management (DONE)

**Goal**: Foundation — Textual app with device list table.

- `SpliceApp` with Header, Footer, StatusBar
- `DevicesScreen` with DataTable (name, host, ports, user, DB URL)
- `DeviceRegistry` service — scans `devices/*/config.yaml`, CRUD operations
- `AddDeviceModal` and `ConfirmDeleteModal` screens
- TCP connectivity test via `@work(thread=True)`
- `selected_device` reactive property on the app

## Phase 2: Path Browser (DONE)

**Goal**: Tree-based YANG catalog browser per device.

- `TabbedContent` layout (Devices, Paths)
- `PathsScreen` with lazy-loading `Tree`, detail panel, search/filter
- `CatalogLoader` service — DB-first with YANG fallback, per-device caching
- Substring filter with 300ms debounce
- Optional semantic search via `PathIndex` (requires `sentence-transformers`)
- Copy path to clipboard (`pyperclip` / `xclip`)
- Config-only filter toggle
- Tab-aware keybinding hints in StatusBar

## Phase 3: Live Telemetry + Status Dashboard (DONE)

**Goal**: Watch live path values and monitor server health.

- `TelemetryScreen` — live-updating DataTable of watched path values
  - `TelemetryService` polls via gNMI Get (gRPC) or DB `last_value` fallback
  - 3-second auto-refresh, pause/resume, trend indicators
  - Cross-tab `w` binding from Paths tab via `WatchPath` message
- `StatusDashboard` — health cards + resource counts
  - `HealthChecker` queries `/health` HTTP endpoint, TCP probes, DB counts
  - `StatusCard` reusable widget with green/red indicator
  - 10-second auto-refresh
- `DeviceEntry` extended with `health_port`

## Phase 4: Device Config Editor, Server Control + Log Viewer (DONE)

**Goal**: Edit device configs, run setup/server as managed subprocesses, and view live logs.

### 4a. Device Config Editor (Devices tab enhancements)

Add an **Edit Device** modal (`e` keybinding) to modify existing device configs:

- Pre-populated form with current values from `config.yaml`
- Editable fields: host, NETCONF port, gNMI port, username, password, DB URL, health port
- Saves back to `config.yaml` via `DeviceRegistry.update()`
- `DeviceRegistry.update()` method: reads existing YAML, patches changed fields, writes back

### 4b. Server Control (new Operations tab, Tab 5)

Manage `splice setup` and `splice run` as subprocesses per device.

**Widget hierarchy:**
```
OperationsScreen
  Vertical(id="ops-root")
    Horizontal(id="ops-toolbar")
      Button("Setup", id="btn-setup")
      Button("Run", id="btn-run")
      Button("Stop", id="btn-stop")
      Static(id="ops-server-status")   # "Stopped" / "Running (PID 1234, 5m)" / "Setup [4/7]"
    RichLog(id="ops-log")              # Live log output
    Horizontal(id="ops-footer")
      Static(id="ops-stats")           # "Lines: 142 | Errors: 0"
```

**Process management (`services/process_manager.py`):**
```python
@dataclass
class ProcessState:
    status: str          # "stopped" | "running" | "setup" | "crashed"
    pid: int | None
    started_at: float | None
    exit_code: int | None
    setup_step: str      # e.g. "[4/7] Discovering YANG modules..."

class ProcessManager:
    def start_setup(self, config_path: str) -> None
        # Popen(["splice", "--config", config_path, "setup"],
        #       stdout=PIPE, stderr=STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    def start_run(self, config_path: str) -> None
        # Popen(["splice", "--config", config_path, "run"], ...)
    def stop(self) -> None
        # SIGTERM → wait(10) → SIGKILL fallback
    def restart(self) -> None
        # stop() then start_run()
    def get_state(self) -> ProcessState
    def read_output(self) -> list[str]
        # Non-blocking readline from process stdout
```

**Key behaviors:**
- Only one process per device at a time (setup OR run, not both)
- `setup` output is parsed for `[N/7]` progress markers → shown in status
- `run` output monitored for `"gNMI server listening on"` → status changes to "Running"
- `SIGTERM` for graceful shutdown (triggers CLI's cleanup handler)
- `PYTHONUNBUFFERED=1` in subprocess env for real-time output
- Process stdout piped to `RichLog` widget via `@work(thread=True)` reader loop

**Log viewer:**
- `RichLog` widget for scrollable, syntax-highlighted output
- Background worker reads subprocess stdout line-by-line
- Auto-scroll follows latest output; pauses if user scrolls up
- `c` to clear log, `l` to toggle log level filter

**Operations flow:**
1. Select device in Devices tab → switch to Operations tab
2. Press "Setup" → runs `splice --config <path> setup`, streams output to log
3. Setup completes → auto-refreshes Paths tab catalog
4. Press "Run" → starts `splice --config <path> run`, streams output
5. Status shows PID and uptime
6. Press "Stop" → sends SIGTERM, waits for graceful shutdown

### New files

| File | Purpose |
|------|---------|
| `services/process_manager.py` | Subprocess lifecycle (start/stop/status/output) |
| `screens/operations.py` | Operations screen with buttons, status, log viewer |
| `screens/devices.py` | Add `EditDeviceModal`, `e` keybinding |
| `services/device_registry.py` | Add `update()` method |

### Modified files

| File | Change |
|------|--------|
| `app.py` | Add Tab 5 (Operations), binding `5`, wire device changes |
| `widgets/status_bar.py` | Add hints for operations tab |
| `styles.tcss` | Styles for operations screen, log viewer |

### Keybindings (Operations tab)

| Key | Action |
|-----|--------|
| `s` | Run setup |
| `r` | Run server (or restart if running) |
| `x` | Stop server |
| `c` | Clear log |

### Implementation order

1. `DeviceRegistry.update()` + `EditDeviceModal` (quick, standalone)
2. `ProcessManager` service (core logic, testable independently)
3. `OperationsScreen` with RichLog and buttons
4. App integration (Tab 5, wiring, styles)
5. Auto-refresh Paths tab after setup completes

## Phase 5: Job Management (DONE)

**Goal**: View and manage polling jobs from the TUI.

- `JobsScreen` (Tab 6) — DataTable of polling jobs for the selected device
  - Columns: path, interval, enabled, mode, updated
  - `JobService` loads from DB (`jobs` table) via `make_session_factory`
- `EditJobModal` — modal to change interval and enabled state via `Switch`
- `BulkIntervalModal` — modal to set interval on all enabled jobs
- Bulk enable/disable all with uppercase `E`/`D` bindings
- Stats footer showing enabled/disabled counts

### New files

| File | Purpose |
|------|---------|
| `services/job_service.py` | DB access: fetch, update, bulk enable, bulk set interval |
| `screens/jobs.py` | Jobs screen with DataTable, EditJobModal, BulkIntervalModal |

### Modified files

| File | Change |
|------|--------|
| `app.py` | Add Tab 6 (Jobs), binding `6`, wire device changes, `JobService` |
| `widgets/status_bar.py` | Add hints for jobs tab, update tab range to `1-6` |
| `styles.tcss` | Styles for jobs screen |

## Phase 6: Config Viewer + Pre-flight Doctor (DONE)

**Goal**: Browse/edit full device config and run pre-flight validation.

- `ConfigViewerModal` (`v` in Devices tab) — section-based config.yaml browser
  - OptionList of sections (gnmi, poller, cache, discovery, health, history, pm)
  - Right panel shows field values with defaults marked
  - `EditSectionModal` with dynamic form generation from `FieldDef` definitions
  - Supports int/float/bool/str fields; validates choices (expansion_mode, batch_mode, etc.)
  - Saves back to config.yaml preserving other sections
- Pre-flight Doctor (`d` in Operations tab) — runs `splice doctor` as subprocess
  - Colorized output: green PASS, red FAIL, yellow fix hints
  - Uses `@work(thread=True)` subprocess.run (short-lived, not ProcessManager)

### New files

| File | Purpose |
|------|---------|
| `screens/config_viewer.py` | ConfigViewerModal, EditSectionModal, FieldDef, SECTION_FIELDS |

### Modified files

| File | Change |
|------|--------|
| `screens/devices.py` | Add `v` keybinding, `action_view_config` |
| `screens/operations.py` | Add `d` keybinding, Doctor button, `_do_doctor` worker |
| `widgets/status_bar.py` | Add `v:config` and `d:doctor` hints |

## Phase 7: Advanced Telemetry (DONE)

**Goal**: Richer telemetry visualization with sparklines, history, export, and alerts.

- **Sparkline + value history** — In-memory ring buffer (last 60 values) per path, Sparkline widget below table, min/max/avg stats on row selection
- **DB history modal** (`h`) — queries `telemetry_history` table, shows last 100 values with timestamps
- **Export** (`e`) — ExportModal saves watched telemetry to CSV or JSON with format toggle
- **Threshold alerts** (`t`) — ThresholdModal for per-path high/low thresholds; values styled with Rich Text (red for high, yellow for low, with warning icon)

### Modified files

| File | Change |
|------|--------|
| `services/telemetry_service.py` | Added `_history` ring buffer, `record_value()`, `get_history()`, `Threshold` dataclass, threshold methods, `fetch_db_history()` |
| `screens/telemetry.py` | Added Sparkline detail panel, `RowHighlighted` handler, ThresholdModal, ExportModal, HistoryModal, Rich Text alert styling |
| `widgets/status_bar.py` | Updated telemetry tab hints |
| `styles.tcss` | Added sparkline detail panel styles |

### Deliberately deferred

- **gNMI Subscribe streaming** — disproportionate complexity vs. 3s Get polling for a monitoring TUI. Deferred to Phase 9+ if needed.

## Phase 8: Multi-Device Operations

**Goal**: Operate across multiple devices simultaneously.

- **Split view** — side-by-side telemetry from two devices
- **Aggregate status** — dashboard showing all devices at once
- **Batch operations** — run setup/start across multiple devices
- **Device groups** — tag and filter devices by group
