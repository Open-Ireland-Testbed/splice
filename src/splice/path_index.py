# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Path index: tree browser and hybrid search over catalog paths.

Provides two main capabilities:
1. Tree representation of flat catalog paths (no extra dependencies)
2. Hybrid search: BM25 keyword ranking (no extra dependencies) blended
   with embedding-based semantic similarity (pluggable providers: local
   sentence-transformers or OpenAI-compatible API)

Both stacks (DB and in-memory API) produce catalog dicts that
``build_path_entries()`` can consume.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterator, List, Optional, Protocol, Tuple

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PathEntry:
    """A single catalog path with its metadata."""

    path: str
    segments: List[str]
    keyword: str
    config: bool
    data_type: Optional[str] = None
    description: Optional[str] = None
    module: Optional[str] = None
    list_keys: Optional[List[str]] = None


@dataclass
class TreeNode:
    """A node in the hierarchical path tree."""

    segment: str
    full_path: str
    entry: Optional[PathEntry] = None
    children: Dict[str, TreeNode] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------

def build_path_entries(
    catalog: object,
    description_map: Optional[Dict[str, str]] = None,
) -> List[PathEntry]:
    """Build PathEntry list from either stack's catalog format.

    ``catalog`` may be:
    - ``List[Dict]`` — raw output from ``build_catalog()``
    - ``Dict[str, Dict]`` — catalog_map from DB query or InMemoryCatalog
    - ``InMemoryCatalog`` — uses ``.catalog_map`` and ``.description_map``
    """
    from splice.api._catalog import InMemoryCatalog

    entries: List[PathEntry] = []

    if isinstance(catalog, InMemoryCatalog):
        desc_map = description_map or catalog.description_map
        items = catalog.catalog_map
    elif isinstance(catalog, dict):
        desc_map = description_map or {}
        items = catalog
    elif isinstance(catalog, list):
        # Raw catalog list from build_catalog()
        desc_map = description_map or {}
        items = {e["path"]: e for e in catalog}
    else:
        raise TypeError(f"unsupported catalog type: {type(catalog)}")

    for path, info in items.items():
        segments = [s for s in path.split("/") if s]
        desc = info.get("description") or desc_map.get(path)
        entries.append(PathEntry(
            path=path,
            segments=segments,
            keyword=str(info.get("keyword", "")),
            config=bool(info.get("config", False)),
            data_type=info.get("data_type"),
            description=desc,
            module=info.get("module"),
            list_keys=info.get("list_keys"),
        ))

    return entries


# ---------------------------------------------------------------------------
# Tree API
# ---------------------------------------------------------------------------

def build_tree(entries: List[PathEntry]) -> TreeNode:
    """Build a nested tree from flat PathEntry list."""
    root = TreeNode(segment="", full_path="")

    for entry in entries:
        node = root
        for i, seg in enumerate(entry.segments):
            if seg not in node.children:
                partial = "/" + "/".join(entry.segments[: i + 1])
                node.children[seg] = TreeNode(segment=seg, full_path=partial)
            node = node.children[seg]
        node.entry = entry

    return root


def walk_tree(
    node: TreeNode,
    depth: int = 0,
) -> Iterator[Tuple[int, TreeNode]]:
    """Depth-first traversal yielding (depth, node) pairs."""
    yield depth, node
    for child in sorted(node.children.values(), key=lambda n: n.segment):
        yield from walk_tree(child, depth + 1)


def subtree(root: TreeNode, path_prefix: str) -> Optional[TreeNode]:
    """Navigate to a subtree by path prefix.

    Accepts both canonical (``/p1:components/p1:component``) and
    alias-style (``/components/component``) prefixes by stripping
    namespace prefixes during matching.
    """
    segments = [s for s in path_prefix.split("/") if s]
    node = root

    for target in segments:
        # Try exact match first
        if target in node.children:
            node = node.children[target]
            continue

        # Strip namespace prefix for fuzzy match
        target_bare = target.split(":")[-1] if ":" in target else target
        found = False
        for key, child in node.children.items():
            key_bare = key.split(":")[-1] if ":" in key else key
            if key_bare == target_bare:
                node = child
                found = True
                break
        if not found:
            return None

    return node


