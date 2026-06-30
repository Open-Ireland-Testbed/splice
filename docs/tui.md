# TUI Guide

The Splice TUI is an interactive terminal console for managing devices, browsing YANG catalog paths, watching live telemetry, and monitoring server health.

## Installation

```bash
.venv/bin/pip install -e ".[tui]"
```

This installs the [Textual](https://textual.textualize.io/) framework as an extra dependency. Use the project's virtual environment — system Python will reject `pip install` on modern Debian/Ubuntu (PEP 668).

## Launching

```bash
.venv/bin/splice-tui --devices-dir devices
```

The `--devices-dir` flag points to the directory containing device subdirectories (each with a `config.yaml`). Defaults to `devices/` in the current directory.

## Navigation

The TUI has six tabs, switchable via number keys or by clicking the tab headers:

| Key | Tab | Description |
|-----|-----|-------------|
| `1` | Devices | Device management |
| `2` | Paths | YANG catalog browser |
| `3` | Telemetry | Live value watcher |
| `4` | Status | Health dashboard |
| `5` | Operations | Server control + log viewer |
| `6` | Jobs | Polling job management |

Press `q` to quit. Press `?` for a keybinding summary.

---

## Tab 1: Devices

Lists all registered devices found in the `devices/` directory.

### Layout

A DataTable showing each device's name, host, NETCONF port, gNMI port, username, and database URL.

### Keybindings

| Key | Action |
|-----|--------|
| `a` | Add a new device (opens a modal form) |
| `e` | Edit the selected device's connection settings |
| `v` | View/edit full config.yaml (section browser) |
| `d` | Delete the selected device (with confirmation) |
| `t` | Test TCP connectivity to the device's NETCONF port |
| `r` | Refresh the device list |
| `Enter` | Select the highlighted device as active |

### Adding a device

Press `a` to open the add-device form. Fill in:
- **Device name** — directory name under `devices/` (e.g., `teraflex`)
- **Host / IP** — NETCONF target address
- **NETCONF port** — default 830
- **Username** — NETCONF user
- **Password** — stored in `devices/<name>/password` with `0600` permissions
- **gNMI server port** — the port this device's gNMI server will listen on
- **Database URL** — PostgreSQL connection string

This creates `devices/<name>/config.yaml` and a `yang/` subdirectory.

### Editing a device

Press `e` to open the edit form for the selected device. All fields are pre-populated with current values. Leave the password field blank to keep the existing password. Changes are written back to `config.yaml` preserving all sections not shown in the form (poller, cache, discovery, etc.).

### Viewing full config

Press `v` to open the config viewer for the selected device. The left panel shows config sections (gnmi, poller, cache, discovery, health, history, pm). Click a section to see its fields with current values and defaults. Press "Edit Section" to modify fields — the form adapts to each section's field types (inputs for numbers/strings, switches for booleans). Changes are saved directly to `config.yaml`.

### Selecting a device

Press `Enter` on a device row to make it the active device. The status bar at the bottom shows the selected device. All other tabs respond to the active device selection.

---

## Tab 2: Paths

A tree browser for the selected device's YANG catalog paths.

### Data sources

The path browser loads catalog data in priority order:
1. **Database** — queries `catalog_paths` table by device host+port (fastest, requires a prior `splice setup`)
2. **YANG files** — parses `.yang` files from the device's `yang_dir` (fallback, no DB needed)

If neither source has data, a hint is shown to run `splice setup`.

### Layout

```
+------ toolbar ------+
| [Filter paths...  ] | path count
+-------+-------------+
| Tree  | Detail      |
| (60%) | panel (40%) |
|       |             |
+-------+-------------+
```

- **Tree** (left): hierarchical view of catalog paths. Nodes are color-coded by YANG keyword:
  - Dim = container
  - Cyan = list
  - Green = leaf-list
  - Default = leaf

- **Detail panel** (right): shows metadata for the highlighted node — path, module, keyword, type, config flag, list keys, and description.

### Keybindings

| Key | Action |
|-----|--------|
| `/` | Focus the search/filter input |
| `Escape` | Clear search and restore full tree |
| `r` | Reload catalog from source |
| `c` | Copy the selected path to clipboard (requires `pyperclip` or `xclip`) |
| `f` | Toggle config-only filter (show only `config: true` paths) |
| `w` | Send the selected path to the Telemetry tab's watch list |

### Tree navigation

- **Arrow keys** or **j/k**: move up/down
- **Enter** or **Right arrow**: expand a node
- **Left arrow**: collapse a node

Expansion is lazy — children are loaded only when a node is expanded. This keeps the initial load fast even for catalogs with thousands of paths.

### Search and filter

Type in the filter bar to filter paths by substring match. The tree rebuilds to show only matching paths and their parent hierarchy.

**Semantic search** (optional): if `sentence-transformers` is installed (`pip install splice[search]`), prefix your query with `/` to trigger embedding-based semantic search. For example, `/optical power` finds paths related to optical power levels even if the exact words don't appear in the path string.

---

## Tab 3: Telemetry

A live-updating table showing current values for watched paths.

### Data sources

The telemetry service polls values in priority order:
1. **gNMI Get** — gRPC call to `localhost:<gnmi_port>` (requires a running `splice run`)
2. **DB fallback** — reads the `last_value` table (shows cached values when the server isn't running)

The connection status is shown in the toolbar (e.g., "gNMI :9339", "DB fallback", or "Disconnected").

### Layout

```
+------- toolbar ----------+
| [Add path...] [Add] | status
+---------------------------+
| Path | Value | Type | Age | Trend
| ...  | ...   | ...  | ... | ...
+---------------------------+
| [Sparkline ~~~~~~~~] Stats|
|  Min: 0.1  Max: 9.5  ... |
+---------------------------+
| Watching 3 paths          |
+---------------------------+
```

The sparkline panel shows a mini-chart of the last 60 numeric values for the currently highlighted path, along with min/max/avg statistics.

- **Path**: the watched path (truncated if longer than 50 chars)
- **Value**: current value (formatted for readability)
- **Type**: value type (str, int, float, bool, json)
- **Age**: time since the value was last updated (e.g., "5s", "2m", "1.3h")
- **Trend**: direction indicator comparing current to previous value:
  - Up arrow: value increased
  - Down arrow: value decreased
  - Right arrow: value unchanged
  - Delta: non-numeric value changed

Values auto-refresh every 3 seconds.

### Adding paths to watch

Three ways to add paths:
1. **From the Paths tab**: highlight a path and press `w`
2. **Manual input**: type a path in the input field and press `Enter` or click "Add"
3. The path must match a catalog path for gNMI Get to return data

### Keybindings

| Key | Action |
|-----|--------|
| `a` | Focus the path input field |
| `d` | Remove the selected path from the watch list |
| `r` | Force an immediate poll refresh |
| `p` | Pause/resume automatic polling |
| `x` | Clear all watched paths |
| `t` | Set high/low threshold for the selected path |
| `h` | Show DB history for the selected path |
| `e` | Export watched telemetry to CSV/JSON |

### Threshold alerts

Press `t` on a watched path to set high and/or low thresholds. When a value crosses a threshold, it is highlighted in the table (red for high, yellow for low) with a warning icon in the Trend column. Press `t` again to modify or clear thresholds.

### DB history

Press `h` to open a modal showing the last 100 recorded values from the `telemetry_history` database table. Requires `history.enabled: true` in the device config and a running server that records history.

### Export

Press `e` to save all watched path values to a file. Choose CSV or JSON format via the toggle switch. JSON export includes the sparkline history data for each path.

---

## Tab 4: Status

A health dashboard showing the state of the bridge infrastructure for the selected device.

### Layout

```
+------ cards ------+
| [gNMI] [NETCONF] [DB] |
+-----------+------------+
| Health    | Resource   |
| Details   | Counts     |
| (JSON)    | (table)    |
+-----------+------------+
```

### Status cards

Three cards at the top show green/red health indicators:

- **gNMI Server**: TCP probe to `localhost:<gnmi_port>`. Shows port and health endpoint status.
- **NETCONF**: TCP probe to `<host>:<netconf_port>`. Shows circuit breaker state (open/closed) and consecutive failure count.
- **Database**: SQL connectivity check. Shows catalog path and job counts.

### Health details

The left panel shows the raw JSON response from the `/health` HTTP endpoint (default port 9809). This includes circuit breaker state, consecutive failures, and auth failure count. If the server isn't running, a hint is shown.

### Resource counts

The right panel shows a table of DB resource counts:
- Catalog Paths
- Jobs
- Modules
- List Instances

### Auto-refresh

The dashboard auto-refreshes every 10 seconds. Press `r` for an immediate refresh.

---

## Tab 5: Operations

Run `splice setup` and `splice run` as managed subprocesses with live log output.

### Layout

```
+--- toolbar -----------------------+
| [Setup] [Run] [Stop] | status     |
+------------------------------------+
| Log output (RichLog, scrollable)   |
|                                    |
+------------------------------------+
| Lines: 142 | Errors: 0            |
+------------------------------------+
```

### Server status

The status label shows the current state:
- **Stopped** — no process running
- **Setup [4/7] Building catalog...** — setup in progress with step indicator
- **Starting (PID 1234)...** — server process started, waiting for gNMI readiness
- **Running (PID 1234, 5m)** — server is up and serving gNMI requests
- **Crashed (exit 1)** — process exited with an error

### Keybindings

| Key | Action |
|-----|--------|
| `d` | Run `splice doctor` pre-flight checks |
| `s` | Run `splice setup` for the selected device |
| `r` | Run `splice run` (start the gNMI server) |
| `x` | Stop the running process (graceful SIGTERM) |
| `c` | Clear the log output |

### Pre-flight doctor

Press `d` to run validation checks before setup. Output is colorized: green for PASS, red for FAIL, yellow for fix hints. Doctor checks include config validity, DB reachability, migration status, YANG directory, and NETCONF connectivity.

### Usage

1. Select a device in the Devices tab
2. Switch to Operations (`5`)
3. Press `d` to run doctor checks and verify everything is configured correctly
4. Press `s` to run setup — watch progress in the log as it discovers YANG modules, builds the catalog, and initializes jobs
5. When setup completes, the Paths tab auto-refreshes with the new catalog
6. Press `r` to start the gNMI server — the status shows PID and uptime once ready
7. Press `x` to stop the server gracefully

Only one process runs at a time. Stop the current process before starting a new one.

---

## Tab 6: Jobs

View and manage polling jobs for the selected device. Jobs define which YANG paths are polled and at what interval.

### Data source

Jobs are loaded from the `jobs` table in the device's database. A prior `splice setup` is required to populate jobs.

### Layout

```
+------- toolbar ----------+
| device: 42 jobs          |
+---------------------------+
| Path | Interval | Enabled | Mode | Updated
| ...  | 30s      | Yes     | ...  | ...
+---------------------------+
| 42 jobs (40 enabled, 2 disabled) |
+---------------------------+
```

- **Path**: the canonical YANG path (truncated if longer than 60 chars)
- **Interval**: polling interval in seconds
- **Enabled**: green "Yes" or red "No"
- **Mode**: polling mode (typically "sample")
- **Updated**: last modification timestamp

### Keybindings

| Key | Action |
|-----|--------|
| `e` | Edit the selected job (interval, enabled) |
| `r` | Refresh job list from database |
| `E` | Enable all jobs (Shift+E) |
| `D` | Disable all jobs (Shift+D) |
| `i` | Set interval for all enabled jobs |

### Editing a job

Press `e` to open the edit modal for the highlighted job. You can change:
- **Interval**: polling interval in seconds (must be a positive integer)
- **Enabled**: toggle on/off via switch

Changes are written directly to the database.

### Bulk operations

- **Enable all** (`E`): sets all jobs for this device to enabled
- **Disable all** (`D`): sets all jobs for this device to disabled
- **Set interval** (`i`): opens a modal to set a new interval for all currently enabled jobs

---

## Typical workflow

1. **Launch**: `.venv/bin/splice-tui --devices-dir devices`
2. **Add/edit a device**: Tab 1, press `a` to add or `e` to edit
3. **Run setup**: Press `5` then `s` — watch discovery and catalog build
4. **Browse paths**: Press `2`, expand tree nodes to explore the YANG schema
5. **Start the server**: Press `5` then `r`
6. **Watch telemetry**: Press `2`, highlight paths, press `w` to watch; view in Tab 3
7. **Check health**: Press `4` to verify server/device/DB connectivity

## Prerequisites by tab

| Tab | Minimum requirement | Full functionality |
|-----|--------------------|--------------------|
| Devices | `devices/` directory with configs | - |
| Paths | YANG files in device's `yang_dir` | DB after `splice setup` |
| Telemetry | DB with `last_value` data | Running `splice run` for live gNMI |
| Status | - | Running `splice run` + DB |
| Operations | Selected device with `config.yaml` | DB + NETCONF connectivity |
| Jobs | DB after `splice setup` | - |

## Clipboard support

The copy-path feature (`c` in the Paths tab) tries these backends in order:
1. `pyperclip` Python package (cross-platform)
2. `xclip` system command (Linux)

Install one of these for clipboard support:
```bash
pip install pyperclip    # or
sudo apt install xclip   # Linux
```
