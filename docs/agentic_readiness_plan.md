# Agentic Readiness Plan — Splice

## Vision

Make splice a first-class tool for AI agents: coding agents can develop and test against it without real hardware, operations agents can manage devices through MCP tools, and orchestration agents can integrate it into autonomous network workflows.

## Agent Types & Interfaces

| Agent Type | Interface | What It Does |
|---|---|---|
| **Coding Agent** (Claude Code, Copilot) | Filesystem, tests, CLAUDE.md | Develops and extends the bridge codebase |
| **Operations Agent** (LLM with tools) | MCP Server (JSON-RPC) | Reads telemetry, pushes config, diagnoses devices |
| **Orchestration Agent** (Python loop / LLM) | Python SDK (`splice.api`) or MCP | Autonomous network control (digital twin, ML-driven reconfiguration) |

---

## Phase 1: Simulation Mode

**Goal**: Enable the full pipeline (connect → discover → get → set) against a fake NETCONF device so agents and developers can work without real hardware.

### What to build
- A `SimulatedDevice` backend that implements the same interface as `NetconfClient`
- Pre-canned YANG modules and XML responses for 1-2 device profiles (e.g., OpenROADM, generic router)
- Configurable failure injection (auth failure, timeout, malformed XML) for testing agent error handling
- CLI flag: `splice --simulate openroadm run`

### Why first
- Unblocks all other phases — MCP development, coding agent testing, CI without hardware
- Lets agents safely experiment with Set operations
- Enables reproducible demos

### Open questions
- Which device profiles to simulate first? (OpenROADM seems most relevant to the testbed)
- How realistic do simulated responses need to be? (Static snapshots vs. values that drift over time)

---

## Phase 2: Error Taxonomy

**Goal**: Every error carries enough structured metadata for an agent to decide: retry, wait, try alternative, or escalate.

### What to build
- Enrich `api/_errors.py` with:
  - `error_code: str` — machine-readable (e.g., `AUTH_LOCKOUT`, `SESSION_LIMIT`, `DEVICE_UNREACHABLE`, `PATH_NOT_FOUND`, `SET_REJECTED`, `CIRCUIT_BREAKER_OPEN`)
  - `retryable: bool` — can the agent retry this?
  - `retry_after_sec: Optional[float]` — how long to wait before retrying
  - `remediation_hint: str` — human/agent-readable suggestion
- Structured error responses in both SDK exceptions and MCP tool results
- Map existing failure modes:

| Error Code | Retryable | Retry After | Remediation |
|---|---|---|---|
| `AUTH_LOCKOUT` | Yes | 300s | ADVA locks out after failed auth — wait 5 min |
| `SESSION_LIMIT` | Yes | 30s | Device concurrent session limit reached |
| `DEVICE_UNREACHABLE` | Yes | 10s | Network issue — check connectivity |
| `CIRCUIT_BREAKER_OPEN` | Yes | cooldown | Too many consecutive failures — backing off |
| `PATH_NOT_FOUND` | No | — | Path not in catalog — use find_paths to discover valid paths |
| `SET_REJECTED` | No | — | Device rejected config edit — check value/path validity |
| `DISCOVERY_TIMEOUT` | Yes | 30s | YANG fetch took too long — device may be overloaded |

---

## Phase 3: MCP Server

**Goal**: Expose splice as an MCP server so LLM-based agents can call tools directly without shelling out to CLI.

### Architecture
```
splice run (persistent process)
  ├── gNMI gRPC server (port 9339, for gnmic/Telegraf)
  ├── MCP Server (stdio or SSE transport)
  │     └── wraps Device sessions, LiveCache, catalog
  └── Device sessions (auto-reconnect, circuit breaker)
```

### MCP Tools to expose

| Tool | Description | Maps to |
|---|---|---|
| `connect_device` | Connect to a NETCONF device, returns device_id handle | `Device.connect()` |
| `disconnect_device` | Close a device session | `Device.close()` |
| `get_telemetry` | Read current value(s) for a path or pattern | `Device.get()` |
| `set_config` | Push configuration (with safety checks) | `Device.set()` |
| `find_paths` | Search catalog paths by pattern or semantics | `Device.find_paths()` / `path_index` |
| `list_devices` | Show connected devices and their status | New |
| `device_health` | Connection state, circuit breaker status, last poll time | `health.py` |
| `list_capabilities` | Return writable vs readable paths, value types | New (from catalog) |

> **Note**: Discovery is handled implicitly by `connect_device` (via `skip_discover=False` default). A standalone `discover` tool may be added later for re-discovery without reconnecting.

### Statefulness model (implemented)
- MCP server manages a `SessionPool` of `Device` sessions internally
- Agent gets back a `device_id` handle from `connect_device`
- Sessions auto-reconnect on failure (existing reconnect-once logic)
- Sessions expire after configurable idle timeout
- Audit logging records every tool call with timestamps, sanitized params, and duration

### Transport (implemented)
- **stdio** for local use (Claude Code, single-agent)
- SSE for remote/multi-agent use is planned but not yet implemented

---

## Phase 4: Safety & Guardrails

**Goal**: Prevent agents from damaging real hardware or flooding devices.

### Rate Limiting
- Max NETCONF RPCs per minute per device (extend existing poller throttling to API/MCP layer)
- Configurable in YAML:
  ```yaml
  safety:
    max_rpc_per_minute: 30
    max_concurrent_sessions: 3
  ```

### Set Policy
- Define allowed/forbidden write paths and value constraints:
  ```yaml
  set_policy:
    allowed_paths:
      - "/interfaces/interface/*/admin-status"
      - "/org-openroadm-device:*/degree/*/target-output-power"
    forbidden_paths:
      - "/system/**"
    value_constraints:
      "target-output-power":
        min: -20.0
        max: 5.0    # prevent dangerously high optical power
  ```
- **Dry-run mode**: Validate config against policy without applying (`set_config(dry_run=True)`)
- MCP `set_config` tool validates against policy before calling `Device.set()`

### Audit log
- Every MCP tool call logged with timestamp, agent identity (if available), device, action, result
- Enables post-incident analysis: "which agent set the power to +10 dBm?"

---

## Implementation Order & Dependencies

```
Phase 1: Simulation Mode
  │   (no dependencies — can start immediately)
  │
  ├── Phase 2: Error Taxonomy
  │     (independent — can run in parallel with Phase 1)
  │
  └── Phase 3: MCP Server
        (benefits from Phase 1 for testing, Phase 2 for error responses)
        │
        └── Phase 4: Safety & Guardrails
              (builds on MCP layer)
```

## Out of Scope (for now)
- Multi-device orchestration primitives (transactions across devices)
- Agent authentication/authorization (which agent can access which device)
- Natural language path queries ("show me optical power on all line ports")
- Webhook/callback notifications (agent gets notified when a value changes)

These could be future phases once the foundation is solid.
