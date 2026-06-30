# CLI Reference

The `splice` command is the primary entry point for the CLI + database stack. It drives the
full lifecycle: generating config, discovering a device's YANG models, building the telemetry
catalog, creating polling jobs, and serving gNMI. This page documents every subcommand, its
purpose, usage, and notable flags.

After `pip install -e .`, invoke commands as `splice <command>`. Without installing, prefix with
`PYTHONPATH=src python src/splice/cli.py <command>`.

!!! note "Almost every command needs `--config`"
    Only `init`, `quickstart`, and `mcp` run without `--config`. `subscribe` and the
    simulated `run` use the Python API but still require `--config` (except when `--simulate`
    is combined with `run`). Everything else loads a YAML config and connects to a database.

## Global flags

These come before the subcommand: `splice [global flags] <command> [command flags]`.

| Flag | Purpose |
|---|---|
| `--config <path>` | Path to the YAML config file. Required by most commands (see note above). |
| `--simulate <profile>` | Run against a simulated device instead of real hardware (e.g. `openroadm`). Used with `run`. |
| `--verbose` | Show third-party protocol logs (ncclient hello/capability dumps, paramiko). Off by default — these are silenced to `WARNING`. |

```bash
splice --config config.yaml setup
splice --simulate openroadm run
splice --config config.yaml --verbose discover
```

## Command summary

| Command | Group | Purpose |
|---|---|---|
| `init` | Lifecycle | Interactive config generator |
| `quickstart` | Lifecycle | Point at a device → generate config, run setup, then serve |
| `migrate` | Lifecycle | Apply pending database migrations |
| `setup` | Lifecycle | Run the full discovery pipeline |
| `run` | Lifecycle | Start the gNMI server |
| `curate-jobs` | Jobs | Apply `metrics:` curation to existing jobs (no discovery) |
| `discover` | Discovery | Discover YANG modules over NETCONF |
| `fetch-yang` | Discovery | Download YANG schema files from the device |
| `build-catalog` | Discovery | Build the telemetry catalog from fetched YANG |
| `build-aliases` | Discovery | Build prefix-free alias paths for the catalog |
| `list-instances` | Discovery | List resolved keyed-list instances |
| `refresh-list-instances` | Discovery | Re-resolve keyed-list instances from the device |
| `debug-list-instances` | Discovery | Dump a debug pack of list-instance resolution |
| `list-keyed-paths` | Discovery | Expand keyed catalog paths into concrete instances |
| `init-jobs` | Jobs | Create polling jobs from the catalog |
| `set-job` | Jobs | Add or update a single polling job |
| `set-job-selector` | Jobs | Create jobs for list instances matching a selector |
| `list-jobs` | Jobs | List configured polling jobs |
| `doctor` | Diagnostics | Pre-flight validation checks |
| `status` | Diagnostics | Health + poller stats from a running bridge |
| `find-paths` | Diagnostics | Search catalog paths by pattern |
| `export` | Diagnostics | Export catalog or jobs to JSON/CSV |
| `setup-smoke-test` | Diagnostics | Run setup without serving; validate discovery/expansion |
| `setup-e2e-check` | Diagnostics | Verify one expanded path is actually polled |
| `explain-setup` | Diagnostics | Print a human-readable `setup_report.json` summary |
| `dashboards` | Diagnostics | Generate Grafana dashboards from the catalog |
| `mcp` | Interfaces | Start the MCP server on stdio |
| `subscribe` | Interfaces | Subscribe to a path prefix and print updates |

---

## Setup & run lifecycle

### `init`

Interactive config generator. Prompts for device and server settings and writes a config file.

```bash
splice init
splice init --non-interactive --name router1 --host 192.0.2.10 --username admin --gnmi-port 9339
```

| Flag | Default | Purpose |
|---|---|---|
| `--non-interactive` | off | Skip prompts; take values from flags/env vars |
| `--name` | — | Device name |
| `--host` | — | Device host/IP |
| `--username` | — | NETCONF username |
| `--password` | — | NETCONF password (prefer the `NETCONF_PASSWORD` env var) |
| `--port` | `830` | NETCONF port |
| `--gnmi-port` | `9339` | gNMI server port |
| `--db-url` | — | Database URL |

