# Changelog

All notable changes to Splice are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Planned for 0.2.0. These changes came out of running a week-long, agent-supervised
hardware campaign on Lumentum ROADMs through Splice.

### Added
- `Device.set_many([(path, value), ...])` writes several leaves in one `<edit-config>`,
  so the device applies them together. `Device.set` now uses it.
- `Device.rpc(xml)` sends device-defined YANG RPCs (for example, re-arming automatic
  laser shutdown on a Lumentum ROADM).
- MCP `connect_device` accepts `password_file`, so credentials stay out of the agent's
  context and the audit log.
- Kafka publisher: `extra_config`, `publish_event(...)`, and counting of failed deliveries
  (a missing topic used to fail silently).
- Demo assets: a multi-vendor telemetry wall (five bridges → Telegraf → InfluxDB →
  generated Grafana dashboard), a runbook, and a recording of an agent-driven control step
  over MCP.

### Fixed
- Float values were corrupted on write: `Device.set(path, 0.2)` was sent as
  `0.20000000298023224`, which `decimal64` leaves reject. Floats are now sent as doubles and
  written as plain decimals.
- Keyed reads lost their list keys on devices that only return key leaves when asked for
  them (seen on Lumentum). Filters now select the key leaves.
- The MCP server now falls back to the `mcp` 2.x import path for `FastMCP`.

## [0.1.1] - 2026-10-21

First release on PyPI: `pip install splice-gnmi`.

### Changed
- Published to PyPI as `splice-gnmi` (the name `splice` was already taken). The command
  and the Python import are still `splice`.
- Added project links, keywords and classifiers to the package metadata.
- Added a release workflow that publishes to PyPI when a GitHub release is published.

### Fixed
- `pip install ".[mcp]"` picked up `mcp` 2.x, which renamed `FastMCP` and broke the MCP
  server (59 tests failed). The extra is now pinned to `mcp>=1.0,<2`.

## [0.1.0] - 2026-06-30

First public release.

### Added
- gNMI server (Capabilities, Get, Set, Subscribe in ONCE, POLL and STREAM modes) in front of
  any NETCONF/YANG device, with no vendor-specific code.
- `splice quickstart --host <ip>`: discovers the device's YANG models, builds a telemetry
  catalog, and starts serving gNMI in one command.
- Instance-keyed performance-monitoring paths, so per-port metrics stay distinct.
- Resilience for fragile devices: reconnect-once, circuit breaker, and detection of auth
  failures and session limits to avoid account lockouts.
- Curated polling with include patterns and per-pattern intervals.
- Generated Grafana dashboards (live telemetry and YANG inventory) for each device.
- Hybrid path search (BM25 keyword search, plus semantic search when the `search` or
  `openai` extra is installed).
- Hardware-free simulation (`--simulate openroadm`).
- Python API (`Device("sim://openroadm")`) that needs no database or config files.
- MCP server for AI agents, with a deny-by-default set policy, rate limiting, audit log and
  structured errors.
- Interactive terminal console (`splice-tui`).
- On-demand trigger API and optional Kafka publishing.
- Documentation site at <https://splice-gnmi.github.io>.
- Dual licence: AGPL v3.0-or-later, or a commercial licence.

### Tested on
- Lumentum ROADM, Adtran Teraflex and Juniper ILA, on the Open Ireland testbed.

[Unreleased]: https://github.com/Open-Ireland-Testbed/splice/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Open-Ireland-Testbed/splice/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Open-Ireland-Testbed/splice/releases/tag/v0.1.0
