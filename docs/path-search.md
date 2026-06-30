# Finding Paths

A real device catalog can hold tens of thousands of YANG paths. Splice gives you three ways to find the one you want: fast substring/regex matching, ranked hybrid search (keyword + semantic), and a hierarchical tree browser — exposed through the CLI, the Python API, and the MCP server.

!!! tip "Where do paths come from?"
    Paths are the **canonical, module-qualified** YANG paths Splice discovers from the device — e.g. `/org-openroadm-device:org-openroadm-device/circuit-packs`. Each one carries metadata (keyword, data type, config flag, module, description, list keys) that search ranks against. See [Architecture](architecture.md) for how the catalog is built.

## At a glance

| Tool | What it does | Needs |
|------|--------------|-------|
| `splice find-paths --pattern P` | Substring match over catalog paths | nothing |
| `splice find-paths --pattern P --regex` | Regex (`re.search`) match | nothing |
| `splice find-paths --pattern Q --semantic` | Ranked hybrid search (BM25 + embeddings) | `[search]` or `[openai]` for the semantic half |
| `device.find_paths(glob)` (Python) | Glob match over paths | nothing |
| `device.search_paths(query)` (Python) | Ranked hybrid search | as above |
| `find_paths` / `search_paths` (MCP) | Same, for AI agents | as above |

## Substring and regex matching

The default `find-paths` mode is a plain text filter over every catalog path. No extra dependencies, no index build — it just scans.

```bash
# Case-insensitive substring (default)
splice --config config.yaml find-paths --pattern optical-power

# Case-sensitive
splice --config config.yaml find-paths --pattern PreFEC --case-sensitive

# Regular expression (Python re.search semantics)
splice --config config.yaml find-paths --pattern 'interfaces?/.*counters' --regex
```

Flags for the matching modes:

| Flag | Effect | Default |
|------|--------|---------|
| `--pattern` | The text or regex to match (required) | — |
| `--regex` | Treat `--pattern` as a regular expression | off (substring) |
| `--case-sensitive` | Match case exactly | off (case-insensitive) |

Matches are printed one per line, sorted alphabetically. An invalid regex prints `invalid regex: ...` and exits with code `2`.

!!! note "Matching is over the canonical path string"
    Substring and regex match against the full module-qualified path text. If you don't know the module prefix, search for a bare leaf or container name (`circuit-pack-name`, `optical-power`) rather than guessing the prefix.

## Hybrid search (`--semantic`)

`--semantic` switches `find-paths` from literal matching to a **ranked** search. Instead of a yes/no filter, every path gets a relevance score and the top results are returned best-first. This is the right tool when you know *what you want* but not *what it's called* — e.g. searching `optical signal quality` and getting Q-factor and OSNR paths back.

```bash
# Ranked search, top 10 by default
splice --config config.yaml find-paths --pattern "bit error rate" --semantic

# Ask for more results
splice --config config.yaml find-paths --pattern "laser temperature" --semantic --top 25
```

`--top N` controls how many ranked results are returned (default `10`). Output is `score  path`, highest score first:

```text
Building search index over 18423 paths...
Index cached for future searches.

  0.812  /org-openroadm-device:org-openroadm-device/circuit-packs/.../pre-fec-bit-error-rate
  0.744  /org-openroadm-device:org-openroadm-device/.../uncorrected-bit-error-rate
  ...
```

### How the ranking works

Under the hood `--semantic` runs the **hybrid** scoring mode, which blends two signals:

- **BM25 keyword ranking** — a classic information-retrieval score (Okapi BM25, `k1=1.2`, `b=0.75`), computed in pure Python. Always available, no extra dependencies. It indexes each path's segments (namespace prefixes stripped, `-`/`_` turned into spaces), keyword, description, module, data type, and list keys.
- **Embedding cosine similarity** — a semantic score from a vector embedding of each path. Only contributes when an embedding provider is installed.

The two are normalized and blended (BM25 weighted `0.35`, embeddings `0.65` by default). If no embedding provider is installed, hybrid mode degrades gracefully to **BM25-only** and prints a hint:

```text
No embedding provider installed; ranking by keyword (BM25) match only.
For semantic ranking: pip install -e '.[search]'
```

So `--semantic` is always usable — it just gets smarter (true synonym/concept matching) once you add embeddings.