!!! warning "Never commit passwords"
    Prefer `NETCONF_PASSWORD` or a `device.password_file` over `--password`.

### `quickstart`

Point at a NETCONF device and get a running gNMI server in one shot: generates
`devices/<name>/config.yaml`, runs `setup`, then `run`. Does not require `--config`.

```bash
NETCONF_PASSWORD=secret splice quickstart --host 192.0.2.10 --username admin
splice quickstart --host 192.0.2.10 --username admin --password-file ./pw --setup-only
```

| Flag | Default | Purpose |
|---|---|---|
| `--host` | *required* | Device host/IP |
| `--username` | *required* | NETCONF username |
| `--password-file` | — | File containing the NETCONF password (default: `NETCONF_PASSWORD` env var) |
| `--name` | derived from host | Device name |
| `--port` | `830` | NETCONF port |
| `--gnmi-port` | `9339` | gNMI server port |
| `--db-url` | per-device `splice_<name>` DB, created if missing | Database URL |
| `--setup-only` | off | Generate config and run setup, but do not start the server |

### `migrate`

Apply pending database migrations.

```bash
splice --config config.yaml migrate
splice --config config.yaml migrate --dry-run
```

| Flag | Purpose |
|---|---|
| `--dry-run` | Show pending migrations without applying them |

### `setup`

Run the full discovery pipeline: migrate → discover → fetch-yang → build-catalog →
build-aliases → init-jobs (plus list instances, PM filters, and best-effort dashboards).

```bash
splice --config config.yaml setup
splice --config config.yaml setup --include-config --interval 60
splice --config config.yaml setup --prune
splice --config config.yaml setup --prune --force
```

| Flag | Default | Purpose |
|---|---|---|
| `--interval <sec>` | falls back to `poller.default_interval_sec` (`30`) | Polling interval for created jobs |
| `--include-config` | off | Include config (writable) paths in the catalog |
| `--skip-migrate` | off | Skip the database migration step |
| `--skip-list-instances` | off | Skip list-instance resolution |
| `--prune` | off | Delete catalog paths (and their aliases/jobs) absent from this build |
| `--force` | off | Override the prune safety threshold (refuses when new < 50% of old) |

!!! tip "Pruning safely"
    `--prune` removes catalog entries that disappeared between builds. As a guardrail it
    refuses to prune when the new build has fewer than 50% of the previous paths; pass
    `--force` to override.

### `run`

Start the gNMI server, serving the catalog over gRPC and polling the device over NETCONF.
Takes no command-specific flags.

```bash
splice --config config.yaml run
splice --simulate openroadm run                      # simulated device
splice --simulate openroadm --config config.yaml run # simulated, using config gNMI settings
```

---

## Discovery & catalog

These commands are the individual stages that `setup` runs end to end. Use them to re-run a
single stage.

### `discover`

Discover YANG modules advertised by the device over NETCONF. No command-specific flags.

```bash
splice --config config.yaml discover
```

### `fetch-yang`

Download the YANG schema files for discovered modules. No command-specific flags.

```bash
splice --config config.yaml fetch-yang
```

### `build-catalog`

Build the telemetry catalog from fetched YANG files, then report keyed list nodes.

```bash
splice --config config.yaml build-catalog
splice --config config.yaml build-catalog --include-config --prune --force
```

| Flag | Purpose |
|---|---|
| `--include-config` | Include config (writable) paths in the catalog |
| `--prune` | Delete catalog paths (and their aliases/jobs) absent from this build |
| `--force` | Override the prune safety threshold (new < 50% of old) |

### `build-aliases`

Build prefix-free alias paths (no namespace prefixes) for catalog paths. No command-specific flags.

```bash
splice --config config.yaml build-aliases
```

### `list-instances`

List the resolved keyed-list instances stored in the database.

```bash
splice --config config.yaml list-instances
splice --config config.yaml list-instances --list-path /p1:components/p1:component
```

| Flag | Purpose |
|---|---|
| `--list-path` | Filter by a specific list path |

### `refresh-list-instances`

