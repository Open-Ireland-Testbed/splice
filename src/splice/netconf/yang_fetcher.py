# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import os
from typing import Optional
from defusedxml import ElementTree as ET

from splice.db.repository import create_yang_file


def build_get_schema_rpc(identifier: str, version: str | None = None, fmt: str = "yang") -> str:
    v = version or ""
    return (
        "<get-schema xmlns=\"urn:ietf:params:xml:ns:yang:ietf-netconf-monitoring\">"
        f"<identifier>{identifier}</identifier>"
        f"<version>{v}</version>"
        f"<format>{fmt}</format>"
        "</get-schema>"
    )


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def extract_schema_text(reply_xml: str) -> str:
    """Extract YANG text from a get-schema RPC reply XML string."""
    if not reply_xml:
        return ""
    root = ET.fromstring(reply_xml)
    data_el = None
    for el in root.iter():
        if _local(el.tag) == "data":
            data_el = el
            break
    if data_el is None:
        return ""
    # If data element has text, return it; else join child text
    if data_el.text and data_el.text.strip():
        return data_el.text
    # Some servers may wrap content; fall back to inner text
    text_parts = []
    for t in data_el.itertext():
        if t and t.strip():
            text_parts.append(t)
    return "\n".join(text_parts)


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def fetch_and_store_schema(
    manager,
    session,
    module_id: int,
    module_name: str,
    revision: Optional[str],
    out_dir: str,
) -> Optional[str]:
    """Fetch YANG module via get-schema, store to disk and DB. Returns file path or None."""
    rpc = build_get_schema_rpc(module_name, revision, fmt="yang")
    try:
        from ncclient.xml_ import to_ele  # type: ignore
        reply = manager.dispatch(to_ele(rpc))
    except Exception:
        reply = manager.dispatch(ET.fromstring(rpc))

    # ncclient reply may carry .xml as str or Element
    raw = getattr(reply, "xml", "")
    if not isinstance(raw, str):
        raw = ET.tostring(raw, encoding="unicode")

    yang_text = extract_schema_text(raw)
    if not yang_text:
        return None

    os.makedirs(out_dir, exist_ok=True)
    fname = f"{module_name}@{revision}.yang" if revision else f"{module_name}.yang"
    path = os.path.join(out_dir, fname)

    newly_written = not os.path.exists(path)
    with open(path, "w") as f:
        f.write(yang_text)

    try:
        create_yang_file(
            session,
            module_id=module_id,
            path=path,
            checksum=_checksum(yang_text),
        )
    except Exception:
        # The DB row failed but the file is on disk; the caller's existence
        # check would skip re-fetching it, leaving disk and DB inconsistent.
        # Remove the file we just wrote so the next run re-fetches and re-records.
        if newly_written:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise

    return path