!!! note "The three scoring modes"
    The underlying `PathIndex.search()` supports `mode="hybrid"` (default), `"bm25"` (keyword only), and `"semantic"` (embeddings only — raises if no provider is installed). The CLI `--semantic` flag always uses **hybrid**, which is the safe default. The pure-`bm25` and pure-`semantic` modes are reachable through the [Python API](python-api.md).

### Index caching

Embedding every path is slow, so the first `--semantic` run builds an index and caches it to disk next to your YANG directory:

```text
<parent-of-yang_dir>/.search_cache/path_index.json   # metadata + entries
<parent-of-yang_dir>/.search_cache/path_index.npy    # embedding vectors
```

The cache is fingerprinted by the sorted set of catalog paths (MD5). If the catalog changes — you re-run `setup` and discover new paths — the fingerprint changes and the index is rebuilt automatically. BM25 statistics are *not* cached; they rebuild from the entries in milliseconds on load.

!!! tip "First run is the slow one"
    The first semantic search pays the embedding cost (and, with the local provider, a model download). Subsequent searches load the cached `.npy` and are fast. Delete `.search_cache/` to force a clean rebuild.

## Installing embedding providers

The semantic half of hybrid search is optional. Pick one of two extras (see [Installation](installation.md)):

```bash
# Local, offline embeddings via sentence-transformers (model: all-MiniLM-L6-v2)
pip install -e '.[search]'

# OpenAI / OpenAI-compatible API embeddings (model: text-embedding-3-large)
pip install -e '.[openai]'
```

| Extra | Provider | Default model | Network |
|-------|----------|---------------|---------|
| `[search]` | `sentence-transformers` (local) | `all-MiniLM-L6-v2` | downloads model once, then offline |
| `[openai]` | OpenAI API or compatible endpoint | `text-embedding-3-large` | per-query API calls |

Both extras pull in `numpy`.

### Choosing and configuring a provider

Splice auto-detects a provider, with this priority:

1. `GNMI_EMBEDDING_PROVIDER=openai` → use OpenAI embeddings
2. `GNMI_EMBEDDING_PROVIDER=local` → use local sentence-transformers
3. Otherwise: if the `openai` package **and** `OPENAI_API_KEY` are both present → OpenAI
4. Otherwise: if `sentence-transformers` is installed → local
5. Otherwise: BM25-only

Relevant environment variables:

| Variable | Purpose |
|----------|---------|
| `GNMI_EMBEDDING_PROVIDER` | Force `openai` or `local` instead of auto-detecting |
| `OPENAI_API_KEY` | API key for the OpenAI provider (required for it) |
| `GNMI_EMBEDDING_BASE_URL` | Point the OpenAI client at a compatible endpoint (e.g. a self-hosted embeddings server) |

```bash
# Force local embeddings even if an API key is set
GNMI_EMBEDDING_PROVIDER=local \
  splice --config config.yaml find-paths --pattern "frame loss" --semantic

# Use a self-hosted OpenAI-compatible embeddings endpoint
export GNMI_EMBEDDING_PROVIDER=openai
export OPENAI_API_KEY=sk-...
export GNMI_EMBEDDING_BASE_URL=http://192.0.2.10:8080/v1
splice --config config.yaml find-paths --pattern "chromatic dispersion" --semantic
```

!!! warning "Don't mix embedding spaces"
    A cached index records which provider built it. If you switch providers (e.g. from local 384-dim to OpenAI 3072-dim), Splice rebuilds the index with the now-available provider rather than comparing vectors from different models — mixing embedding spaces produces garbage rankings.

## Python API

The [Python API](python-api.md) `Device` exposes two distinct lookups. Note they behave differently from the CLI:

- **`find_paths(pattern)`** — **glob** matching (via `fnmatch`), tested against both the namespace-stripped and canonical path forms. Returns a plain list of paths.
- **`search_paths(query, top_k=10)`** — the same hybrid ranking as `--semantic`. Returns `(path, score)` tuples, best-first. The index is built once and cached on the device instance.

```python
from splice.api.device import Device

dev = Device("sim://openroadm")          # or Device(host="192.0.2.1", username="admin", ...)
dev.discover()

# Glob match — note the wildcard, this is NOT substring
for p in dev.find_paths("*/circuit-packs/*/ports/*"):
    print(p)

# Ranked hybrid search
for path, score in dev.search_paths("optical signal-to-noise ratio", top_k=5):
    print(f"{score:.3f}  {path}")
```