Re-resolve keyed-list instances from the device (picks up added/removed instances).

```bash
splice --config config.yaml refresh-list-instances
splice --config config.yaml refresh-list-instances --list-path /p1:components/p1:component
```

| Flag | Purpose |
|---|---|
| `--list-path` | Limit the refresh to a specific list path |

### `debug-list-instances`

Dump a debug pack describing how list instances were resolved, for troubleshooting.

```bash
splice --config config.yaml debug-list-instances --out ./debug_pack
splice --config config.yaml debug-list-instances --out ./debug_pack --limit 5
```

| Flag | Default | Purpose |
|---|---|---|
| `--out` | *required* | Output directory for the debug pack |
| `--limit <n>` | `0` (no limit) | Max list paths to inspect |

### `list-keyed-paths`

Expand keyed catalog paths into concrete per-instance paths.

```bash
splice --config config.yaml list-keyed-paths
splice --config config.yaml list-keyed-paths --all --path /p1:components
```

| Flag | Purpose |
|---|---|
| `--all` | Expand all catalog paths if no jobs are set |
| `--path` | Filter expanded paths by prefix |

---

## Jobs

Polling jobs determine which catalog paths the running server actually polls and how often.

### `init-jobs`

Create polling jobs from the catalog (subject to `metrics:` curation when configured).

```bash
splice --config config.yaml init-jobs
splice --config config.yaml init-jobs --interval 60
```

| Flag | Default | Purpose |
|---|---|---|
| `--interval <sec>` | falls back to `poller.default_interval_sec` (`30`) | Polling interval for created jobs |

### `set-job`

Add or update a single polling job for one path.

```bash
splice --config config.yaml set-job --path /p1:components/p1:component/p1:state --interval 30
splice --config config.yaml set-job --path components/component/state --interval 30 --alias
```

| Flag | Purpose |
|---|---|
| `--path` | *required* — the path to poll |
| `--interval <sec>` | *required* — polling interval |
| `--alias` | Treat `--path` as an alias (prefix-free) path |

### `set-job-selector`

Create jobs for the list instances under a base path that match a selector expression.

```bash
splice --config config.yaml set-job-selector \
  --path /p1:components/p1:component/p1:state \
  --list-path /p1:components/p1:component \
  --selector "name~'1/2/n2/' and type='transceiver'" \
  --interval 30
```

| Flag | Purpose |
|---|---|
| `--path` | *required* — base canonical path to apply the selector to |
| `--list-path` | *required* — list path containing the keyed instances |
| `--selector` | *required* — selector expression, e.g. `name~'1/2/n2/' and type='transceiver'` |
| `--interval <sec>` | *required* — polling interval |
| `--alias` | Treat `--path` as an alias path |

!!! note "Selector syntax"
    Selectors combine clauses with `and`. `=` matches a key exactly; `~` matches a substring.
    The same `parse_selector`/`match_selector` logic backs `doctor` checks (see `pm.py`).

### `curate-jobs`

Apply `metrics:` curation to existing jobs without re-running discovery. Useful after editing
the `metrics.include` patterns in your config.

```bash
splice --config config.yaml curate-jobs
```

Takes no command-specific flags. Uses `metrics.default_interval_sec` (`10`) for any jobs it
(re)creates. Exits with status `2` and a hint if no `metrics:` section with `enabled: true`
and `include` patterns is configured.

### `list-jobs`

List the configured polling jobs. No command-specific flags.

```bash
splice --config config.yaml list-jobs
```

---

## Diagnostics

### `doctor`

Run pre-flight validation checks (connection, catalog, circuit breaker, PM filters, …). No
command-specific flags.

```bash
splice --config config.yaml doctor
```

### `status`

Show health and poller-sweep stats from a running bridge, falling back to database freshness
when the bridge isn't reachable. This command never creates a device row as a side effect — a
missing row just means `setup` hasn't run.

```bash
splice --config config.yaml status
splice --config config.yaml status --json
```

| Flag | Purpose |
|---|---|
| `--json` | Print raw JSON instead of the formatted summary |

### `find-paths`

Search catalog paths by pattern. Supports substring, regex, and embedding-based semantic search.