def print_tree(node: TreeNode, max_depth: int = 3, _depth: int = 0) -> None:
    """Print a text rendering of the tree for CLI/debugging."""
    if _depth > max_depth:
        return

    if _depth == 0:
        label = "/" if not node.segment else node.segment
    else:
        label = node.segment

    suffix = ""
    if node.entry:
        suffix = f"  [{node.entry.keyword}]"
        if node.entry.data_type:
            suffix += f" ({node.entry.data_type})"

    indent = "  " * _depth
    print(f"{indent}{label}{suffix}")

    for child in sorted(node.children.values(), key=lambda n: n.segment):
        print_tree(child, max_depth=max_depth, _depth=_depth + 1)


# ---------------------------------------------------------------------------
# Embedding providers (pluggable)
# ---------------------------------------------------------------------------

class EmbeddingProvider(Protocol):
    """Protocol for embedding providers."""

    def embed(self, texts: list[str]) -> np.ndarray: ...


class LocalEmbeddings:
    """Local sentence-transformers embeddings."""

    provider_type: str = "local"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model = None

    def _load_model(self):
        if self._model is None:
            # Suppress noisy logs from sentence-transformers / huggingface
            _prev_level = logging.getLogger("sentence_transformers").level
            logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self.model_name)
            finally:
                logging.getLogger("sentence_transformers").setLevel(_prev_level)

    def embed(self, texts: list[str]) -> np.ndarray:
        self._load_model()
        return self._model.encode(texts, show_progress_bar=False)


