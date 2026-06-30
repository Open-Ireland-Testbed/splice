# TUI Path Search — Integration Guide

## Overview

The `splice.path_index` module provides two capabilities for TUI consumption:

1. **Tree browser** — hierarchical navigation of catalog paths
2. **Semantic search** — embedding-based fuzzy search over paths and descriptions

## Quick Start

```python
from splice.catalog import build_catalog
from splice.path_index import (
    build_path_entries, build_tree, subtree, walk_tree,
    PathIndex, is_search_available,
)

# Build from raw catalog
cat = build_catalog("path/to/yang")
entries = build_path_entries(cat)

# Tree browser
tree = build_tree(entries)
node = subtree(tree, "/components")  # alias-style works
for depth, child in walk_tree(node):
    print("  " * depth + child.segment)

# Semantic search (optional)
if is_search_available():
    idx = PathIndex(entries)
    idx.build()
    for entry, score in idx.search("amplifier input power"):
        print(f"{score:.3f}  {entry.path}")
    # Persist for fast reload
    idx.save("cache/path_index")
    idx = PathIndex.load("cache/path_index")
```

## Using with InMemoryCatalog (API stack)

```python
from splice.api.device import Device

dev = Device(host="192.0.2.15", username="admin", password="secret")
dev.connect()
entries = build_path_entries(dev.catalog)
```

## Using with DB stack

```python
from splice.db.catalog_query import fetch_catalog_map_with_module
entries = build_path_entries(catalog_map, description_map=desc_map)
```

## Key types

- `PathEntry` — flat entry with path, segments, keyword, config, data_type, description, module, list_keys
- `TreeNode` — tree node with segment, full_path, optional entry, children dict
- `PathIndex` — wraps sentence-transformers for semantic search; `.build()`, `.search()`, `.save()`, `.load()`

## TUI recommendations

- Use `subtree()` for drill-down navigation (supports alias-style paths without namespace prefixes)
- Use `walk_tree()` for rendering visible nodes with depth-based indentation
- Lazy-load `PathIndex` only when user activates search mode
- Call `is_search_available()` to gracefully degrade if sentence-transformers isn't installed
- Pre-build and `.save()` the index on catalog build; `.load()` for instant startup
