# Installation

Splice installs as a single Python package that ships the `splice` CLI and the optional `splice-tui` console. This page covers prerequisites, a from-source editable install, every optional extra, and how to verify the result.

## Prerequisites

| Requirement | Needed for | Notes |
|---|---|---|
| **Python ≥ 3.10** | Everything | `requires-python = ">=3.10"` in `pyproject.toml`. |
| **PostgreSQL 16** | The CLI + Database stack only | Used for the persistent catalog, jobs, and telemetry history. Not required for the Python API, simulation, or the MCP server. |
| A C toolchain / build deps | Building some wheels | Most dependencies ship binary wheels (`psycopg[binary]`, `grpcio`), so a compiler is usually unnecessary. |

!!! note "No database? No problem"
    The [Python API](python-api.md), [simulation mode](#verifying-the-install), and the MCP server run entirely in memory with **no PostgreSQL and no config files**. You only need a database when you use the `splice --config ... setup | run` CLI stack, which persists the catalog, jobs, and history.

The core install always pulls these runtime dependencies (from `pyproject.toml`):

- `sqlalchemy>=2.0`, `psycopg[binary]>=3.1` — ORM and PostgreSQL driver
- `pyyaml>=6.0` — config parsing
- `pyang>=2.7.1` — YANG model parsing
- `grpcio>=1.66`, `grpcio-tools>=1.66` — the gNMI/gRPC server
- `ncclient>=0.6`, `paramiko>=3,<4` — NETCONF over SSH
- `defusedxml>=0.7` — hardened XML parsing

!!! warning "Why paramiko is pinned to `>=3,<4`"
    paramiko 4.x **removed the SHA-1 key-exchange algorithms** (`diffie-hellman-group14-sha1`, `diffie-hellman-group1-sha1`) that many optical and legacy NETCONF devices still require. Splice pins paramiko to the 3.x line so the SSH transport can negotiate with them. If you install paramiko separately, keep it below 4.0 or the bridge may fail to connect to older gear.

## Install from source

The quickest way is from PyPI, where the package is called `splice-gnmi` (the command and the Python import are both `splice`):

```bash
pip install splice-gnmi
pip install "splice-gnmi[tui,mcp]"   # with the optional console and AI-agent interface
```

To work on Splice itself, install it editable (`-e`) from a clone of the repository:

```bash
git clone https://github.com/Open-Ireland-Testbed/splice.git
cd splice

python -m venv .venv
source .venv/bin/activate

pip install -e .            # core CLI + gNMI server
```

This registers two console entry points (see `[project.scripts]`):

| Command | Entry point | Provided by |
|---|---|---|
| `splice` | `splice.cli:main` | Core install |
| `splice-tui` | `splice.tui:main` | Requires the `[tui]` extra |

!!! tip "Without an editable install"
    If you prefer not to install the package, you can still run the CLI directly from the source tree:

    ```bash
    PYTHONPATH=src python src/splice/cli.py --help
    ```

## Optional extras

Splice keeps heavy or specialized dependencies out of the core install. Add them with the standard `pip install -e ".[extra]"` syntax. Extras can be combined: `pip install -e ".[tui,mcp,search]"`.

| Extra | Installs | Enables |
|---|---|---|
| `tui` | `textual>=2.0` | The interactive `splice-tui` console — see the [TUI Guide](tui.md). |
| `mcp` | `mcp>=1.0` | The MCP server (`splice mcp`) that exposes the bridge as tools for AI agents. |
| `search` | `sentence-transformers>=2.2`, `numpy>=1.24` | Local semantic path search (embeddings) blended into hybrid catalog search. |
| `openai` | `openai>=1.0`, `numpy>=1.24` | Semantic path search via OpenAI embeddings instead of local models. |
| `kafka` | `confluent-kafka>=2.3` | Publishing telemetry and trigger events to Kafka. |
| `docs` | `mkdocs-material>=9.5`, `mkdocstrings[python]>=0.24`, `pymdown-extensions>=10` | Building this documentation site locally. |

```bash
# Common combinations
pip install -e ".[tui]"             # interactive console
pip install -e ".[mcp]"             # AI-agent interface
pip install -e ".[search]"          # local semantic search
pip install -e ".[tui,mcp,search]"  # full developer setup
```

!!! note "Search modes degrade gracefully"
    Keyword search (BM25) is **always available** with the core install — it needs no extra. Installing `[search]` or `[openai]` adds embedding-based semantic ranking that blends into the default `hybrid` search mode. Without either extra, hybrid search transparently falls back to BM25.

!!! tip "Choosing `search` vs `openai`"
    Pick `search` for a fully local, offline embedding model (`sentence-transformers`). Pick `openai` if you would rather call the OpenAI embeddings API and avoid downloading model weights. Installing both is unnecessary.

## Verifying the install

Confirm the CLI is on your path:

```bash
splice --help
```

You should see the full command list (`init`, `setup`, `run`, `doctor`, `find-paths`, `mcp`, and more). Every command except `init` and `mcp` requires a `--config` file.

The fastest end-to-end check needs **no hardware and no database** — run Splice against the bundled simulated OpenROADM device:

```bash
splice --simulate openroadm run
```

In another terminal, query it like any real gNMI server:

```bash
gnmic -a localhost:9339 --insecure capabilities
gnmic -a localhost:9339 --insecure get --path '/'
```

Or read a value straight from the simulated device through the Python API, which exercises the install without starting a server:

```python
from splice.api.device import Device

with Device("sim://openroadm") as dev:
    print(dev.get("/org-openroadm-device/node-id"))
```

If you installed the TUI extra, verify its entry point too:

```bash
splice-tui --devices-dir devices
```

## Database setup (CLI stack only)

When you move from simulation to the persistent CLI workflow, point `db.url` in your config at a PostgreSQL 16 instance. The schema is created by the bundled migrations, which run automatically as part of `setup` (or on their own via `migrate`):

```bash
splice --config config.yaml setup     # migrate + discover + catalog + jobs + dashboards
```

See [Configuration](configuration.md) for the `db.url` format and the rest of the config reference, and [Quickstart](quickstart.md) for the first-device walkthrough.

## Running in containers

For container and production deployment, Splice ships a `Dockerfile` and a `docker-compose.yml` (bundling PostgreSQL). Those are covered in [Deployment](deployment.md) rather than here.

## Next steps

- [Quickstart](quickstart.md) — onboard your first real device.
- [Configuration](configuration.md) — every config section, with examples and defaults.
- [TUI Guide](tui.md) — the interactive operator console.
- [Python API](python-api.md) — the database-free `Device` class.