!!! warning "`find_paths` is glob, not substring"
    Unlike `splice find-paths` (substring by default), `Device.find_paths()` uses shell-glob semantics. `"optical-power"` matches only an exact path; use `"*optical-power*"` to match anywhere in the path.

For lower-level control — choosing `bm25` vs `hybrid` vs `semantic` mode, adjusting `bm25_weight`, or persisting an index with `save()`/`load()` — use `splice.path_index.PathIndex` directly:

```python
from splice.path_index import PathIndex, build_path_entries

entries = build_path_entries(dev._catalog)   # accepts catalog list, map, or InMemoryCatalog
idx = PathIndex(entries)
idx.build()                                   # builds BM25; adds embeddings if a provider exists

idx.search("Q-factor", mode="bm25")           # keyword only
idx.search("Q-factor", mode="hybrid", bm25_weight=0.5)
idx.search("Q-factor", mode="semantic")       # raises RuntimeError if no provider

idx.save("/path/to/myindex")                  # writes .json + .npy
PathIndex.load("/path/to/myindex")            # restores; rebuilds BM25 in ms
```

## MCP server (AI agents)

The [MCP server](index.md) exposes both lookups as tools for AI agents:

- **`find_paths(device_id, pattern)`** — glob match; returns the matching paths.
- **`search_paths(device_id, query, top_k=10)`** — hybrid ranked search; returns rich result objects with `path`, `score`, `module`, `keyword`, `data_type`, `config`, and `description`, plus a `count`. When no embedding provider is installed it adds a `note` explaining results are BM25-only. The index is cached per device for the session.

```json
{
  "device_id": "dev-1",
  "query": "FEC bit error rate",
  "results": [
    {
      "path": "/org-openroadm-device:org-openroadm-device/.../pre-fec-bit-error-rate",
      "score": 0.81,
      "module": "org-openroadm-device",
      "keyword": "leaf",
      "data_type": "decimal64",
      "config": false,
      "description": "..."
    }
  ],
  "count": 1
}
```

This is how an agent answers "where's the bit error rate on this device?" without being told the exact YANG path. See the MCP tool table in the project README for the full tool set.

## The path tree browser

Flat path lists are searchable but hard to *explore*. Splice can render the catalog as a hierarchical tree, collapsing the thousands of flat paths into their natural YANG containment structure.

The tree API (`splice.path_index`) provides:

- `build_tree(entries)` — nest flat `PathEntry` objects into a `TreeNode` hierarchy.
- `walk_tree(node)` — depth-first traversal yielding `(depth, node)` pairs.
- `subtree(root, prefix)` — navigate to a sub-branch by path prefix. It accepts **both** canonical (`/p1:components/p1:component`) and alias-style (`/components/component`) prefixes, stripping namespace prefixes when matching.
- `print_tree(node, max_depth=3)` — print a readable indented rendering, annotating leaves with their `[keyword]` and `(data_type)`.

This is what powers the **Paths** tab in the [TUI](tui.md): a lazy-loading tree you expand branch by branch, with a detail panel showing each path's module, keyword, type, and config flag, plus substring filtering and `/query` semantic search.

```python
from splice.path_index import build_path_entries, build_tree, subtree, print_tree

entries = build_path_entries(dev._catalog)
root = build_tree(entries)

# Print the interfaces subtree, three levels deep
node = subtree(root, "/interfaces/interface")
if node:
    print_tree(node, max_depth=3)
```

## Choosing a method

!!! example "Which one should I use?"
    - **I know the exact name or a unique substring** → `splice find-paths --pattern ...` (instant, no deps).
    - **I need a structured pattern** → `--regex`, or `Device.find_paths()` with globs.
    - **I know the concept but not the YANG name** → `--semantic` (install `[search]` or `[openai]` for true semantic matching).
    - **I want to browse the hierarchy** → the [TUI](tui.md) Paths tab, or the tree API.
    - **An AI agent is driving** → the MCP `find_paths` / `search_paths` tools.

## See also

- [Configuration](configuration.md) — catalog discovery, `yang_dir`, and metrics curation
- [Python API](python-api.md) — `Device.find_paths` / `Device.search_paths`
- [Installation](installation.md) — the `[search]` and `[openai]` extras
- [TUI](tui.md) — interactive tree browser and live search
