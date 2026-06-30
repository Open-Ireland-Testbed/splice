"""Tests for path_index module: tree browser and semantic search."""

import json
import tempfile
from pathlib import Path

import pytest

from splice.path_index import (
    PathEntry,
    PathIndex,
    TreeNode,
    build_path_entries,
    build_tree,
    print_tree,
    subtree,
    walk_tree,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CATALOG = [
    {
        "path": "/p1:components/p1:component",
        "keyword": "list",
        "config": True,
        "data_type": None,
        "list_keys": ["name"],
        "module": "openconfig-platform",
        "description": "List of platform components",
        "filter_xml": "<filter/>",
        "namespace_map": {"urn:oc:platform": "p1"},
    },
    {
        "path": "/p1:components/p1:component/p1:state",
        "keyword": "container",
        "config": False,
        "data_type": None,
        "list_keys": None,
        "module": "openconfig-platform",
        "description": "Operational state data",
        "filter_xml": "<filter/>",
        "namespace_map": {"urn:oc:platform": "p1"},
    },
    {
        "path": "/p1:components/p1:component/p1:state/p1:temperature",
        "keyword": "leaf",
        "config": False,
        "data_type": "decimal64",
        "list_keys": None,
        "module": "openconfig-platform",
        "description": "Temperature of the component",
        "filter_xml": "<filter/>",
        "namespace_map": {"urn:oc:platform": "p1"},
    },
    {
        "path": "/p2:interfaces/p2:interface",
        "keyword": "list",
        "config": True,
        "data_type": None,
        "list_keys": ["name"],
        "module": "openconfig-interfaces",
        "description": "List of interfaces",
        "filter_xml": "<filter/>",
        "namespace_map": {"urn:oc:if": "p2"},
    },
    {
        "path": "/p2:interfaces/p2:interface/p2:state/p2:counters/p2:in-octets",
        "keyword": "leaf",
        "config": False,
        "data_type": "uint64",
        "list_keys": None,
        "module": "openconfig-interfaces",
        "description": "Input octet counter",
        "filter_xml": "<filter/>",
        "namespace_map": {"urn:oc:if": "p2"},
    },
]


@pytest.fixture
def entries():
    return build_path_entries(SAMPLE_CATALOG)


@pytest.fixture
def tree(entries):
    return build_tree(entries)


# ---------------------------------------------------------------------------
# build_path_entries
# ---------------------------------------------------------------------------

class TestBuildPathEntries:
    def test_from_list(self):
        entries = build_path_entries(SAMPLE_CATALOG)
        assert len(entries) == 5
        paths = {e.path for e in entries}
        assert "/p1:components/p1:component" in paths
        assert "/p2:interfaces/p2:interface" in paths

    def test_from_dict(self):
        catalog_map = {e["path"]: e for e in SAMPLE_CATALOG}
        entries = build_path_entries(catalog_map)
        assert len(entries) == 5

    def test_from_in_memory_catalog(self):
        from splice.api._catalog import InMemoryCatalog

        catalog_map = {e["path"]: e for e in SAMPLE_CATALOG}
        desc_map = {e["path"]: e["description"] for e in SAMPLE_CATALOG if e.get("description")}
        cat = InMemoryCatalog(catalog_map=catalog_map, description_map=desc_map)
        entries = build_path_entries(cat)
        assert len(entries) == 5
        temp_entry = [e for e in entries if "temperature" in e.path][0]
        assert temp_entry.description == "Temperature of the component"

    def test_segments_correct(self):
        entries = build_path_entries(SAMPLE_CATALOG)
        comp = [e for e in entries if e.path == "/p1:components/p1:component"][0]
        assert comp.segments == ["p1:components", "p1:component"]

    def test_metadata_preserved(self):
        entries = build_path_entries(SAMPLE_CATALOG)
        temp = [e for e in entries if "temperature" in e.path][0]
        assert temp.keyword == "leaf"
        assert temp.config is False
        assert temp.data_type == "decimal64"
        assert temp.description == "Temperature of the component"


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------

class TestBuildTree:
    def test_root_has_children(self, tree):
        assert len(tree.children) == 2
        assert "p1:components" in tree.children
        assert "p2:interfaces" in tree.children

    def test_hierarchy_depth(self, tree):
        comp = tree.children["p1:components"].children["p1:component"]
        assert "p1:state" in comp.children
        state = comp.children["p1:state"]
        assert "p1:temperature" in state.children

    def test_intermediate_nodes_created(self, tree):
        """Intermediate nodes without direct entries get created."""
        # /p2:interfaces/p2:interface/p2:state/p2:counters is intermediate
        iface = tree.children["p2:interfaces"].children["p2:interface"]
        assert "p2:state" in iface.children
        state = iface.children["p2:state"]
        assert "p2:counters" in state.children
        counters = state.children["p2:counters"]
        # counters is intermediate — no direct entry in catalog
        assert counters.entry is None
        # but has child
        assert "p2:in-octets" in counters.children

    def test_leaf_entries_attached(self, tree):
        temp = (
            tree.children["p1:components"]
            .children["p1:component"]
            .children["p1:state"]
            .children["p1:temperature"]
        )
        assert temp.entry is not None
        assert temp.entry.keyword == "leaf"

    def test_full_path_set(self, tree):
        comp = tree.children["p1:components"].children["p1:component"]
        assert comp.full_path == "/p1:components/p1:component"


# ---------------------------------------------------------------------------
# walk_tree
# ---------------------------------------------------------------------------

class TestWalkTree:
    def test_dfs_order(self, tree):
        nodes = list(walk_tree(tree))
        # First node is root
        assert nodes[0][1].segment == ""
        # All nodes visited
        segments = [n.segment for _, n in nodes]
        assert "p1:components" in segments
        assert "p1:temperature" in segments
        assert "p2:in-octets" in segments

    def test_depth_values(self, tree):
        nodes = list(walk_tree(tree))
        root_depth = nodes[0][0]
        assert root_depth == 0
        # Children of root should be depth 1
        for d, n in nodes:
            if n.segment in ("p1:components", "p2:interfaces"):
                assert d == 1


# ---------------------------------------------------------------------------
# subtree
# ---------------------------------------------------------------------------

class TestSubtree:
    def test_canonical_prefix(self, tree):
        node = subtree(tree, "/p1:components/p1:component")
        assert node is not None
        assert node.segment == "p1:component"

    def test_alias_style_prefix(self, tree):
        """Alias-style (no namespace prefix) should still find the node."""
        node = subtree(tree, "/components/component")
        assert node is not None
        assert node.segment == "p1:component"

    def test_nonexistent_returns_none(self, tree):
        assert subtree(tree, "/does/not/exist") is None

    def test_partial_prefix(self, tree):
        node = subtree(tree, "/p1:components")
        assert node is not None
        assert "p1:component" in node.children


# ---------------------------------------------------------------------------
# print_tree (smoke test)
# ---------------------------------------------------------------------------

class TestPrintTree:
    def test_does_not_crash(self, tree, capsys):
        print_tree(tree, max_depth=2)
        captured = capsys.readouterr()
        assert "p1:components" in captured.out
        assert "p1:component" in captured.out

    def test_max_depth_limits_output(self, tree, capsys):
        print_tree(tree, max_depth=1)
        captured = capsys.readouterr()
        # Depth 1 = root children only, depth 2 should not appear
        assert "p1:temperature" not in captured.out


# ---------------------------------------------------------------------------
# Description extraction integration
# ---------------------------------------------------------------------------

class TestDescriptionExtraction:
    def test_build_catalog_includes_description(self):
        """Verify description is extracted from YANG model."""
        import tempfile
        from pathlib import Path
        from splice.catalog import build_catalog

        yang = """
        module test-desc {
          namespace "urn:test:desc";
          prefix "td";
          container sensors {
            description "Container for sensor data";
            leaf temperature {
              type decimal64 { fraction-digits 2; }
              description "Current temperature reading";
            }
          }
        }
        """
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test-desc.yang"
            p.write_text(yang)
            cat = build_catalog(str(p.parent))

        container = [e for e in cat if e["path"].endswith(":sensors")][0]
        assert container["description"] == "Container for sensor data"

        leaf = [e for e in cat if e["path"].endswith(":temperature")][0]
        assert leaf["description"] == "Current temperature reading"


# ---------------------------------------------------------------------------
# PathIndex (semantic search — guarded by importorskip)
# ---------------------------------------------------------------------------

class TestPathIndex:
    @pytest.fixture(autouse=True)
    def _skip_if_no_transformers(self):
        pytest.importorskip("sentence_transformers")

    @pytest.fixture
    def index(self, entries):
        idx = PathIndex(entries)
        idx.build()
        return idx

    def test_search_returns_results(self, index):
        results = index.search("temperature")
        assert len(results) > 0
        paths = [e.path for e, _ in results]
        assert any("temperature" in p for p in paths)

    def test_search_scores_ordered(self, index):
        results = index.search("component temperature", top_k=5)
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)

    def test_save_load_roundtrip(self, index):
        with tempfile.TemporaryDirectory() as d:
            save_path = str(Path(d) / "test_index")
            index.save(save_path)

            loaded = PathIndex.load(save_path)
            assert len(loaded.entries) == len(index.entries)
            assert loaded.model_name == index.model_name

            # Verify metadata preserved
            orig_paths = {e.path for e in index.entries}
            loaded_paths = {e.path for e in loaded.entries}
            assert orig_paths == loaded_paths

    def test_search_without_build_raises(self, entries):
        idx = PathIndex(entries)
        with pytest.raises(RuntimeError, match="call .build"):
            idx.search("test")


