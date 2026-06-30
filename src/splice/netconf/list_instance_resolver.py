# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple
import logging
import os

from defusedxml import ElementTree as ET

if TYPE_CHECKING:
    from splice.netconf.debug import DiscoveryDebugWriter

_DEBUG_ENV = "GNMI_LIST_INSTANCE_DEBUG"
_logger = logging.getLogger(__name__)


def _debug_enabled() -> bool:
    val = os.environ.get(_DEBUG_ENV, "").strip().lower()
    return val not in ("", "0", "false", "no")


def _rpc_error_details(exc: Exception) -> Dict[str, object]:
    details: Dict[str, object] = {}
    for attr in ("error_tag", "error_type", "error_severity", "error_message", "error_info"):
        val = getattr(exc, attr, None)
        if val:
            details[attr] = val
    xml = getattr(exc, "xml", None) or getattr(exc, "_raw", None)
    if xml:
        details["xml"] = xml
    return details


def _split_tag(tag: str) -> tuple[str, str]:
    if "}" in tag:
        ns, local = tag[1:].split("}", 1)
        return local, ns
    return tag, ""


def _path_elems_from_canonical(
    canonical_path: str, namespace_map: Dict[str, str] | None
) -> List[Tuple[str, str]]:
    if not canonical_path:
        return []
    parts = [p for p in canonical_path.strip("/").split("/") if p]
    prefix_to_ns = {}
    if namespace_map:
        prefix_to_ns = {v: k for k, v in namespace_map.items()}
    elems: List[Tuple[str, str]] = []
    for part in parts:
        part = part.split("[", 1)[0]
        if ":" in part:
            prefix, name = part.split(":", 1)
            ns = prefix_to_ns.get(prefix, "")
            elems.append((name, ns))
        else:
            elems.append((part, ""))
    return elems


