# Python API Reference

The `Device` class is Splice's programmatic, **no-database** API. It wraps the
same NETCONF discovery, catalog-building, and polling core used by the CLI, but
keeps all state in memory — no PostgreSQL, no config files required. This makes
it ideal for scripting, notebooks, tests, and AI-agent workflows.

Construct a `Device` against real hardware (`Device(host, username=...,
password=...)`) or a hardware-free simulator (`Device("sim://openroadm")`), then
discover paths, read telemetry, push config, or start a gNMI server — all from
Python.

## Usage

```python
from splice.api.device import Device

# Hardware-free: run the full pipeline against a simulated OpenROADM device.
with Device("sim://openroadm") as dev:
    dev.discover()            # NETCONF capabilities → YANG modules
    dev.fetch_yang()          # download module sources
    dev.build_catalog()       # build the in-memory path catalog

    for path in dev.find_paths("optical-power"):
        print(path, dev.get(path))
```

The context manager (`with ...`) opens the NETCONF session on entry and closes
it on exit. See the full method reference below.

::: splice.api.device.Device
    handler: python
    options:
      show_root_heading: true
      show_source: false
      members_order: source
