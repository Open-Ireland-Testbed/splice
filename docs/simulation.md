# Simulation

Splice ships with a built-in **simulation mode** that runs the entire
discover → fetch-YANG → build-catalog → get/set pipeline against a fake NETCONF
backend — no real hardware, credentials, or network access required. It is the
fastest way to try the CLI, develop against the Python API, drive the MCP
server, or write tests.

!!! tip "Why simulate?"
    Real ROADMs and transponders throttle NETCONF sessions and lock out after
    auth failures. Simulation lets you exercise the full stack — including the
    gNMI gRPC server — with zero risk to lab equipment, and it runs in
    milliseconds because no I/O is performed.

## How it works

A simulation profile is a small class implementing the `DeviceProfile`
protocol (`src/splice/simulator/profiles/base.py`). It supplies:

- a list of advertised **YANG modules** (name / revision / namespace),
- NETCONF **server capabilities**,
- pre-canned XML for `<get>`, `<get-config>`, `<get-schema>`, `<edit-config>`,
  and `ietf-netconf-monitoring` `<schemas>`.

`SimulatedNetconfClient` (`src/splice/simulator/backend.py`) is a drop-in
replacement for the real `NetconfClient`: it exposes the same public methods
(`connect`, `get`, `get_config`, `dispatch`, `edit_config`, `commit`, `lock`,
…), so `NetconfClientPool` and the `Device` class consume it without any
special handling. All methods are thread-safe and return immediately.

The only profile registered today is **`openroadm`**.

## CLI

Select a profile with the global `--simulate PROFILE` flag. Simulation mode is
wired up for the `run` command, which starts a gNMI server backed by the
simulated catalog.

```bash
# Discover the simulated device and serve gNMI on the default port (9339)
splice --simulate openroadm run
```

On startup you will see the pipeline run end-to-end:

```text
Starting simulated device (profile=openroadm)
Connecting to simulated backend...
Catalog: <N> paths discovered
Starting gNMI server on 0.0.0.0:9339
```

### Using a config file for gNMI settings

By default the simulated server binds `0.0.0.0:9339`. Pass `--config` to take
the gNMI `port` and `bind` from your YAML instead — no database is touched, and
the `device` connection section is ignored (the backend is the simulator):

```bash
splice --simulate openroadm --config config.yaml run
```

!!! note
    Only the `gnmi.port` and `gnmi.bind` keys are read in simulation mode. The
    rest of the config (NETCONF device, `db.url`, poller, …) is not used,
    because the simulated client performs no real polling or persistence.

### Verifying the simulated server

With the server running, point any gNMI client at it:

```bash
# Dump everything the simulated catalog exposes
gnmic -a 127.0.0.1:9339 --insecure get --path /

# Read a known optical-power leaf from the OpenROADM device tree
gnmic -a 127.0.0.1:9339 --insecure subscribe --mode once \
  --path '/org-openroadm-device/org-openroadm-device'
```

## Python API

The `Device` class accepts a `sim://<profile>` host URI, or an explicit
`simulate=` keyword. Both activate the same simulated backend.

```python
from splice.api import Device

# sim:// URI form
dev = Device("sim://openroadm")
dev.connect()                         # runs discover + fetch-yang + build-catalog
print(len(dev.catalog_paths), "paths")
updates = dev.get("/org-openroadm-device/org-openroadm-device")
for u in updates:
    print(u["path"], "=", u["value"])
dev.close()
```

The `simulate=` keyword wins over the host string, which is convenient when a
placeholder host is required by a caller:

```python
dev = Device("unused-host", simulate="openroadm")
```

Use it as a context manager to guarantee cleanup:

```python
from splice.api import Device

with Device("sim://openroadm") as dev:
    paths = dev.find_paths("*node-id*")
    status = dev.connection_status      # {"connected": True, ...}
    results = dev.search_paths("optical power", top_k=5)
```