# ---------------------------------------------------------------------------
# Text processing helpers (BM25)
# ---------------------------------------------------------------------------

class TestTextProcessing:
    def test_tokenize_lowercases_and_splits(self):
        from splice.path_index import _tokenize

        assert _tokenize("Interface Admin-Status (up)") == ["interface", "admin", "status", "up"]

    def test_search_text_includes_metadata(self, entries):
        from splice.path_index import _search_text

        temp = [e for e in entries if "temperature" in e.path][0]
        text = _search_text(temp)
        assert "temperature" in text
        assert "Temperature of the component" in text
        assert "openconfig-platform" in text
        assert "decimal64" in text
        # namespace prefixes are stripped
        assert "p1:" not in text

    def test_search_text_includes_list_keys(self, entries):
        from splice.path_index import _search_text

        comp = [e for e in entries if e.path == "/p1:components/p1:component"][0]
        assert "name" in _search_text(comp).split()


# ---------------------------------------------------------------------------
# BM25 search (no embedding provider required)
# ---------------------------------------------------------------------------

@pytest.fixture
def bm25_index(entries, monkeypatch):
    """Index built with no embedding provider available."""
    import splice.path_index as pi

    def _no_provider():
        raise ImportError("no provider in this test")

    monkeypatch.setattr(pi, "get_embedding_provider", _no_provider)
    idx = PathIndex(entries)
    idx.build()
    return idx


