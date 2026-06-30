# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, List, Optional, Tuple
from defusedxml import ElementTree as ET


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _find_children_by_local(el: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in list(el) if _local(c.tag) == name]


def _find_first_by_local(el: ET.Element, name: str) -> Optional[ET.Element]:
    for c in list(el):
        if _local(c.tag) == name:
            return c
    return None


def _text(el: Optional[ET.Element]) -> Optional[str]:
    if el is None:
        return None
    txt = (el.text or "").strip()
    return txt if txt != "" else None


def parse_yang_library(xml: str) -> Optional[Dict[str, object]]:
    """Parse RFC 8525 yang-library or RFC 7895 modules-state.

    Returns:
      {"source": "ietf-yang-library"|"ietf-yang-library (RFC 7895)", "modules": [...]}
      or None if not present.
    """
    if not xml:
        return None

    root = ET.fromstring(xml)
    data_el = _find_first_by_local(root, "data")
    data = data_el if data_el is not None else root

    # RFC 8525
    yl = _find_first_by_local(data, "yang-library")
    if yl is not None:
        module_sets = _find_children_by_local(yl, "module-set")
        modules: List[Dict[str, object]] = []
        for ms in module_sets:
            for mod in _find_children_by_local(ms, "module"):
                modules.append(
                    {
                        "name": _text(_find_first_by_local(mod, "name")),
                        "revision": _text(_find_first_by_local(mod, "revision")),
                        "namespace": _text(_find_first_by_local(mod, "namespace")),
                        "conformance": _text(_find_first_by_local(mod, "conformance-type")),
                        "features": [
                            _text(f) for f in _find_children_by_local(mod, "feature") if _text(f)
                        ],
                        "deviations": [
                            {
                                "name": _text(_find_first_by_local(d, "name")),
                                "revision": _text(_find_first_by_local(d, "revision")),
                            }
                            for d in _find_children_by_local(mod, "deviation")
                        ],
                        "submodules": [
                            {
                                "name": _text(_find_first_by_local(sm, "name")),
                                "revision": _text(_find_first_by_local(sm, "revision")),
                            }
                            for sm in _find_children_by_local(mod, "submodule")
                        ],
                    }
                )
        return {"source": "ietf-yang-library", "modules": modules}

    # RFC 7895
    ms = _find_first_by_local(data, "modules-state")
    if ms is not None:
        modules = []
        for mod in _find_children_by_local(ms, "module"):
            modules.append(
                {
                    "name": _text(_find_first_by_local(mod, "name")),
                    "revision": _text(_find_first_by_local(mod, "revision")),
                    "namespace": _text(_find_first_by_local(mod, "namespace")),
                    "conformance": _text(_find_first_by_local(mod, "conformance-type")),
                    "features": [
                        _text(f) for f in _find_children_by_local(mod, "feature") if _text(f)
                    ],
                    "deviations": [
                        {
                            "name": _text(_find_first_by_local(d, "name")),
                            "revision": _text(_find_first_by_local(d, "revision")),
                        }
                        for d in _find_children_by_local(mod, "deviation")
                    ],
                    "submodules": [
                        {
                            "name": _text(_find_first_by_local(sm, "name")),
                            "revision": _text(_find_first_by_local(sm, "revision")),
                        }
                        for sm in _find_children_by_local(mod, "submodule")
                    ],
                }
            )
        return {"source": "ietf-yang-library (RFC 7895)", "modules": modules}

    return None


def parse_netconf_monitoring(xml: str) -> Optional[Dict[str, object]]:
    """Parse RFC 6022 ietf-netconf-monitoring schemas.

    Returns:
      {"source": "ietf-netconf-monitoring", "modules": [...]}
      or None if not present.
    """
    if not xml:
        return None

    root = ET.fromstring(xml)
    data_el = _find_first_by_local(root, "data")
    data = data_el if data_el is not None else root
    ns = _find_first_by_local(data, "netconf-state")
    if ns is None:
        return None

    schemas = _find_first_by_local(ns, "schemas")
    if schemas is None:
        return None

    modules = []
    for s in _find_children_by_local(schemas, "schema"):
        modules.append(
            {
                "name": _text(_find_first_by_local(s, "identifier")),
                "revision": _text(_find_first_by_local(s, "version")),
                "namespace": _text(_find_first_by_local(s, "namespace")),
                "format": _text(_find_first_by_local(s, "format")),
                "location": _text(_find_first_by_local(s, "location")),
            }
        )

    return {"source": "ietf-netconf-monitoring", "modules": modules}


def select_supported_modules(xml: str) -> Tuple[Optional[str], List[Dict[str, object]]]:
    """Return (source, modules) using yang-library preferred, else netconf-monitoring."""
    yl = parse_yang_library(xml)
    if yl is not None:
        return yl["source"], yl["modules"]  # type: ignore[index]

    nm = parse_netconf_monitoring(xml)
    if nm is not None:
        return nm["source"], nm["modules"]  # type: ignore[index]

    return None, []