A simulated `Device` supports the full API surface — `get`, `set`,
`subscribe`, `serve`, `find_paths`, `search_paths`, `doctor`,
`connection_status`, and `catalog_paths`. To serve gNMI from Python:

```python
with Device("sim://openroadm") as dev:
    dev.serve(port=9339, blocking=True)   # Ctrl-C to stop
```

!!! tip
    See the [Python API](python-api.md) reference for the complete `Device`
    method set — everything documented there works identically against a
    `sim://` device.

## What the `openroadm` profile provides

The profile (`src/splice/simulator/profiles/openroadm.py`) models an
OpenROADM-compliant ROADM and is realistic enough to drive discovery, catalog
building, telemetry reads, and config writes.

**YANG modules (13):**

| Module | Revision |
|---|---|
| `org-openroadm-device` | 2023-05-26 |
| `org-openroadm-common-types` | 2023-05-26 |
| `org-openroadm-pm` | 2023-05-26 |
| `org-openroadm-alarm` | 2023-05-26 |
| `org-openroadm-interfaces` | 2023-05-26 |
| `org-openroadm-optical-transport-power-specifications` | 2023-05-26 |
| `org-openroadm-lldp` | 2023-05-26 |
| `org-openroadm-common-network` | 2023-05-26 |
| `org-openroadm-resource-types` | 2023-05-26 |
| `org-openroadm-wavelength-map` | 2023-05-26 |
| `ietf-yang-types` | 2013-07-15 |
| `ietf-inet-types` | 2013-07-15 |
| `ietf-netconf-monitoring` | 2010-10-04 |

**Capabilities:** NETCONF base 1.0/1.1, `writable-running`, `candidate`,
`confirmed-commit:1.1`, `rollback-on-error`, `xpath`, and `notification`, plus
per-module capability URIs. This means both writable-running and
candidate+commit set flows can be exercised.

**Telemetry data** returned by `<get>`:

- **Device identity** — `node-id` (`ROADM-SIM-01`), `node-number`, `node-type`
  (`rdm`), `vendor`, `model` (`OpenROADM-v13.1`), `software-version`,
  `ipAddress`.
- **Shelves** — keyed list (`shelf-1`, `shelf-2`) with type and
  operational-state.
- **Degrees** — keyed list with `max-wavelengths` and an `otdr-port`.
- **Shared-risk-groups** — keyed list with add/drop port limits.
- **Interfaces** — keyed list of four ports (`DEG1-TTP-TXRX`, `DEG2-TTP-TXRX`,
  `SRG1-PP1-TXRX`, `SRG1-PP2-TXRX`) each carrying an `optical-power` container
  (`input-power`, `output-power`, `target-output-power`) and an
  `optical-channel` (`frequency`, `width`). One port reports `degraded`
  operational-state, giving you a realistic non-nominal value to query.
- **Alarms** — `active-alarm-list` with one `opticalPowerDegraded` warning on
  `SRG1-PP2-TXRX`.
- **LLDP** — `system-name` and `system-description`.

The backend routes filters by keyword: a filter mentioning `alarm` returns the
alarm tree, `lldp` returns LLDP, `netconf-monitoring` returns the `<schemas>`
list, and anything else returns the full device tree. Writes
(`edit_config`, `commit`, `lock`/`unlock`, `discard_changes`) all return an
`<ok/>` reply.

## Using simulation in tests

Simulation is the backbone of the test suite — `tests/test_simulator.py`,
`tests/test_interface_parity_sim.py`, `tests/test_mcp_integration.py`, and
others all run against `sim://openroadm` with no fixtures or hardware.

```python
def test_full_pipeline():
    from splice.api.device import Device

    with Device("sim://openroadm") as dev:
        assert dev._connected
        assert len(dev.catalog_paths) > 0
        assert dev.find_paths("*node-id*")
```

You can also work with the simulator primitives directly:

