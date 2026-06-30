# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from typing import Dict, List
from urllib.parse import quote, unquote


def is_path_prefix(path: str, prefix: str) -> bool:
    """True if path equals prefix or is a descendant of it.

    Boundary-aware: a descendant continues with '/' (next path element) or
    '[' (a predicate on the prefix's last element, e.g. prefix
    '/pm/otnpm-data' matches '/pm/otnpm-data[interface=x]/...'). This avoids
    the plain-startswith false positive where '/system' would match
    '/system-backup'.
    """
    return (
        path == prefix
        or path.startswith(prefix + "/")
        or path.startswith(prefix + "[")
        or prefix == "/"
    )


def normalize_path(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    norm = []
    for p in parts:
        norm.append(p.split(":", 1)[1] if ":" in p else p)
    return "/" + "/".join(norm)


def encode_key_value(value: str) -> str:
    return quote(value, safe="")


def decode_key_value(value: str) -> str:
    return unquote(value)


def apply_list_keys(path: str, values: Dict[str, str], list_keys: Dict[str, List[str]]) -> str:
    """Convert list path to key-qualified form if key values are present.

    Example:
      path /interfaces/interface/name with key ['name'] -> /interfaces/interface[name=eth0]
    """
    if not list_keys:
        return path

    parts = [p for p in path.strip("/").split("/") if p]
    new_parts: List[str] = []
    for i, part in enumerate(parts):
        cur_path = "/" + "/".join(parts[: i + 1])
        keys = list_keys.get(cur_path)
        if keys:
            kv = []
            for k in keys:
                key_path = cur_path + "/" + k
                if key_path in values:
                    kv.append(f"{k}={encode_key_value(str(values[key_path]))}")
            if kv:
                new_parts.append(f"{part}[{','.join(kv)}]")
            else:
                new_parts.append(part)
        else:
            new_parts.append(part)
    return "/" + "/".join(new_parts)
