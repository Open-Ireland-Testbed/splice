# MCP Server

Splice ships a [Model Context Protocol](https://modelcontextprotocol.io) server that exposes the bridge as a set of tools for AI agents (Claude Code, Claude Desktop, and any MCP-capable client). It runs over **stdio**, requires **no database and no YAML config**, and is built directly on the [Python API](python-api.md) — an agent can connect to a NETCONF device, read telemetry, search the YANG catalog, and (subject to policy) push configuration, all through structured tool calls.

!!! note "Install the extra"
    The MCP server depends on the `mcp` package. Install it with the `mcp` extra:
    ```bash
    .venv/bin/pip install -e ".[mcp]"
    ```

## Starting the server

The server is launched by the `splice mcp` subcommand. Unlike most commands, `mcp` does **not** require `--config` — it manages its own in-memory device sessions.

```bash
# Plain server on stdio
splice mcp

# With a set policy (controls what set_config may write)
splice mcp --policy policy.yaml

# With an audit log of every tool call (JSON-lines)
splice mcp --audit-log audit.jsonl

# Both together
splice mcp --policy policy.yaml --audit-log audit.jsonl
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--policy` | none (deny-all) | Path to a set-policy YAML file. Without it, **all writes are denied**. |
| `--audit-log` | `splice_audit.jsonl` | Path to the JSON-lines audit file. Every tool call is appended here (and to Python logging). |

!!! warning "Run it from your MCP client, not by hand"
    `splice mcp` speaks the MCP protocol on stdin/stdout — it is not interactive. Start it from an MCP client (see [Wiring into Claude Code](#wiring-into-claude-code)) rather than from a normal shell.

## The tools

The server registers **11 tools**. Every agent workflow starts with `connect_device`, which returns a `device_id` handle that all other tools take as their first argument.

| Tool | Purpose | Key parameters |
|------|---------|----------------|
| `connect_device` | Connect to a NETCONF device (real or simulated), run discovery + catalog build, return a `device_id`. | `host`, `port` (830), `username` (`admin`), `password`, `device_id`, `yang_dir`, `skip_discover`, `skip_fetch_yang`, `skip_catalog`, `simulate` |
| `disconnect_device` | Close a session and release resources. | `device_id` |
| `get_telemetry` | Read current values for a path or glob via a direct NETCONF get. | `device_id`, `path` |
| `set_config` | Push a config value via NETCONF edit-config. Subject to the set policy. | `device_id`, `path`, `value`, `operation` (`merge`), `dry_run` (`False`) |
| `find_paths` | Glob-match the catalog and return canonical paths. | `device_id`, `pattern` |
| `search_paths` | Natural-language search over the catalog (hybrid BM25 + embeddings). | `device_id`, `query`, `top_k` (10) |
| `subscribe_once` | One-shot read of all values under a path prefix (gNMI Subscribe ONCE equivalent). | `device_id`, `path` |
| `list_devices` | List all connected sessions with status, idle/uptime, catalog size. | — |
| `device_health` | Connection state, circuit-breaker status, and timing for one device. | `device_id` |
| `doctor` | Diagnostic checks (connection, circuit breaker, catalog, cache) plus stats. | `device_id` |
| `list_capabilities` | Readable vs. writable catalog paths, optionally filtered. | `device_id`, `pattern` (`*`) |

### connect_device

Connecting runs the full discovery pipeline (NETCONF session → module discovery → YANG fetch → catalog build), which can take a few minutes on large devices. Use the `skip_*` flags to reuse cached schemas and start instantly:

- `yang_dir` — a directory of cached `.yang` files.
- `skip_fetch_yang=true` — don't re-fetch schemas (requires `yang_dir` populated).
- `skip_discover=true` — don't re-discover modules.
- `skip_catalog=true` — defer catalog build (then `find_paths` may return empty).

The `simulate` parameter (e.g. `"openroadm"`) connects to a built-in simulated backend instead of real hardware — no device needed. The agent may also pass `host="sim://openroadm"`, which is recognized and treated the same way.

!!! example "Connecting to the simulator"
    An agent calls `connect_device(simulate="openroadm")` and gets back:
    ```json
    {
      "device_id": "sim_openroadm_1a2b3c4d",
      "status": "connected",
      "host": "sim://openroadm",
      "port": 830,
      "catalog_paths": 1234
    }
    ```

### set_config and dry_run

`set_config` supports `operation` values `merge` (default), `replace`, and `delete`. Pass `dry_run=true` to run the **policy check only** — the tool returns `"policy_check": "passed"` without touching the device. This lets an agent verify a write is permitted before committing to it.

Every form a path could resolve to (raw, predicate-stripped, alias-resolved) is checked against the policy, fail-closed: a write is **forbidden** if *any* form matches a forbidden pattern, and **allowed** only if *every* form is in the allowed list.

## Safety

The MCP surface is designed to be handed to an autonomous agent, so writes and request rates are gated.

### Set policy (deny-all by default)

`set_config` is governed by a `SetPolicy`. **With no `--policy` file, the policy is deny-all** — every write is rejected with `SET_POLICY_DENIED` and the message `no paths are allowed (default deny-all policy)`. To permit writes you must explicitly allow paths.

A policy YAML supports three keys:

| Key | Meaning |
|-----|---------|
| `allowed_paths` | Glob patterns (`fnmatch`) a path must match to be writable. Empty ⇒ deny all. |
| `forbidden_paths` | Glob patterns that are always denied — **forbidden overrides allowed**. |
| `value_constraints` | Per-path-pattern numeric bounds (`min` / `max`) checked against the value. |

The file may put these under a top-level `set_policy:` block, or at the document root.

!!! example "policy.yaml"
    ```yaml
    set_policy:
      allowed_paths:
        - "/interfaces/interface[*]/config/description"
        - "/components/component[*]/*/config/target-output-power"
      forbidden_paths:
        - "*/admin-state"          # never let the agent shut a port
        - "*/reset"
      value_constraints:
        "/components/component[*]/*/config/target-output-power":
          min: -20.0
          max: 3.0
    ```
    With this policy an agent may set a description or an in-range output power, but any attempt to write `admin-state`, or an output power above `3.0`, is denied before reaching the device.

!!! tip "Validate first"
    Have the agent call `set_config(..., dry_run=true)` to confirm the policy permits a write, then repeat without `dry_run` to apply it.

### Rate limiting

Each device is rate-limited to **30 RPCs per minute** over a sliding 60-second window (configurable via `create_mcp_server(max_rpcs_per_minute=...)`). The limiter applies to the RPC-issuing tools — `get_telemetry`, `set_config`, and `subscribe_once`. When the limit is hit, the tool returns a `RATE_LIMITED` error carrying `retry_after_sec` (the seconds until the oldest call ages out of the window).

### Idle session cleanup

Sessions idle for longer than **30 minutes** are closed automatically; cleanup is piggybacked on every tool invocation. A `device_id` whose session has been reclaimed yields `DEVICE_NOT_CONNECTED` on its next use — the agent should `connect_device` again.

### Audit log

When `--audit-log` is set (default `splice_audit.jsonl`), every tool call appends one JSON object: timestamp, tool name, `device_id`, parameters, status, duration, and `error_code` on failure. **Sensitive parameter values are redacted** — any key named `password`, `passwd`, `secret`, `token`, or `credential` is written as `***REDACTED***`.

```json
{"timestamp": "2026-06-30T12:00:00+00:00", "tool": "connect_device", "device_id": "sim_openroadm_1a2b3c4d", "params": {"host": "sim://openroadm", "password": "***REDACTED***"}, "status": "success", "duration_ms": 842.13}
```

## Error taxonomy

Tools never raise across the MCP boundary; on failure they return a structured error object. Every error carries:

- `error_code` — one of the codes below.
- `error: true` and `status: "error"`.
- `message` — the underlying detail.
- `retryable` — whether retrying may succeed.
- `retry_after_sec` — suggested wait before retry (`null` when not retryable).
- `remediation_hint` — a human/agent-readable next step.

| `error_code` | Retryable | `retry_after_sec` | Meaning / hint |
|--------------|-----------|-------------------|----------------|
| `AUTH_LOCKOUT` | yes | 300 | Device locked out after auth failures. Wait 5 minutes. |
| `SESSION_LIMIT` | yes | 60 | Device hit its max concurrent session limit. Close sessions or wait. |
| `DEVICE_UNREACHABLE` | yes | 10 | Network connectivity problem reaching the device. |
| `CIRCUIT_BREAKER_OPEN` | yes | 60 | Breaker open after repeated failures; wait for cooldown. |
| `PATH_NOT_FOUND` | no | — | Path not in the catalog. Use `find_paths` to locate valid paths. |
| `SET_REJECTED` | no | — | Device rejected the write (not writable, or bad value). |
| `SET_POLICY_DENIED` | no | — | Blocked by the set policy. Adjust `allowed_paths`. |
| `DISCOVERY_TIMEOUT` | yes | 30 | Discovery too slow. Retry with `skip_discover=true`. |
| `DEVICE_NOT_CONNECTED` | no | — | No live session for that `device_id`. Call `connect_device` first. |
| `RATE_LIMITED` | yes | (dynamic) | Too many RPCs this window; `retry_after_sec` gives the exact wait. |

!!! note "Semantic search availability"
    `search_paths` always works in keyword-only (BM25) mode. If no embedding provider is installed, the result includes a `note` recommending `pip install splice[search]` for semantic ranking — it does **not** error.

## Wiring into Claude Code

Register Splice as an MCP server in your client's MCP configuration. The command is `splice mcp`; pass `--policy` / `--audit-log` as extra `args`. Use the path to the `splice` entry point inside your virtualenv (or `python -m splice.cli` if not installed as a script).

```json
{
  "mcpServers": {
    "splice": {
      "command": "/path/to/.venv/bin/splice",
      "args": ["mcp", "--policy", "/path/to/policy.yaml", "--audit-log", "/path/to/audit.jsonl"]
    }
  }
}
```

!!! tip "Start with the simulator"
    Before pointing an agent at real hardware, have it `connect_device(simulate="openroadm")`. It exercises the full tool surface — discovery, catalog search, telemetry, and policy-gated writes — with no device and no risk.

## A typical agent flow

1. `connect_device(host="192.0.2.10", username="admin", password="…")` → `device_id`.
2. `search_paths(device_id, "optical receive power")` or `find_paths(device_id, "*/components/*")` to locate paths.
3. `get_telemetry(device_id, path)` / `subscribe_once(device_id, path)` to read values.
4. `list_capabilities(device_id, pattern)` to see which paths are writable.
5. `set_config(device_id, path, value, dry_run=true)` to validate, then again without `dry_run` to apply (policy permitting).
6. `doctor(device_id)` / `device_health(device_id)` to check connection and circuit-breaker state.
7. `disconnect_device(device_id)` when done.

## See also

- [Python API](python-api.md) — the `Device` class the MCP tools wrap.
- [Configuration](configuration.md) — YAML config for the CLI/server stacks.
- [CLI Reference](index.md) — the full `splice` command surface.
- [Simulation](quickstart.md) — running against the built-in simulated device.