class TestBM25Search:
    def test_build_without_provider_succeeds(self, bm25_index):
        assert bm25_index.has_embeddings is False

    def test_bm25_finds_exact_term(self, bm25_index):
        results = bm25_index.search("temperature", mode="bm25", top_k=5)
        top_entry, top_score = results[0]
        assert "temperature" in top_entry.path
        assert top_score > 0

    def test_bm25_ranks_descriptions(self, bm25_index):
        # "octet" appears only in the in-octets leaf (path + description)
        results = bm25_index.search("input octet counter", mode="bm25", top_k=5)
        assert "in-octets" in results[0][0].path

    def test_scores_non_increasing(self, bm25_index):
        results = bm25_index.search("component state", mode="bm25", top_k=10)
        scores = [s for _, s in results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_query_returns_zero_scores(self, bm25_index):
        results = bm25_index.search("", mode="bm25", top_k=5)
        assert all(s == 0.0 for _, s in results)

    def test_unmatched_query_returns_zero_scores(self, bm25_index):
        results = bm25_index.search("@#$%", mode="bm25", top_k=5)
        assert all(s == 0.0 for _, s in results)

    def test_hybrid_degrades_to_bm25_without_provider(self, bm25_index):
        hybrid = bm25_index.search("temperature", mode="hybrid", top_k=5)
        bm25 = bm25_index.search("temperature", mode="bm25", top_k=5)
        assert [e.path for e, _ in hybrid] == [e.path for e, _ in bm25]

    def test_semantic_without_provider_raises(self, bm25_index, monkeypatch):
        import splice.path_index as pi

        def _no_provider():
            raise ImportError("no provider in this test")

        monkeypatch.setattr(pi, "get_embedding_provider", _no_provider)
        with pytest.raises(RuntimeError, match="semantic search unavailable"):
            bm25_index.search("temperature", mode="semantic")

    def test_unknown_mode_raises(self, bm25_index):
        with pytest.raises(ValueError, match="unknown search mode"):
            bm25_index.search("temperature", mode="fuzzy")

    def test_save_load_preserves_bm25_ranking(self, bm25_index, monkeypatch):
        import splice.path_index as pi

        def _no_provider():
            raise ImportError("no provider in this test")

        monkeypatch.setattr(pi, "get_embedding_provider", _no_provider)
        with tempfile.TemporaryDirectory() as d:
            save_path = str(Path(d) / "index")
            before = bm25_index.search("temperature", mode="bm25", top_k=3)
            bm25_index.save(save_path)

            loaded = PathIndex.load(save_path)
            after = loaded.search("temperature", mode="bm25", top_k=3)

        assert [(e.path, round(s, 9)) for e, s in before] == \
               [(e.path, round(s, 9)) for e, s in after]


# ---------------------------------------------------------------------------
# Hybrid search with a deterministic fake provider (requires numpy only)
# ---------------------------------------------------------------------------

class FakeEmbeddings:
    """Deterministic hash-bucket bag-of-words embeddings for tests."""

    provider_type = "fake"
    model_name = "fake-hash-embedder"

    def embed(self, texts):
        import numpy as np

        vecs = []
        for text in texts:
            v = np.zeros(32)
            for tok in text.lower().split():
                v[hash(tok) % 32] += 1.0
            vecs.append(v)
        return np.array(vecs)


class TestHybridSearch:
    @pytest.fixture(autouse=True)
    def _skip_if_no_numpy(self):
        pytest.importorskip("numpy")

    @pytest.fixture
    def index(self, entries):
        idx = PathIndex(entries, provider=FakeEmbeddings())
        idx.build()
        return idx

    def test_hybrid_returns_blended_scores_in_unit_range(self, index):
        results = index.search("temperature", mode="hybrid", top_k=5)
        assert len(results) == 5
        assert all(0.0 <= s <= 1.0 for _, s in results)

    def test_weight_one_matches_bm25_ranking(self, index):
        hybrid = index.search("component temperature", mode="hybrid", bm25_weight=1.0)
        bm25 = index.search("component temperature", mode="bm25")
        assert [e.path for e, _ in hybrid] == [e.path for e, _ in bm25]

    def test_weight_zero_matches_semantic_ranking(self, index):
        hybrid = index.search("component temperature", mode="hybrid", bm25_weight=0.0)
        semantic = index.search("component temperature", mode="semantic")
        assert [e.path for e, _ in hybrid] == [e.path for e, _ in semantic]

    def test_default_mode_is_hybrid(self, index):
        default = index.search("temperature")
        hybrid = index.search("temperature", mode="hybrid")
        assert [e.path for e, _ in default] == [e.path for e, _ in hybrid]