```python
from splice.simulator import (
    available_profiles,
    get_profile,
    make_simulated_client,
)

assert "openroadm" in available_profiles()

profile = get_profile("openroadm")          # raises ValueError if unknown
assert profile.name == "openroadm"

client = make_simulated_client("openroadm")  # a SimulatedNetconfClient
client.connect()
reply = client.get("<filter/>")
print(reply.data_xml)
client.close()
```

!!! example "Run the simulator tests"
    ```bash
    .venv/bin/python -m pytest tests/test_simulator.py -v
    ```

## Adding a new profile

A profile is any class satisfying the `DeviceProfile` protocol. The contract
(`src/splice/simulator/profiles/base.py`):

```python
from typing import Dict, List, Optional, Protocol

class DeviceProfile(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def capabilities(self) -> List[str]: ...
    @property
    def modules(self) -> List[Dict[str, object]]: ...   # name/revision/namespace

    def get_response(self, filter_xml: str) -> str: ...
    def get_config_response(self, source: str, filter_xml: str) -> str: ...
    def get_schema_response(self, identifier: str, version: Optional[str]) -> str: ...
    def edit_config_response(self, target: str, config: str) -> str: ...
    def netconf_monitoring_xml(self) -> str: ...
```

!!! warning "Return well-formed replies"
    Every `*_response` method must return a complete
    `<rpc-reply><data>...</data></rpc-reply>` document parseable by
    `parse_xml_to_kv()`. `get_schema_response` must return valid (if minimal)
    YANG module text so `build_catalog_in_memory()` can build a catalog — and
    that text must be XML-escaped inside the `<data>` element. Use the
    OpenROADM profile as a template.

**Steps:**

1. Create `src/splice/simulator/profiles/<myprofile>.py` implementing the
   protocol. Define your YANG module list, capabilities, YANG schema stubs, and
   pre-canned XML responses.
2. Register it in `src/splice/simulator/__init__.py`:

   ```python
   from splice.simulator.profiles.myprofile import MyProfile

   _PROFILES: Dict[str, Type] = {
       "openroadm": OpenROADMProfile,
       "myprofile": MyProfile,
   }
   ```

3. It is now usable everywhere: `splice --simulate myprofile run`,
   `Device("sim://myprofile")`, and `get_profile("myprofile")`.
   `available_profiles()` will list it.

!!! tip "Schema fallback"
    If `get_schema_response` is asked for an identifier you did not define a
    full schema for, return a minimal stub module (matching namespace + prefix)
    so the YANG fetch does not fail hard. The OpenROADM profile demonstrates
    this fallback.

## What simulation does and does not emulate

| Emulated | Not emulated |
|---|---|
| YANG module discovery via `ietf-netconf-monitoring` schemas | Real on-device datastore state or live value changes over time |
| `<get-schema>` YANG download and catalog build | True YANG validation of edits (writes always return `<ok/>`) |
| `<get>` / `<get-config>` telemetry reads with realistic OpenROADM data | Network latency, session limits, or auth lockouts |
| `<edit-config>`, `commit`, `lock`/`unlock`, `discard-changes` (all `<ok/>`) | Persistence — edits do not mutate subsequent `<get>` responses |
| NETCONF capabilities (writable-running + candidate) | NETCONF event notifications / subscriptions (`take_notification` returns `None`) |
| Keyed lists, nested containers, alarms, LLDP | PM history accumulation; values are static snapshots |

!!! note
    Because responses are static, telemetry values do not drift between polls.
    Simulation validates plumbing, path matching, catalog construction, and the
    gNMI/MCP surfaces — not time-series dynamics.

## See also

- [CLI Reference](cli.md) — the `run` command and global flags
- [Python API](python-api.md) — the full `Device` interface
- [MCP Server](mcp.md) — drive the simulator from an AI agent
- [Configuration](configuration.md) — `gnmi.port` / `gnmi.bind` used with `--config`
