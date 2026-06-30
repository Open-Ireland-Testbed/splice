# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple


def _strip_prefix(elem: str) -> str:
    if ":" in elem:
        return elem.split(":", 1)[1]
    return elem


def _normalize(elem: str) -> str:
    # keep alnum, dash, underscore; replace others with '-'
    s = re.sub(r"[^a-zA-Z0-9_\-]", "-", elem)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def canonical_to_alias(canonical_path: str) -> str:
    """Convert canonical YANG path to a human-friendly alias path.

    - Strip prefixes (p1:foo -> foo)
    - Normalize token characters
    """
    if not canonical_path:
        return ""
    parts = [p for p in canonical_path.strip("/").split("/") if p]
    alias_parts = [_normalize(_strip_prefix(p)) for p in parts]
    return "/" + "/".join(alias_parts)


def ensure_unique_aliases(paths: Iterable[str]) -> Dict[str, str]:
    """Return mapping canonical->alias ensuring unique aliases.

    If collision occurs, suffix with "-n" in order of appearance.
    """
    alias_map: Dict[str, str] = {}
    used: Dict[str, int] = {}
    for p in paths:
        base = canonical_to_alias(p)
        if base not in used:
            used[base] = 1
            alias_map[p] = base
            continue
        used[base] += 1
        alias_map[p] = f"{base}-{used[base]}"
    return alias_map