class OpenAIEmbeddings:
    """OpenAI API embeddings (or any OpenAI-compatible endpoint)."""

    provider_type: str = "openai"

    def __init__(
        self,
        model: str = "text-embedding-3-large",
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.model_name = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._base_url = base_url or os.environ.get("GNMI_EMBEDDING_BASE_URL")
        if not self._api_key:
            raise ValueError(
                "OpenAI API key required: pass api_key= or set OPENAI_API_KEY env var"
            )

    def embed(self, texts: list[str]) -> np.ndarray:
        import numpy as np

        # Suppress httpx info logs from openai client
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("openai").setLevel(logging.WARNING)

        from openai import OpenAI

        client_kwargs: dict = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        client = OpenAI(**client_kwargs)

        all_embeddings: list = []
        batch_size = 2048
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            response = client.embeddings.create(model=self.model_name, input=batch)
            # Sort by index to preserve order
            sorted_data = sorted(response.data, key=lambda d: d.index)
            all_embeddings.extend([d.embedding for d in sorted_data])

        return np.array(all_embeddings)


def get_embedding_provider() -> EmbeddingProvider:
    """Auto-detect the best available embedding provider.

    Priority:
    1. GNMI_EMBEDDING_PROVIDER=openai env var -> OpenAIEmbeddings
    2. GNMI_EMBEDDING_PROVIDER=local env var -> LocalEmbeddings
    3. If openai package + OPENAI_API_KEY available -> OpenAIEmbeddings
    4. If sentence-transformers available -> LocalEmbeddings
    5. Raise ImportError with helpful message
    """
    provider_env = os.environ.get("GNMI_EMBEDDING_PROVIDER", "").lower()

    if provider_env == "openai":
        return OpenAIEmbeddings()

    if provider_env == "local":
        return LocalEmbeddings()

    # Auto-detect: try openai first if key is present
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # noqa: F401
            return OpenAIEmbeddings()
        except ImportError:
            pass

    # Try local sentence-transformers
    try:
        import sentence_transformers  # noqa: F401
        return LocalEmbeddings()
    except ImportError:
        pass

    raise ImportError(
        "No embedding provider available. Install one of:\n"
        "  pip install splice[search]    # local sentence-transformers\n"
        "  pip install splice[openai]    # OpenAI API embeddings\n"
        "Or set GNMI_EMBEDDING_PROVIDER and the required package/env vars."
    )


# ---------------------------------------------------------------------------
# Semantic search API
# ---------------------------------------------------------------------------

def _clean_segment(segment: str) -> str:
    """Strip namespace prefix and convert hyphens to spaces."""
    bare = segment.split(":")[-1] if ":" in segment else segment
    return bare.replace("-", " ").replace("_", " ")


def _embed_text(entry: PathEntry) -> str:
    """Build the text string to embed for a path entry."""
    parts = ["/".join(_clean_segment(s) for s in entry.segments)]
    if entry.description:
        parts.append(entry.description)
    if entry.module:
        parts.append(f"module: {entry.module}")
    if entry.data_type:
        parts.append(f"type: {entry.data_type}")
    return ". ".join(parts)


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    """Lowercase and split into alphanumeric word tokens."""
    return _TOKEN_RE.findall(text.lower())


def _search_text(entry: PathEntry) -> str:
    """Build the text string indexed for BM25 keyword search."""
    parts = [
        " ".join(_clean_segment(s) for s in entry.segments),
        entry.keyword,
    ]
    if entry.description:
        parts.append(entry.description)
    if entry.module:
        parts.append(entry.module)
    if entry.data_type:
        parts.append(entry.data_type)
    if entry.list_keys:
        parts.append(" ".join(entry.list_keys))
    return " ".join(p for p in parts if p)


def is_search_available() -> bool:
    """Check whether any embedding provider is available."""
    # Check env var overrides
    provider_env = os.environ.get("GNMI_EMBEDDING_PROVIDER", "").lower()
    if provider_env == "openai":
        try:
            import openai  # noqa: F401
            return bool(os.environ.get("OPENAI_API_KEY"))
        except ImportError:
            return False
    if provider_env == "local":
        try:
            import sentence_transformers  # noqa: F401
            return True
        except ImportError:
            return False

    # Auto-detect: either provider works
    if os.environ.get("OPENAI_API_KEY"):
        try:
            import openai  # noqa: F401
            return True
        except ImportError:
            pass

    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        pass

    return False


_BM25_K1 = 1.2
_BM25_B = 0.75


class PathIndex:
    """Hybrid keyword + semantic search over catalog paths.

    Two scoring backends:
    - BM25 keyword ranking — pure Python, always available.
    - Embedding cosine similarity — requires a provider:
      ``LocalEmbeddings`` (sentence-transformers, default) or
      ``OpenAIEmbeddings`` (OpenAI API or compatible endpoints).
      Install with ``pip install splice[search]`` for local
      or ``pip install splice[openai]`` for OpenAI.

    The default ``hybrid`` search mode blends both, and degrades to
    BM25-only when no embedding provider is installed.
    """

    def __init__(
        self,
        entries: List[PathEntry],
        model_name: str = "all-MiniLM-L6-v2",
        provider: EmbeddingProvider | None = None,
    ):
        self.entries = entries
        self._provider = provider
        # model_name kept for backward compat and save/load
        if provider is not None:
            self.model_name = getattr(provider, "model_name", model_name)
        else:
            self.model_name = model_name
        self._embeddings = None
        self._bm25_built = False
        self._bm25_term_freqs: List[Dict[str, int]] = []
        self._bm25_doc_freqs: Dict[str, int] = {}
        self._bm25_doc_lens: List[int] = []
        self._bm25_avgdl: float = 0.0

    @property
    def has_embeddings(self) -> bool:
        return self._embeddings is not None

    def build(self) -> None:
        """Build the BM25 index and, if a provider is available, embeddings."""
        self._build_bm25()

        if self._provider is None:
            try:
                self._provider = get_embedding_provider()
            except ImportError:
                logger.info(
                    "No embedding provider installed — search will use "
                    "BM25 keyword ranking only"
                )
                return
            # Update model_name from auto-detected provider
            self.model_name = getattr(self._provider, "model_name", self.model_name)

        texts = [_embed_text(e) for e in self.entries]
        self._embeddings = self._provider.embed(texts)

    def _build_bm25(self) -> None:
        """Precompute BM25 term statistics for all entries."""
        self._bm25_term_freqs = []
        self._bm25_doc_freqs = {}
        self._bm25_doc_lens = []

        for entry in self.entries:
            tokens = _tokenize(_search_text(entry))
            freqs: Dict[str, int] = {}
            for tok in tokens:
                freqs[tok] = freqs.get(tok, 0) + 1
            self._bm25_term_freqs.append(freqs)
            self._bm25_doc_lens.append(len(tokens))
            for tok in freqs:
                self._bm25_doc_freqs[tok] = self._bm25_doc_freqs.get(tok, 0) + 1

        n = len(self._bm25_doc_lens)
        self._bm25_avgdl = (sum(self._bm25_doc_lens) / n) if n else 0.0
        self._bm25_built = True

    def _bm25_scores(self, query: str) -> List[float]:
        """BM25 score of the query against every entry."""
        n_docs = len(self.entries)
        scores = [0.0] * n_docs
        query_tokens = set(_tokenize(query))
        if not query_tokens or not n_docs:
            return scores

        avgdl = self._bm25_avgdl or 1.0
        for term in query_tokens:
            doc_freq = self._bm25_doc_freqs.get(term)
            if not doc_freq:
                continue
            idf = math.log(1.0 + (n_docs - doc_freq + 0.5) / (doc_freq + 0.5))
            for i in range(n_docs):
                freq = self._bm25_term_freqs[i].get(term, 0)
                if not freq:
                    continue
                denom = freq + _BM25_K1 * (
                    1 - _BM25_B + _BM25_B * self._bm25_doc_lens[i] / avgdl
                )
                scores[i] += idf * (freq * (_BM25_K1 + 1)) / denom
        return scores

    def _semantic_scores(self, query: str):
        """Cosine similarity of the query against every entry.

        Returns None when embeddings cannot be produced (no provider
        installed).
        """
        if self._provider is None:
            # Embeddings may exist (loaded from cache) but the original
            # provider is unavailable. Must rebuild with the available
            # provider — mixing embedding spaces (e.g., OpenAI 3072-dim
            # with MiniLM 384-dim) produces garbage results or dimension
            # mismatches.
            try:
                self._provider = get_embedding_provider()
            except ImportError:
                return None
            logger.info("Cached provider unavailable — rebuilding index with available provider")
            self.model_name = getattr(self._provider, "model_name", self.model_name)
            self._embeddings = self._provider.embed([_embed_text(e) for e in self.entries])
        elif self._embeddings is None:
            self._embeddings = self._provider.embed([_embed_text(e) for e in self.entries])

        # numpy ships with the search/openai extras, so it is present
        # whenever a provider could be resolved above.
        import numpy as np

        q_emb = self._provider.embed([query])
        norms_e = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        norms_q = np.linalg.norm(q_emb, axis=1, keepdims=True)
        cos_sim = (self._embeddings @ q_emb.T) / (norms_e * norms_q + 1e-10)
        return cos_sim.flatten()

    def search(
        self,
        query: str,
        top_k: int = 20,
        mode: str = "hybrid",
        bm25_weight: float = 0.35,
    ) -> List[Tuple[PathEntry, float]]:
        """Search entries by relevance to ``query``.

        Modes:
        - ``"hybrid"`` (default): weighted blend of BM25 keyword score
          and embedding cosine similarity; degrades to BM25-only when no
          embedding provider is installed.
        - ``"bm25"``: keyword ranking only (no extra dependencies).
        - ``"semantic"``: embedding similarity only; raises RuntimeError
          when no provider is available.
        """
        if mode not in ("hybrid", "bm25", "semantic"):
            raise ValueError(f"unknown search mode: {mode!r}")
        if not self._bm25_built:
            raise RuntimeError("call .build() before .search()")

        if mode == "bm25":
            final_scores: List[float] = self._bm25_scores(query)
        elif mode == "semantic":
            sem = self._semantic_scores(query)
            if sem is None:
                raise RuntimeError(
                    "semantic search unavailable: no embedding provider installed. "
                    "Install with: pip install splice[search]"
                )
            final_scores = [float(s) for s in sem]
        else:
            sem = self._semantic_scores(query)
            bm25 = self._bm25_scores(query)
            if sem is None:
                final_scores = bm25
            else:
                bm25_max = max(bm25, default=0.0)
                final_scores = [
                    bm25_weight * (b / bm25_max if bm25_max > 0 else 0.0)
                    # cosine is in [-1, 1]; shift to [0, 1] before blending
                    + (1.0 - bm25_weight) * (float(s) + 1.0) / 2.0
                    for b, s in zip(bm25, sem)
                ]

        top_indices = sorted(
            range(len(self.entries)),
            key=lambda i: final_scores[i],
            reverse=True,
        )[:top_k]
        return [(self.entries[i], final_scores[i]) for i in top_indices]

    def save(self, path: str) -> None:
        """Persist index to JSON metadata + .npy embeddings.

        BM25 statistics are not persisted — they derive purely from the
        entries and are rebuilt in milliseconds on ``load()``.
        """
        base = Path(path)
        meta = []
        for e in self.entries:
            meta.append({
                "path": e.path,
                "segments": e.segments,
                "keyword": e.keyword,
                "config": e.config,
                "data_type": e.data_type,
                "description": e.description,
                "module": e.module,
                "list_keys": e.list_keys,
            })

        provider_type = getattr(self._provider, "provider_type", "local") if self._provider else "local"

        meta_path = base.with_suffix(".json")
        emb_path = base.with_suffix(".npy")

        with open(meta_path, "w") as f:
            json.dump({
                "model_name": self.model_name,
                "provider_type": provider_type,
                "entries": meta,
            }, f)

        if self._embeddings is not None:
            import numpy as np

            np.save(str(emb_path), self._embeddings)

    @classmethod
    def load(cls, path: str) -> PathIndex:
        """Load a previously saved index."""
        base = Path(path)
        meta_path = base.with_suffix(".json")
        emb_path = base.with_suffix(".npy")

        with open(meta_path) as f:
            data = json.load(f)

        entries = []
        for m in data["entries"]:
            entries.append(PathEntry(
                path=m["path"],
                segments=m["segments"],
                keyword=m["keyword"],
                config=m["config"],
                data_type=m.get("data_type"),
                description=m.get("description"),
                module=m.get("module"),
                list_keys=m.get("list_keys"),
            ))

        model_name = data.get("model_name", "all-MiniLM-L6-v2")
        provider_type = data.get("provider_type", "local")

        # Reconstruct appropriate provider (lazy — only loaded on build/search)
        provider: EmbeddingProvider | None = None
        if provider_type == "openai":
            try:
                provider = OpenAIEmbeddings(model=model_name)
            except (ImportError, ValueError):
                logger.debug("Could not restore OpenAI provider; will auto-detect on next build/search")
        elif provider_type == "local":
            try:
                import sentence_transformers  # noqa: F401
                provider = LocalEmbeddings(model_name=model_name)
            except ImportError:
                logger.debug("Could not restore local provider; will auto-detect on next build/search")

        idx = cls(entries, model_name=model_name, provider=provider)
        if emb_path.exists():
            import numpy as np

            idx._embeddings = np.load(str(emb_path))
        idx._build_bm25()
        return idx