```bash
splice --config config.yaml find-paths --pattern optical-power
splice --config config.yaml find-paths --pattern '^/p1:components' --regex --case-sensitive
splice --config config.yaml find-paths --pattern "input optical power" --semantic --top 5
```

| Flag | Default | Purpose |
|---|---|---|
| `--pattern` | *required* | The pattern to search for |
| `--regex` | off | Treat the pattern as a regular expression |
| `--case-sensitive` | off | Case-sensitive matching |
| `--semantic` | off | Embedding-based semantic search (requires `sentence-transformers`, i.e. the `search` extra) |
| `--top <n>` | `10` | Number of results for semantic search |

### `export`

Export the catalog or jobs to a file.

```bash
splice --config config.yaml export --what catalog --format json --out catalog.json
splice --config config.yaml export --what jobs --format csv --out jobs.csv
```

| Flag | Choices | Purpose |
|---|---|---|
| `--what` | `catalog`, `jobs` | What to export (*required*) |
| `--format` | `json`, `csv` | Output format (*required*) |
| `--out` | — | Output file path (*required*) |

### `setup-smoke-test`

Run the setup pipeline without serving, validating discovery and expansion. No command-specific
flags.

```bash
splice --config config.yaml setup-smoke-test
```

### `setup-e2e-check`

Verify that one expanded path is actually polled. By default it reads the existing
`setup_report.json`; pass `--run-setup` to regenerate it first.

```bash
splice --config config.yaml setup-e2e-check
splice --config config.yaml setup-e2e-check --run-setup
splice --config config.yaml setup-e2e-check --report ./setup_report.json
```

| Flag | Default | Purpose |
|---|---|---|
| `--run-setup` | off | Run `setup-smoke-test` first (can take a long time) |
| `--report` | next to `--config`, else `./setup_report.json` | Path to `setup_report.json` |

### `explain-setup`

Print a human-readable summary of `setup_report.json`.

```bash
splice --config config.yaml explain-setup
splice --config config.yaml explain-setup --report ./setup_report.json
```

| Flag | Default | Purpose |
|---|---|---|
| `--report` | next to `--config`, else `./setup_report.json` | Path to `setup_report.json` |

### `dashboards`

Generate Grafana dashboards (live telemetry + YANG inventory) from the device's YANG catalog.
Also runs as a best-effort step at the end of `setup`.

```bash
splice --config config.yaml dashboards
splice --config config.yaml dashboards --out grafana --bucket telemetry --measurement telemetry
```

| Flag | Default | Purpose |
|---|---|---|
| `--out` | `dashboards` | Output directory |
| `--bucket` | `gnmi` | InfluxDB bucket referenced by queries |
| `--measurement` | `telemetry` | InfluxDB measurement referenced by queries |

---

## Interfaces

### `mcp`

Start the MCP server on the stdio transport, exposing the bridge as MCP tools for AI agents.
Does not require `--config`.

```bash
splice mcp
splice mcp --policy policy.yaml
splice mcp --audit-log audit.jsonl
```

| Flag | Default | Purpose |
|---|---|---|
| `--policy` | — | Path to a set policy YAML file |
| `--audit-log` | `splice_audit.jsonl` | Path to the audit log file (JSON-lines) |

### `subscribe`

Subscribe to a path prefix and print updates as they arrive. Uses the Python API `Device`;
requires `--config`.

```bash
splice --config config.yaml subscribe --path /p1:components
splice --config config.yaml subscribe --path /p1:components --interval 5 --count 10 --format json
```

| Flag | Default | Purpose |
|---|---|---|
| `--path` | *required* | Path prefix to subscribe to |
| `--interval <sec>` | `10` | Poll interval |
| `--count <n>` | `0` (unlimited) | Number of update batches to print before stopping |
| `--format` | `kv` | Output format: `kv` or `json` |

Press `Ctrl-C` to stop an unlimited subscription; the command prints the number of updates received.

---

## See also

- [Configuration](configuration.md) — the YAML config consumed by `--config`, including the
  `poller`, `metrics`, and `discovery` sections referenced above.
- [Quickstart](quickstart.md) — the `init` → `setup` → `run` workflow end to end.