def build_list_instance_filter_xml(
    canonical_path: str,
    namespace_map: Dict[str, str] | None,
    list_keys: List[str],
) -> str:
    path_elems = _path_elems_from_canonical(canonical_path, namespace_map)
    namespaces: List[str] = []
    for _, ns in path_elems:
        if ns not in namespaces:
            namespaces.append(ns)
    prefix_map = {ns: f"p{idx+1}" for idx, ns in enumerate(namespaces)}
    if namespace_map:
        for ns, prefix in namespace_map.items():
            prefix_map[ns] = prefix

    def tag(name: str, ns: str) -> str:
        return f"{prefix_map[ns]}:{name}" if ns else name

    xmlns_attrs = " ".join([f"xmlns:{prefix_map[ns]}=\"{ns}\"" for ns in namespaces if ns])
    if not xmlns_attrs:
        xmlns_attrs = ""

    lines = ["<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">"]
    indent = "  "
    for i, (name, ns) in enumerate(path_elems):
        t = tag(name, ns)
        if i == 0 and xmlns_attrs:
            lines.append(f"{indent}<{t} {xmlns_attrs}>")
        else:
            lines.append(f"{indent}<{t}>")
        indent += "  "
    # add key leaves at list node
    list_ns = path_elems[-1][1] if path_elems else ""
    for k in list_keys:
        t = tag(k, list_ns)
        lines.append(f"{indent}<{t}/>")
    for i in range(len(path_elems) - 1, -1, -1):
        indent = indent[:-2]
        name, ns = path_elems[i]
        t = tag(name, ns)
        lines.append(f"{indent}</{t}>")
    lines.append("</filter>")
    return "\n".join(lines)


def build_list_entry_filter_xml(
    canonical_path: str,
    namespace_map: Dict[str, str] | None,
) -> str:
    path_elems = _path_elems_from_canonical(canonical_path, namespace_map)
    namespaces: List[str] = []
    for _, ns in path_elems:
        if ns not in namespaces:
            namespaces.append(ns)
    prefix_map = {ns: f"p{idx+1}" for idx, ns in enumerate(namespaces)}
    if namespace_map:
        for ns, prefix in namespace_map.items():
            prefix_map[ns] = prefix

    def tag(name: str, ns: str) -> str:
        return f"{prefix_map[ns]}:{name}" if ns else name

    xmlns_attrs = " ".join([f"xmlns:{prefix_map[ns]}=\"{ns}\"" for ns in namespaces if ns])
    if not xmlns_attrs:
        xmlns_attrs = ""

    lines = ["<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\">"]
    indent = "  "
    for i, (name, ns) in enumerate(path_elems):
        t = tag(name, ns)
        if i == 0 and xmlns_attrs:
            lines.append(f"{indent}<{t} {xmlns_attrs}>")
        else:
            lines.append(f"{indent}<{t}>")
        indent += "  "
    for i in range(len(path_elems) - 1, -1, -1):
        indent = indent[:-2]
        name, ns = path_elems[i]
        t = tag(name, ns)
        lines.append(f"{indent}</{t}>")
    lines.append("</filter>")
    return "\n".join(lines)


def build_xpath_filter_xml(
    canonical_path: str,
    namespace_map: Dict[str, str] | None,
) -> str:
    parts = []
    for part in canonical_path.strip("/").split("/"):
        if not part:
            continue
        parts.append(part.split("[", 1)[0])
    select = "/" + "/".join(parts)
    prefixes = set()
    for seg in parts:
        if ":" in seg:
            prefixes.add(seg.split(":", 1)[0])
    attrs = []
    if namespace_map:
        for prefix in sorted(prefixes):
            for ns, mapped_prefix in namespace_map.items():
                if mapped_prefix == prefix:
                    attrs.append(f"xmlns:{prefix}=\"{ns}\"")
                    break
    ns_attrs = (" " + " ".join(attrs)) if attrs else ""
    return (
        f"<filter xmlns=\"urn:ietf:params:xml:ns:netconf:base:1.0\" "
        f"type=\"xpath\" select=\"{select}\"{ns_attrs}/>"
    )


def extract_list_instances(
    xml: str,
    canonical_path: str,
    namespace_map: Dict[str, str] | None,
    list_keys: List[str],
) -> List[Dict[str, str]]:
    if not xml:
        return []
    root = ET.fromstring(xml)
    data = None
    for el in root.iter():
        if el.tag.endswith("data"):
            data = el
            break
    if data is None:
        return []

    path_elems = _path_elems_from_canonical(canonical_path, namespace_map)
    if not path_elems:
        return []

    def find_matching(nodes, idx):
        if idx >= len(path_elems):
            return nodes
        name, ns = path_elems[idx]
        out = []
        for n in nodes:
            for c in list(n):
                cname, cns = _split_tag(c.tag)
                if cname == name and cns == ns:
                    out.append(c)
        return find_matching(out, idx + 1)

    list_nodes = find_matching([data], 0)
    if not list_nodes:
        return []

    list_ns = path_elems[-1][1]
    instances: List[Dict[str, str]] = []
    for node in list_nodes:
        kv: Dict[str, str] = {}
        for child in list(node):
            cname, cns = _split_tag(child.tag)
            if cname in list_keys and cns == list_ns:
                if child.text is not None and child.text.strip():
                    kv[cname] = child.text.strip()
        if kv and all(k in kv for k in list_keys):
            instances.append(kv)
    return instances


def discover_list_instances(
    fetch: callable,
    list_nodes: Iterable[Dict[str, object]],
) -> Dict[str, List[Dict[str, str]]]:
    out: Dict[str, List[Dict[str, str]]] = {}
    for node in list_nodes:
        path = str(node["canonical_path"])
        list_keys = list(node.get("list_keys") or [])
        namespace_map = node.get("namespace_map") or {}
        if not list_keys:
            continue
        filter_xml = build_list_instance_filter_xml(path, namespace_map, list_keys)
        try:
            reply = fetch(filter_xml)
        except Exception as exc:
            if _debug_enabled():
                logger = logging.getLogger(__name__)
                logger.error("list instance fetch failed for %s", path)
                logger.error("list instance filter xml for %s:\n%s", path, filter_xml)
                details = _rpc_error_details(exc)
                meta = {k: v for k, v in details.items() if k != "xml"}
                if meta:
                    logger.error("list instance rpc error details for %s: %s", path, meta)
                if details.get("xml"):
                    logger.error("list instance rpc error xml for %s:\n%s", path, details["xml"])
                logger.exception("list instance fetch exception for %s", path)
            continue
        instances = extract_list_instances(reply, path, namespace_map, list_keys)
        if instances:
            out[path] = instances
    return out


# ---------------------------------------------------------------------------
# Adaptive Discovery with Filter Ladder
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    """Result of discovering instances for a single list path."""

    list_path: str
    status: str  # "success", "empty", "failed"
    attempt_used: Optional[str] = None
    instance_count: int = 0
    duration_ms: int = 0
    rpc_error: Optional[Dict[str, Any]] = None
    instances: List[Dict[str, str]] = field(default_factory=list)
    attempts_tried: List[str] = field(default_factory=list)


def _parse_rpc_error_for_retry(exc: Exception) -> Dict[str, Any]:
    """Parse RPC error to determine retry strategy."""
    details = _rpc_error_details(exc)
    error_tag = str(details.get("error_tag", "")).lower()
    error_type = str(details.get("error_type", "")).lower()
    error_message = str(details.get("error_message", "")).lower()

    # Check for patterns indicating we should try shallow filter
    should_try_shallow = (
        "list key" in error_message
        or "free the list instance" in error_message
        or "cannot" in error_message and "key" in error_message
        or error_tag in ("unknown-element", "invalid-value", "malformed-message")
    )

    # Check for patterns indicating we should try get-config
    should_try_get_config = (
        error_tag in ("access-denied", "operation-failed")
        or "operational" in error_message
        or "state data" in error_message
    )

    # Check for fatal errors that shouldn't retry
    is_fatal = (
        error_tag in ("lock-denied", "in-use", "resource-denied")
        or "connection" in error_message.lower()
        or "transport" in error_message.lower()
    )

    return {
        "error_tag": error_tag,
        "error_type": error_type,
        "error_message": details.get("error_message"),
        "should_try_shallow": should_try_shallow,
        "should_try_get_config": should_try_get_config,
        "is_fatal": is_fatal,
        "raw": {k: v for k, v in details.items() if k != "xml"},
    }


def _try_fetch_instances(
    fetch_fn: Callable[[str], str],
    filter_xml: str,
    canonical_path: str,
    namespace_map: Dict[str, str],
    list_keys: List[str],
) -> Tuple[List[Dict[str, str]], Optional[str], Optional[Dict[str, Any]]]:
    """Try a single fetch attempt.

    Returns:
        (instances, response_xml, rpc_error_dict)
        - instances: list of key-value dicts if successful
        - response_xml: raw response XML if successful
        - rpc_error_dict: error info dict if failed
    """
    try:
        response_xml = fetch_fn(filter_xml)
        instances = extract_list_instances(response_xml, canonical_path, namespace_map, list_keys)
        return instances, response_xml, None
    except Exception as exc:
        error_info = _parse_rpc_error_for_retry(exc)
        return [], None, error_info


def discover_list_instances_adaptive(
    fetch_get: Callable[[str], str],
    fetch_get_config: Callable[[str], str],
    list_nodes: Iterable[Dict[str, object]],
    xpath_supported: bool = False,
    debug_writer: Optional["DiscoveryDebugWriter"] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Tuple[Dict[str, List[Dict[str, str]]], List[DiscoveryResult]]:
    """Discover list instances using an adaptive filter ladder.

    Attempts multiple filter strategies in order until one succeeds:
    1. Keys-only subtree filter with get (operational)
    2. Shallow list-entry filter with get (operational)
    3. Keys-only subtree filter with get-config (running)
    4. Shallow list-entry filter with get-config (running)
    5. XPath filter with get (if xpath_supported)

    Args:
        fetch_get: Function to fetch using NETCONF get
        fetch_get_config: Function to fetch using NETCONF get-config
        list_nodes: Iterable of list node dicts with canonical_path, list_keys, namespace_map
        xpath_supported: Whether the device supports XPath filters
        debug_writer: Optional debug writer for dumping XML request/response
        progress_callback: Optional callback(index, path) for progress reporting

    Returns:
        (instances_dict, results_list) where:
        - instances_dict: mapping of canonical_path -> list of key-value dicts
        - results_list: list of DiscoveryResult for each path attempted
    """
    out: Dict[str, List[Dict[str, str]]] = {}
    results: List[DiscoveryResult] = []

    list_nodes_list = list(list_nodes)

    for idx, node in enumerate(list_nodes_list):
        path = str(node["canonical_path"])
        list_keys = list(node.get("list_keys") or [])
        namespace_map = node.get("namespace_map") or {}

        if progress_callback:
            progress_callback(idx, path)

        if not list_keys:
            results.append(DiscoveryResult(
                list_path=path,
                status="empty",
                attempt_used=None,
                instance_count=0,
            ))
            continue

        start_time = time.time()
        result = _discover_single_path_adaptive(
            path=path,
            list_keys=list_keys,
            namespace_map=namespace_map,
            fetch_get=fetch_get,
            fetch_get_config=fetch_get_config,
            xpath_supported=xpath_supported,
            debug_writer=debug_writer,
        )
        result.duration_ms = int((time.time() - start_time) * 1000)

        results.append(result)
        if result.instances:
            out[path] = result.instances

    return out, results


def _discover_single_path_adaptive(
    path: str,
    list_keys: List[str],
    namespace_map: Dict[str, str],
    fetch_get: Callable[[str], str],
    fetch_get_config: Callable[[str], str],
    xpath_supported: bool,
    debug_writer: Optional["DiscoveryDebugWriter"],
) -> DiscoveryResult:
    """Discover instances for a single path using adaptive filter ladder."""

    attempts_tried: List[str] = []
    last_error: Optional[Dict[str, Any]] = None

    # Define the filter ladder
    # Each entry: (attempt_name, filter_xml_builder, fetch_function)
    ladder: List[Tuple[str, str, Callable[[str], str]]] = []

    # Build filter XMLs
    keys_only_xml = build_list_instance_filter_xml(path, namespace_map, list_keys)
    shallow_xml = build_list_entry_filter_xml(path, namespace_map)

    # Attempt 1: Keys-only with get
    ladder.append(("keys_only_get", keys_only_xml, fetch_get))
    # Attempt 2: Shallow with get
    ladder.append(("shallow_get", shallow_xml, fetch_get))
    # Attempt 3: Keys-only with get-config
    ladder.append(("keys_only_config", keys_only_xml, fetch_get_config))
    # Attempt 4: Shallow with get-config
    ladder.append(("shallow_config", shallow_xml, fetch_get_config))

    # Attempt 5: XPath (only if supported)
    if xpath_supported:
        xpath_xml = build_xpath_filter_xml(path, namespace_map)
        ladder.append(("xpath_get", xpath_xml, fetch_get))

    for attempt_name, filter_xml, fetch_fn in ladder:
        attempts_tried.append(attempt_name)

        if debug_writer:
            debug_writer.write_attempt(
                list_path=path,
                attempt_name=attempt_name,
                request_xml=filter_xml,
            )

        instances, response_xml, error_info = _try_fetch_instances(
            fetch_fn=fetch_fn,
            filter_xml=filter_xml,
            canonical_path=path,
            namespace_map=namespace_map,
            list_keys=list_keys,
        )

        if error_info:
            last_error = error_info
            if debug_writer:
                debug_writer.write_attempt(
                    list_path=path,
                    attempt_name=attempt_name,
                    request_xml=filter_xml,
                    error=str(error_info.get("error_message") or error_info.get("error_tag")),
                )
            # Check if fatal error - stop trying
            if error_info.get("is_fatal"):
                _logger.warning(
                    "Fatal error discovering %s (attempt %s): %s",
                    path, attempt_name, error_info.get("error_message")
                )
                break
            # Otherwise continue to next attempt
            _logger.debug(
                "Attempt %s failed for %s: %s, trying next",
                attempt_name, path, error_info.get("error_tag")
            )
            continue

        # Success - we got a response (even if 0 instances)
        if debug_writer and response_xml:
            debug_writer.write_attempt(
                list_path=path,
                attempt_name=attempt_name,
                request_xml=filter_xml,
                response_xml=response_xml,
            )
            debug_writer.write_summary(
                list_path=path,
                status="success" if instances else "empty",
                attempt_used=attempt_name,
                instance_count=len(instances),
                duration_ms=0,  # Will be set by caller
            )

        status = "success" if instances else "empty"
        return DiscoveryResult(
            list_path=path,
            status=status,
            attempt_used=attempt_name,
            instance_count=len(instances),
            instances=instances,
            attempts_tried=attempts_tried,
        )

    # All attempts failed
    return DiscoveryResult(
        list_path=path,
        status="failed",
        attempt_used=None,
        instance_count=0,
        rpc_error=last_error.get("raw") if last_error else None,
        instances=[],
        attempts_tried=attempts_tried,
    )
