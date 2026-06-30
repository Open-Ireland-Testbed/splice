# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import os
from typing import Dict, List, Tuple

from pyang import context, repository

_log = logging.getLogger(__name__)


def build_pyang_context(yang_dir: str) -> context.Context:
    repo = repository.FileRepository(yang_dir)
    ctx = context.Context(repo)
    loaded = 0
    for fname in sorted(os.listdir(yang_dir)):
        if not fname.endswith(".yang"):
            continue
        path = os.path.join(yang_dir, fname)
        with open(path, "r") as f:
            mod = ctx.add_module(fname, f.read())
        if mod is None:
            # pyang records the failure in ctx.errors rather than raising; a
            # module that fails to load contributes NO paths to the catalog, so
            # surface it instead of letting coverage silently shrink.
            _log.warning("YANG module failed to load (paths will be missing): %s", fname)
        else:
            loaded += 1
    ctx.validate()
    # pyang accumulates parse/validation problems in ctx.errors without
    # raising. Report error-level entries so operators see which modules are
    # degraded rather than discovering missing telemetry paths at query time.
    error_modules = set()
    for err in getattr(ctx, "errors", []):
        try:
            pos, tag = err[0], err[1]
        except (IndexError, TypeError):
            continue
        level = None
        try:
            from pyang import error as _pyang_error
            level = _pyang_error.err_level(tag)
        except Exception:
            level = None
        # err_level 1/2 are errors; treat unknown as error to be safe.
        if level in (None, 1, 2):
            ref = getattr(pos, "ref", None) or getattr(pos, "top", None)
            error_modules.add(str(ref))
    if error_modules:
        _log.warning(
            "pyang reported errors in %d YANG file(s); some paths may be "
            "missing from the catalog: %s",
            len(error_modules),
            ", ".join(sorted(os.path.basename(m) for m in error_modules)[:10]),
        )
    return ctx


def _listify(val):
    if val is None:
        return []
    if isinstance(val, list):
        return val
    return [val]


def _stmt_namespace(stmt, fallback):
    mod = getattr(stmt, "i_module", None) or getattr(stmt, "i_orig_module", None)
    if mod is None:
        return fallback
    ns_stmt = mod.search_one("namespace")
    return ns_stmt.arg if ns_stmt is not None else fallback


def iter_data_nodes(stmt, path_elems, results, root_ns):
    for ch in _listify(getattr(stmt, "i_children", None)):
        if ch.keyword in ("container", "list", "leaf", "leaf-list", "anyxml", "anydata"):
            ns = _stmt_namespace(ch, root_ns)
            new_path = path_elems + [(ch.arg, ns, ch.keyword)]
            is_config = getattr(ch, "i_config", True)
            data_type = None
            if ch.keyword in ("leaf", "leaf-list"):
                t = ch.search_one("type")
                if t is not None:
                    data_type = t.arg
            list_keys = None
            if ch.keyword == "list":
                keys = getattr(ch, "i_key", None)
                if keys:
                    list_keys = [k.arg for k in keys]
            desc_stmt = ch.search_one("description")
            results.append(
                {
                    "path_elems": new_path,
                    "keyword": ch.keyword,
                    "config": bool(is_config),
                    "data_type": data_type,
                    "list_keys": list_keys,
                    "description": desc_stmt.arg if desc_stmt else None,
                }
            )
            iter_data_nodes(ch, new_path, results, root_ns)
        elif ch.keyword in ("choice", "case"):
            iter_data_nodes(ch, path_elems, results, root_ns)
        else:
            iter_data_nodes(ch, path_elems, results, root_ns)


def build_filter_xml(path_elems: List[Tuple[str, str, str]]) -> Tuple[str, Dict[str, str]]:
    namespaces: List[str] = []
    for _, ns, _ in path_elems:
        if ns not in namespaces:
            namespaces.append(ns)
    prefix_map = {ns: f"p{idx+1}" for idx, ns in enumerate(namespaces)}

    def tag(name, ns):
        return f"{prefix_map[ns]}:{name}"

    xmlns_attrs = " ".join([f"xmlns:{prefix_map[ns]}=\"{ns}\"" for ns in namespaces])

    lines = ["<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">"]
    indent = "  "
    for i, (name, ns, _) in enumerate(path_elems):
        t = tag(name, ns)
        if i == 0:
            lines.append(f"{indent}<{t} {xmlns_attrs}>")
        else:
            lines.append(f"{indent}<{t}>")
        indent += "  "
    for i in range(len(path_elems) - 1, -1, -1):
        indent = indent[:-2]
        name, ns, _ = path_elems[i]
        t = tag(name, ns)
        lines.append(f"{indent}</{t}>")
    lines.append("</filter>")
    return "\n".join(lines), prefix_map


def path_to_str(path_elems: List[Tuple[str, str, str]], prefix_map: Dict[str, str]) -> str:
    return "/" + "/".join([f"{prefix_map[ns]}:{name}" for name, ns, _ in path_elems])


def build_catalog(yang_dir: str, include_config: bool = True) -> List[Dict[str, object]]:
    ctx = build_pyang_context(yang_dir)
    catalog = []

    for mod in sorted(ctx.modules.values(), key=lambda m: m.arg):
        if mod.keyword != "module":
            continue
        ns_stmt = mod.search_one("namespace")
        root_ns = ns_stmt.arg if ns_stmt is not None else ""
        nodes = []
        iter_data_nodes(mod, [], nodes, root_ns)
        for n in nodes:
            if not include_config and n["config"]:
                continue
            path_elems = n["path_elems"]
            filter_xml, prefix_map = build_filter_xml(path_elems)
            path_str = path_to_str(path_elems, prefix_map)
            catalog.append(
                {
                    "module": mod.arg,
                    "root_namespace": root_ns,
                    "path": path_str,
                    "path_elems": [
                        {"name": name, "namespace": ns, "keyword": kw}
                        for name, ns, kw in path_elems
                    ],
                    "keyword": n["keyword"],
                    "config": n["config"],
                    "data_type": n.get("data_type"),
                    "list_keys": n.get("list_keys"),
                    "filter_xml": filter_xml,
                    "namespace_map": prefix_map,
                    "description": n.get("description"),
                }
            )

    return catalog
