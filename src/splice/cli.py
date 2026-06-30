# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import argparse
import sys
import signal
import re
import logging
import threading
import importlib
import os
import json
import time
import datetime
import uuid
from pathlib import Path
from defusedxml import ElementTree as ET

from splice.config import load_config
from splice.db.session import make_session_factory
from splice.db.device_repository import find_device, get_or_create_device
from splice.db.catalog_query import (
    fetch_catalog_map,
    fetch_catalog_map_with_module,
    fetch_catalog_paths,
    fetch_type_map,
    fetch_list_keys,
    fetch_list_nodes,
)
from splice.db.alias_query import fetch_alias_map
from splice.db.model_query import fetch_models
from splice.db.job_repository import disable_jobs_except, fetch_job_intervals, upsert_jobs
from splice.db.list_instance_repository import fetch_list_instances, replace_list_instances
from splice.db.last_value_repository import (
    delete_last_values,
    fetch_last_values,
    upsert_last_values,
)
from splice.db.history_repository import insert_history
from splice.db.pm_filter_repository import fetch_pm_filters, replace_pm_filters
from splice.alias import ensure_unique_aliases
from splice.gnmi_server import CacheView, serve
import splice.gnmi_pb2 as gnmi_pb2
from splice.netconf.poller import NetconfPoller, parse_xml_to_kv
from splice.netconf.notifications import NotificationListener
from splice.cache_store import MemoryCache
from splice.netconf.client import NetconfClient, NetconfClientPool, _force_close_session
from splice.netconf.set_executor import NetconfSetExecutor
from splice.netconf.trigger_executor import NetconfTriggerExecutor, TriggerValidationError
from splice.health import HealthServer
from splice.trigger_api import TriggerAPIServer
from splice.kafka_publisher import KafkaPublisher
from splice.exporter import export_json, export_csv
from splice.storage import archive_to_file
from splice.planner import QueryTask, plan_batched_queries, plan_module_batched_queries, plan_queries
from splice.pathing import apply_list_keys, is_path_prefix, normalize_path
from splice.expander import (
    build_filter_xml_for_path,
    expand_path,
    expand_path_guarded,
    parse_predicates,
    strip_predicates,
)
from splice.pm import match_selector, parse_pm_current_data, parse_selector
from splice.netconf.list_instance_resolver import (
    discover_list_instances,
    discover_list_instances_adaptive,
    build_list_instance_filter_xml,
    build_list_entry_filter_xml,
    build_xpath_filter_xml,
    extract_list_instances,
)
from splice.netconf.debug import DiscoveryDebugWriter
from splice.setup import SetupReport, write_setup_report, create_setup_report
from splice.jobs import apply_rules, curate_jobs
from splice.runtime import (
    run_discovery,
    run_fetch_yang,
    run_build_catalog,
    run_build_catalog_with_paths,
    run_build_aliases,
    run_init_jobs,
)
from splice.db.migrator import (
    MigrationLockError,
    ensure_database,
    get_pending_migrations,
    run_migrations,
)
from splice.doctor import run_all_checks
from sqlalchemy import text


def _make_manager(cfg):
    try:
        from ncclient import manager
    except Exception as e:
        raise RuntimeError("ncclient is required for NETCONF operations") from e

    return manager.connect(
        host=cfg.device.host,
        port=cfg.device.port,
        username=cfg.device.username,
        password=cfg.device.password or "",
        hostkey_verify=False,
        allow_agent=False,
        look_for_keys=False,
    )


def _make_manager_fn(cfg):
    """Return a callable that creates a fresh NETCONF manager."""
    def factory():
        return _make_manager(cfg)
    return factory


def _make_pool_from_config(client, cfg):
    """Create NetconfClientPool with settings from poller config."""
    return NetconfClientPool(
        client,
        reconnect_delay_sec=cfg.poller.reconnect_delay_sec,
        circuit_breaker_threshold=cfg.poller.circuit_breaker_threshold,
        circuit_breaker_cooldown_sec=cfg.poller.circuit_breaker_cooldown_sec,
        auth_failure_cooldown_sec=cfg.poller.auth_failure_cooldown_sec,
    )


def _filter_list_nodes_for_jobs(list_nodes, jobs, list_keys):
    if not jobs:
        return list_nodes
    needed_list_paths = set()
    for path in jobs.keys():
        base_path = strip_predicates(path)
        parts = [p for p in base_path.strip("/").split("/") if p]
        cur_parts = []
        for part in parts:
            cur_parts.append(part)
            cur_path = "/" + "/".join(cur_parts)
            if cur_path in list_keys:
                needed_list_paths.add(cur_path)
    if not needed_list_paths:
        return list_nodes
    return [n for n in list_nodes if n.get("canonical_path") in needed_list_paths]


def _strip_ns(tag: str) -> tuple[str, str]:
    if "}" in tag:
        ns, local = tag[1:].split("}", 1)
        return local, ns
    return tag, ""


def _inspect_components(manager, pattern: str | None, limit: int) -> None:
    filter_xml = (
        '<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
        '<components xmlns="http://openconfig.net/yang/platform">'
        "<component><name/></component>"
        "</components></filter>"
    )
    reply = manager.get(filter_xml)
    root = ET.fromstring(reply.data_xml)
    names = []
    for el in root.iter():
        local, _ = _strip_ns(el.tag)
        if local == "name" and el.text:
            names.append(el.text.strip())
    if pattern:
        try:
            rx = re.compile(pattern)
            names = [n for n in names if rx.search(n)]
        except re.error as e:
            print(f"invalid discovery.component_pattern: {e}")
            return
    if not names:
        print("component inspection: no component names found")
        return
    names = names[: max(1, limit)]
    for name in names:
        flt = (
            '<filter xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
            '<components xmlns="http://openconfig.net/yang/platform">'
            f"<component><name>{name}</name></component>"
            "</components></filter>"
        )
        try:
            data = manager.get(flt).data_xml
        except Exception as e:
            print(f"component inspection failed for {name}: {e}")
            continue
        try:
            doc = ET.fromstring(data)
        except Exception:
            print(f"component inspection parse failed for {name}")
            continue
        children = set()
        for comp in doc.iter():
            local, ns = _strip_ns(comp.tag)
            if local != "component":
                continue
            for child in list(comp):
                child_local, child_ns = _strip_ns(child.tag)
                if child_local == "name":
                    continue
                label = child_local if not child_ns else f"{child_local}<{child_ns}>"
                children.add(label)
        if children:
            print(f"component {name}: {', '.join(sorted(children))}")
        else:
            print(f"component {name}: no subtree")


def _resolve_all_list_instances(
    cfg,
    session,
    dev,
    list_nodes,
    batch_size: int,
    sleep_sec: float,
):
    if not list_nodes:
        print("no list nodes found for list instance resolution")
        return 0
    client = NetconfClient(
        host=cfg.device.host,
        port=cfg.device.port,
        username=cfg.device.username,
        password=cfg.device.password or "",
    )
    pool = _make_pool_from_config(client, cfg)
    total = 0
    try:
        import time

        step = max(1, batch_size)
        resume_batch = max(1, getattr(cfg.discovery, "resume_batch", 1))
        heartbeat_sec = float(getattr(cfg.discovery, "heartbeat_sec", 0.0) or 0.0)
        total_nodes = len(list_nodes)
        start_idx = (resume_batch - 1) * step
        if start_idx >= len(list_nodes):
            print(f"resume_batch {resume_batch} is past total batches; nothing to do")
            return 0
        if resume_batch > 1:
            print(f"resuming list instance discovery at batch {resume_batch}")
        for idx in range(start_idx, len(list_nodes), step):
            batch = list_nodes[idx : idx + step]
            batch_no = idx // step + 1
            print(f"list instance batch {batch_no}: nodes {idx + 1}-{idx + len(batch)} of {total_nodes}")
            batch_started = time.monotonic()
            stop_event = threading.Event()
            hb_thread = None
            if heartbeat_sec > 0:
                def _heartbeat():
                    while not stop_event.wait(heartbeat_sec):
                        elapsed = time.monotonic() - batch_started
                        print(f"batch {batch_no} still running ({elapsed:.0f}s)")
                hb_thread = threading.Thread(target=_heartbeat, daemon=True)
                hb_thread.start()
            resolved = discover_list_instances(
                fetch=lambda fx: pool.get(fx).data_xml,
                list_nodes=batch,
            )
            if hb_thread:
                stop_event.set()
                hb_thread.join(timeout=1.0)
            if resolved:
                list_paths = [n["canonical_path"] for n in batch]
                deduped = {}
                for list_path, entries in resolved.items():
                    seen = set()
                    unique_entries = []
                    for entry in entries:
                        key = tuple(sorted((k, str(v)) for k, v in entry.items()))
                        if key in seen:
                            continue
                        seen.add(key)
                        unique_entries.append(entry)
                    deduped[list_path] = unique_entries
                batch_instances = 0
                for list_path, entries in sorted(deduped.items()):
                    count = len(entries)
                    batch_instances += count
                    print(f"  {list_path}: {count} instances")
                elapsed = time.monotonic() - batch_started
                print(f"batch {batch_no} resolved {batch_instances} instances in {elapsed:.1f}s")
                total += replace_list_instances(session, dev.id, deduped, list_paths=list_paths)
            else:
                for node in batch:
                    print(f"  {node['canonical_path']}: 0 instances")
                elapsed = time.monotonic() - batch_started
                print(f"batch {batch_no} resolved 0 instances in {elapsed:.1f}s")
            if sleep_sec > 0 and idx + step < len(list_nodes):
                time.sleep(sleep_sec)
        # Reset resume_batch after successful completion to prevent
        # stale values from skipping batches on subsequent runs
        if hasattr(cfg.discovery, "resume_batch") and cfg.discovery.resume_batch > 1:
            logging.info(
                "list instance discovery complete — resume_batch should be "
                "reset to 1 in config to avoid skipping batches on next run"
            )
    finally:
        client.close()
    return total


def _local_name(seg: str) -> str:
    if ":" in seg:
        seg = seg.split(":", 1)[1]
    return seg.split("[", 1)[0]


# Selector parsing/matching lives in pm.py so doctor checks can use it
# without importing cli (cli imports doctor — circular otherwise).
_parse_selector = parse_selector
_match_selector = match_selector


def _pm_root_from_path(path: str) -> str | None:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return None
    locals_ = [_local_name(p).lower() for p in parts]
    if not any(l in ("mon-type", "mon-val") for l in locals_):
        return None
    for i, lname in enumerate(locals_):
        if "pm" in lname and ("current" in lname or "curr" in lname):
            return "/" + "/".join(parts[: i + 1])
    for i, lname in enumerate(locals_):
        if lname in ("pm-current-data", "pm-current"):
            return "/" + "/".join(parts[: i + 1])
    return None


def _namespace_map_for_root(catalog: dict, root: str) -> dict | None:
    entry = catalog.get(root)
    if entry and entry.get("namespace_map"):
        return entry.get("namespace_map")
    for path, info in catalog.items():
        if path.startswith(root + "/"):
            return info.get("namespace_map")
    return None


def _instance_map_for_path(path: str, list_keys: dict, list_instances: dict) -> dict:
    instance_map = {}
    for list_path in list_keys.keys():
        base = strip_predicates(list_path)
        if path.startswith(base):
            entries = list_instances.get(list_path)
            if entries:
                instance_map[list_path] = entries[0]
    return instance_map


def _pick_pm_fanout_list_path(
    root: str,
    list_keys: dict,
    list_instances: dict,
    preferred: str | None,
) -> str | None:
    if (
        preferred
        and list_instances.get(preferred)
        and root.startswith(strip_predicates(preferred))
    ):
        # Only honour the preferred fanout list for roots actually under it;
        # unrelated roots (e.g. card PM when the preference is an interface
        # list) must fan over their own list ancestry.
        return preferred
    candidates = []
    for list_path in list_keys.keys():
        base = strip_predicates(list_path)
        if root.startswith(base) and list_instances.get(list_path):
            candidates.append(list_path)
    if not candidates:
        return None
    for list_path in candidates:
        leaf = list_path.rsplit("/", 1)[-1]
        if "interface" in _local_name(leaf).lower():
            return list_path
    return candidates[0]


def _discover_pm_filters(cfg, session, dev):
    catalog = fetch_catalog_map(session, dev.id)
    list_keys = fetch_list_keys(session, dev.id)
    list_instances = fetch_list_instances(session, dev.id)
    candidates = set()
    for path in catalog.keys():
        root = _pm_root_from_path(path)
        if root:
            candidates.add(root)
    if not candidates:
        return []
    roots = sorted(candidates)[: max(1, cfg.discovery.pm_max_paths)]

    selector_clauses, selector_ops = [], []
    if cfg.discovery.pm_selector:
        try:
            selector_clauses, selector_ops = _parse_selector(cfg.discovery.pm_selector)
        except ValueError as e:
            logging.warning("pm selector ignored: %s", e)

    # The configured fanout list (e.g. the interface list). Roots under it
    # collapse into ONE subtree filter per instance: a filter cut at the keyed
    # list element returns every PM domain below that instance in a single
    # RPC, instead of one narrow RPC per root x instance (hundreds of round
    # trips per sweep on session-throttled devices). The selector only ever
    # applies to this list.
    fanout_list = cfg.discovery.pm_selector_list_path
    fanout_instances: list = []
    if fanout_list:
        fanout_instances = list(list_instances.get(fanout_list) or [])
        all_count = len(fanout_instances)
        if selector_clauses:
            fanout_instances = [
                i
                for i in fanout_instances
                if _match_selector(i, selector_clauses, selector_ops)
            ]
            if all_count and not fanout_instances:
                logging.warning(
                    "pm selector %r matched 0 of %d instances of %s — no "
                    "instance-keyed PM filters will be built. The device may "
                    "have been re-provisioned; compare the selector against "
                    "`refresh-list-instances` output.",
                    cfg.discovery.pm_selector,
                    all_count,
                    fanout_list,
                )
        elif not cfg.discovery.pm_fanout:
            fanout_instances = fanout_instances[:1]
        if cfg.discovery.pm_fanout_limit and cfg.discovery.pm_fanout_limit > 0:
            fanout_instances = fanout_instances[: cfg.discovery.pm_fanout_limit]

    filters = []
    collapsed_emitted = False
    for root in roots:
        if (
            fanout_list
            and fanout_instances
            and root.startswith(strip_predicates(fanout_list))
        ):
            if collapsed_emitted:
                continue
            collapsed_emitted = True
            for inst in fanout_instances:
                # Pin ancestor lists (e.g. managed-element) to their first
                # instance, then key the fanout list to this instance.
                inst_map = _instance_map_for_path(fanout_list, list_keys, list_instances)
                inst_map[fanout_list] = inst
                filters.append(
                    {
                        "root_path": fanout_list,
                        "filter_xml": build_filter_xml_for_path(
                            fanout_list,
                            _namespace_map_for_root(catalog, fanout_list),
                            list_keys,
                            inst_map,
                        ),
                        "list_path": fanout_list,
                        "key_values": inst,
                    }
                )
            continue

        # Roots outside the fanout list get one narrow per-root filter with
        # ancestor lists pinned to their first instance (e.g. card 1/1).
        # parse_pm_current_data keys the results from the reply itself, so
        # whatever instances the device returns are still distinguishable.
        filters.append(
            {
                "root_path": root,
                "filter_xml": build_filter_xml_for_path(
                    root,
                    _namespace_map_for_root(catalog, root),
                    list_keys,
                    _instance_map_for_path(root, list_keys, list_instances),
                ),
                "list_path": None,
                "key_values": None,
            }
        )
    adapter_modules = cfg.discovery.pm_adapter_modules or []
    for mod_name in adapter_modules:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, "discover_pm_filters", None)
            if callable(fn):
                extra = fn(cfg, session, dev) or {}
                if isinstance(extra, dict):
                    extra = [{"root_path": k, "filter_xml": v} for k, v in extra.items()]
                filters.extend(extra)
        except Exception:
            logging.exception("pm adapter %s failed", mod_name)
    return filters


def _init_jobs_curated(cfg, session, device_id, paths, interval) -> str:
    """Create polling jobs for catalog paths, honouring metrics curation.
    Returns the human-readable summary for the caller to print."""
    if cfg.metrics.enabled and (cfg.metrics.include or []):
        curated = curate_jobs(
            paths, cfg.metrics.include, cfg.metrics.default_interval_sec
        )
        if not curated:
            logging.warning(
                "metrics.include matched 0 of %d catalog paths — no curated "
                "jobs created. Patterns match substrings of both qualified "
                "and prefix-stripped paths.",
                len(paths),
            )
            return "0 curated jobs (patterns matched nothing)"
        count = upsert_jobs(session, device_id, curated)
        disabled = 0
        if cfg.metrics.disable_uncurated:
            disabled = disable_jobs_except(session, device_id, set(curated))
        return (
            f"{count} curated jobs from {len(paths)} catalog paths"
            + (f", {disabled} uncurated jobs disabled" if disabled else "")
        )
    count = run_init_jobs(session, device_id, paths, interval)
    if cfg.poller.rules:
        overrides = apply_rules(paths, cfg.poller.rules)
        if overrides:
            upsert_jobs(session, device_id, overrides)
    return f"{count} jobs ({interval}s interval)"


def _setup_report_path(args) -> str:
    """Reports live next to the device config so multi-device setups don't
    overwrite each other; without --config fall back to the CWD (legacy)."""
    config = getattr(args, "config", None)
    if config:
        return str(Path(config).resolve().parent / "setup_report.json")
    return "./setup_report.json"


def _resolve_report_for_read(args) -> str:
    """Reader counterpart: explicit --report wins, then the config-dir report,
    then the legacy CWD file."""
    explicit = getattr(args, "report", None)
    if explicit:
        return explicit
    candidate = _setup_report_path(args)
    if Path(candidate).exists():
        return candidate
    return "./setup_report.json"


def _maybe_prune_catalog(session, device_id, built_paths, force: bool) -> bool:
    """Prune stale catalog paths after a build. Returns False when refused by
    the safety threshold (new build < 50% of stored paths), which usually
    means the build itself is wrong, not the stored catalog."""
    from splice.db.catalog_repository import prune_catalog_paths

    existing = len(fetch_catalog_paths(session, device_id))
    if existing and len(built_paths) < 0.5 * existing and not force:
        print(
            f"  prune refused: this build produced {len(built_paths)} paths but "
            f"{existing} are stored (<50%). A likely cause is a different "
            f"--include-config than the original build, or YANG parse failures. "
            f"Re-run with --force to prune anyway."
        )
        return False
    counts = prune_catalog_paths(session, device_id, built_paths)
    print(
        "  pruned: "
        + ", ".join(f"{table}={n}" for table, n in counts.items())
    )
    return True


def _normalize_instance_entries(entries) -> list:
    return sorted(
        tuple(sorted((k, str(v)) for k, v in entry.items())) for entry in entries or []
    )


def _refresh_pm_filters_for_drift(cfg, session, dev, pm_filters, fetch):
    """Re-resolve the PM fanout lists against the live device and rebuild PM
    filters when instances drifted (e.g. line ports were re-provisioned).

    Conservative on failure: a transient fetch error or an empty answer keeps
    the cached instances and filters — drift refresh must never make things
    worse than stale.
    """
    fanout_paths = sorted({row.list_path for row in pm_filters if row.list_path})
    if not fanout_paths:
        return pm_filters
    wanted = set(fanout_paths)
    list_nodes = [
        n for n in fetch_list_nodes(session, dev.id) if n["canonical_path"] in wanted
    ]
    if not list_nodes:
        return pm_filters
    try:
        resolved = discover_list_instances(fetch=fetch, list_nodes=list_nodes)
    except Exception:
        logging.exception("pm drift refresh failed; keeping cached PM filters")
        return pm_filters
    resolved = {k: v for k, v in (resolved or {}).items() if v}
    if not resolved:
        logging.warning(
            "pm drift refresh: device returned no instances for %s; "
            "keeping cached PM filters",
            ", ".join(fanout_paths),
        )
        return pm_filters
    cached = fetch_list_instances(session, dev.id)
    changed = [
        lp
        for lp in resolved
        if _normalize_instance_entries(cached.get(lp))
        != _normalize_instance_entries(resolved[lp])
    ]
    if not changed:
        logging.info("pm fanout instances unchanged; keeping PM filters")
        return pm_filters
    logging.warning(
        "PM fanout instances drifted on %s — refreshing list instances and "
        "rebuilding PM filters",
        ", ".join(changed),
    )
    replace_list_instances(
        session, dev.id, {lp: resolved[lp] for lp in changed}, list_paths=changed
    )
    new_filters = _discover_pm_filters(cfg, session, dev)
    if not new_filters:
        logging.warning("PM filter rebuild produced no filters; keeping previous set")
        return pm_filters
    replace_pm_filters(session, dev.id, new_filters)
    return fetch_pm_filters(session, dev.id)


def _safe_slug(path: str) -> str:
    if not path:
        return "root"
    slug = path.strip("/").replace("/", "__")
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", slug)
    return slug or "root"


def _coerce_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    try:
        from lxml import etree as lxml_etree

        if isinstance(content, lxml_etree._Element):
            return lxml_etree.tostring(content, encoding="unicode")
    except Exception:
        pass
    try:
        return str(content)
    except Exception:
        return ""


def _write_text(path: Path, content) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(_coerce_text(content))


def _rpc_error_payload(exc: Exception) -> dict:
    payload = {}
    for attr in ("error_tag", "error_type", "error_severity", "error_message", "error_info"):
        val = getattr(exc, attr, None)
        if val:
            payload[attr] = val
    xml = getattr(exc, "xml", None) or getattr(exc, "_raw", None)
    if xml:
        payload["xml"] = _coerce_text(xml)
    return payload


class NetconfDisconnectedError(RuntimeError):
    pass


def _is_auth_failure(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "authentication" in msg and "failed" in msg


def _netconf_call(client: NetconfClient, fn, max_retries: int = 3, backoff_sec: float = 0.5):
    last = None
    for i in range(max(1, max_retries)):
        try:
            mgr = client.manager()
            return fn(mgr)
        except Exception as exc:
            if _is_auth_failure(exc):
                raise
            if "Not connected to NETCONF server" in str(exc):
                raise NetconfDisconnectedError(str(exc)) from exc
            last = exc
            client.close()
            if i + 1 < max_retries:
                time.sleep(backoff_sec * (2**i))
    if last is not None:
        raise last
    raise RuntimeError("NETCONF call failed with max_retries=0")


def _run_list_instance_attempt(
    client: NetconfClient,
    call_fn,
    filter_xml: str,
    path: str,
    list_keys: list,
    namespace_map: dict | None,
) -> dict:
    start_wall = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    started = time.monotonic()
    response_xml = None
    instance_count = 0
    error_payload = None
    exception_str = None
    success = False
    try:
        reply = _netconf_call(client, lambda mgr: call_fn(mgr, filter_xml))
        response_xml = getattr(reply, "xml", None) or getattr(reply, "data_xml", None) or str(reply)
        instances = extract_list_instances(response_xml, path, namespace_map, list_keys)
        instance_count = len(instances)
        success = instance_count > 0
    except NetconfDisconnectedError:
        raise
    except Exception as exc:
        exception_str = f"{type(exc).__name__}: {exc}"
        error_payload = _rpc_error_payload(exc)
        if error_payload and error_payload.get("xml"):
            response_xml = error_payload.get("xml")
    ended = time.monotonic()
    end_wall = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    return {
        "filter_xml": filter_xml,
        "response_xml": response_xml,
        "success": success,
        "instance_count": instance_count,
        "exception": exception_str,
        "error": error_payload,
        "start_time": start_wall,
        "end_time": end_wall,
        "duration_sec": round(ended - started, 3),
    }


def _cmd_init(args):
    """Interactive config generator."""
    import getpass
    import yaml

    name = getattr(args, "name", None)
    host = getattr(args, "host", None)
    username = getattr(args, "username", None)
    password = getattr(args, "password", None)
    port = getattr(args, "port", 830)
    gnmi_port = getattr(args, "gnmi_port", 9339)
    db_url = getattr(args, "db_url", None)
    non_interactive = getattr(args, "non_interactive", False)

    if not non_interactive:
        name = name or input("Device name (e.g. teraflex): ").strip()
        host = host or input("Device host/IP: ").strip()
        username = username or input("NETCONF username [admin]: ").strip() or "admin"
        password = password or os.environ.get("NETCONF_PASSWORD") or getpass.getpass("NETCONF password: ")
        port_str = input(f"NETCONF port [{port}]: ").strip()
        if port_str:
            port = int(port_str)
        gnmi_port_str = input(f"gNMI server port [{gnmi_port}]: ").strip()
        if gnmi_port_str:
            gnmi_port = int(gnmi_port_str)
        db_url = db_url or input("Database URL [postgresql+psycopg://postgres:postgres@localhost:5432/splice]: ").strip()
        if not db_url:
            db_url = "postgresql+psycopg://postgres:postgres@localhost:5432/splice"
    else:
        if not name:
            print("ERROR: --name is required in non-interactive mode")
            return 1
        if not host:
            print("ERROR: --host is required in non-interactive mode")
            return 1
        username = username or "admin"
        password = password or os.environ.get("NETCONF_PASSWORD") or ""
        db_url = db_url or "postgresql+psycopg://postgres:postgres@localhost:5432/splice"

    # Create device directory
    device_dir = Path(f"devices/{name}")
    device_dir.mkdir(parents=True, exist_ok=True)
    yang_dir = device_dir / "yang"
    yang_dir.mkdir(parents=True, exist_ok=True)

    # Write password file
    password_file = device_dir / "password"
    password_file.write_text(password + "\n", encoding="utf-8")
    password_file.chmod(0o600)

    # Generate config
    config = {
        "device": {
            "host": host,
            "port": port,
            "username": username,
            "password_file": str(device_dir / "password"),
            "name": name,
        },
        "db": {"url": db_url},
        "yang_dir": str(yang_dir.resolve()),
        "gnmi": {
            "bind": "0.0.0.0",
            "port": gnmi_port,
            "heartbeat_sec": 10,
            "expansion_mode": "direct",
            "max_expanded_paths_per_base": 10000,
            "max_total_expanded_paths": 100000,
        },
        "poller": {
            "default_interval_sec": 30,
            "batch_enabled": True,
            "batch_mode": "top",
            "preserve_namespaces": False,
            "rules": [],
        },
        "discovery": {
            "resolve_all_lists": False,
            "list_batch_size": 50,
            "list_sleep_sec": 0.5,
        },
        "cache": {"ttl_sec": 60, "warm_start": False},
        "health": {"enabled": True, "bind": "0.0.0.0", "port": 9809},
    }

    config_path = device_dir / "config.yaml"
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"Created {config_path}")
    print(f"Created {password_file}")
    print(f"Created {yang_dir}/")
    print()
    print("Next steps:")
    print(f"  splice --config {config_path} setup")
    print(f"  splice --config {config_path} run")
    return 0


def _cmd_quickstart(args):
    """Point-at-IP onboarding: generate config, run setup, start the server."""
    password = None
    if args.password_file:
        try:
            password = Path(args.password_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"ERROR: cannot read --password-file: {exc}")
            return 1
    password = password or os.environ.get("NETCONF_PASSWORD")
    if not password:
        print("ERROR: provide the device password via --password-file or NETCONF_PASSWORD")
        return 1

    # '.' and ':' map to DIFFERENT characters so an IPv4 host (10.0.0.1) and
    # an IPv6-ish host (10:0:0:1) can't derive the same device name (and
    # thereby share a config dir + database).
    name = args.name or args.host.replace(".", "_").replace(":", "-")
    existing_config = Path("devices") / name / "config.yaml"
    if existing_config.exists():
        print(
            f"ERROR: {existing_config} already exists — quickstart will not "
            f"overwrite it. Use it directly:\n"
            f"  splice --config {existing_config} setup && "
            f"splice --config {existing_config} run\n"
            f"or pass a different --name."
        )
        return 1
    db_url = args.db_url or (
        f"postgresql+psycopg://postgres:postgres@localhost:5432/splice_{name}"
    )

    from types import SimpleNamespace

    rc = _cmd_init(
        SimpleNamespace(
            name=name,
            host=args.host,
            username=args.username,
            password=password,
            port=args.port,
            gnmi_port=args.gnmi_port,
            db_url=db_url,
            non_interactive=True,
        )
    )
    if rc:
        return rc

    config_path = str(Path("devices") / name / "config.yaml")
    print()
    print(f"Running setup for {args.host} ...")
    rc = main(["--config", config_path, "setup"])
    if rc:
        print(f"Setup failed. After fixing, rerun: splice --config {config_path} setup")
        return rc
    if args.setup_only:
        print(f"Setup complete. Start the server with: splice --config {config_path} run")
        return 0
    print()
    print(f"Starting gNMI server on :{args.gnmi_port} ...")
    return main(["--config", config_path, "run"])


def _migration_fix_hints(exc: Exception) -> list[str]:
    """Return actionable operator hints for migration failures."""
    text_lower = str(exc).lower()
    if isinstance(exc, MigrationLockError):
        return [
            "Another setup/migrate is already running against this DB. Wait and retry.",
            "Run: splice --config <config> doctor",
            "Inspect lock blockers in Postgres: SELECT pid, state, wait_event_type, wait_event, now()-xact_start AS age, query FROM pg_stat_activity WHERE datname = current_database() ORDER BY xact_start NULLS LAST;",
        ]
    if "lock timeout" in text_lower or "deadlock detected" in text_lower:
        return [
            "A migration statement waited on a DB lock and timed out.",
            "Ensure no long-running transactions are open, then rerun migrate/setup.",
            "Inspect lock blockers in Postgres: SELECT pid, state, wait_event_type, wait_event, now()-xact_start AS age, query FROM pg_stat_activity WHERE datname = current_database() ORDER BY xact_start NULLS LAST;",
        ]
    return [
        "Run: splice --config <config> doctor",
        "If this persists, run migrations separately first: splice --config <config> migrate",
    ]


def _cmd_migrate(args, cfg):
    """Apply pending database migrations."""
    from splice.db.session import make_engine

    created = ensure_database(cfg.db.url)
    if created:
        print(f"Created database.")

    engine = make_engine(cfg.db.url)
    dry_run = getattr(args, "dry_run", False)

    if dry_run:
        pending = get_pending_migrations(engine)
        if pending:
            print(f"{len(pending)} pending migrations:")
            for ver, path in pending:
                print(f"  {ver} ({path.name})")
        else:
            print("All migrations applied.")
        return 0

    try:
        count = run_migrations(engine)
    except Exception as exc:
        print("Migration failed.")
        print(f"  Error: {exc}")
        for line in _migration_fix_hints(exc):
            print(f"  Fix: {line}")
        return 1
    if count:
        print(f"{count} migrations applied.")
    else:
        print("All migrations already applied.")
    return 0


def _cmd_setup(args, cfg):
    """Single-command full setup pipeline."""
    from splice.db.session import make_engine, make_session_factory

    interval = getattr(args, "interval", None) or cfg.poller.default_interval_sec
    include_config = getattr(args, "include_config", False)
    skip_migrate = getattr(args, "skip_migrate", False)
    skip_list_instances = getattr(args, "skip_list_instances", False)

    # Step 1: Check database
    print("[1/8] Checking database...", end="", flush=True)
    try:
        created = ensure_database(cfg.db.url)
        if created:
            print("           created")
        else:
            print("           OK")
    except Exception as exc:
        print(f"           FAILED")
        print(f"  Error: {exc}")
        print(f"  Fix: Check db.url in config. Verify PostgreSQL is running.")
        return 1

    engine = make_engine(cfg.db.url)
    SessionFactory = make_session_factory(cfg.db.url)

    # Step 2: Check device connectivity
    print("[2/8] Checking device connectivity...", end="", flush=True)
    import socket
    try:
        with socket.create_connection((cfg.device.host, cfg.device.port), timeout=10):
            pass
        print("  OK")
    except Exception as exc:
        print(f"  FAILED")
        print(f"  Error: Cannot reach {cfg.device.host}:{cfg.device.port}: {exc}")
        print(f"  Fix: Check device is powered on and network-reachable.")
        return 1

    # Step 3: Run migrations
    if not skip_migrate:
        print("[3/8] Running migrations...", end="", flush=True)
        try:
            count = run_migrations(engine)
            if count:
                print(f"          {count} applied")
            else:
                print(f"          up to date")
        except Exception as exc:
            print("          FAILED")
            print(f"  Error: {exc}")
            for line in _migration_fix_hints(exc):
                print(f"  Fix: {line}")
            return 1
    else:
        print("[3/8] Running migrations...          skipped")

    # Step 4-7 of 8: Run discovery pipeline with NETCONF
    with SessionFactory() as session:
        dev = get_or_create_device(
            session,
            host=cfg.device.host,
            port=cfg.device.port,
            username=cfg.device.username,
            password_ref=None,
            name=cfg.device.name,
        )

        # Step 4: Discover YANG modules
        print("[4/8] Discovering YANG modules...", end="", flush=True)
        try:
            with _make_manager(cfg) as mgr:
                module_count = run_discovery(cfg, mgr, session)
            print(f"    {module_count} modules")
        except Exception as exc:
            print(f"    FAILED")
            print(f"  Error: {exc}")
            print(f"  Fix: Check NETCONF credentials and device connectivity.")
            return 1

        # Step 5: Fetch YANG schemas
        print("[5/8] Fetching YANG schemas...", end="", flush=True)
        try:
            reconnect = _make_manager_fn(cfg)
            mgr = _make_manager(cfg)
            try:
                yang_count = run_fetch_yang(cfg, mgr, session, reconnect_fn=reconnect)
            finally:
                try:
                    mgr.close_session()
                except Exception:
                    _force_close_session(mgr)
            print(f"       {yang_count} files")
        except Exception as exc:
            print(f"       FAILED")
            print(f"  Error: {exc}")
            return 1

        # Step 6: Build catalog + aliases
        print("[6/8] Building catalog + aliases...", end="", flush=True)
        try:
            catalog_count, built_paths = run_build_catalog_with_paths(
                cfg, session, include_config=include_config
            )
            print(f"  {catalog_count} paths")
            if getattr(args, "prune", False):
                _maybe_prune_catalog(
                    session, dev.id, built_paths, getattr(args, "force", False)
                )
            paths = fetch_catalog_paths(session, dev.id)
            alias_count = run_build_aliases(session, dev.id, paths)
        except Exception as exc:
            print(f"  FAILED")
            print(f"  Error: {exc}")
            return 1

        # Optionally resolve list instances
        if not skip_list_instances and cfg.discovery.resolve_all_lists:
            list_nodes = fetch_list_nodes(session, dev.id)
            if list_nodes:
                print(f"        Resolving list instances for {len(list_nodes)} paths...")
                total = _resolve_all_list_instances(
                    cfg, session, dev, list_nodes,
                    cfg.discovery.list_batch_size,
                    cfg.discovery.list_sleep_sec,
                )
                print(f"        List instances added: {total}")

        # PM filter discovery
        if cfg.discovery.pm_discovery:
            pm_filters = _discover_pm_filters(cfg, session, dev)
            stored = replace_pm_filters(session, dev.id, pm_filters) if pm_filters else 0
            if stored:
                print(f"        PM filters stored: {stored}")

        # Step 7: Initialize polling jobs
        print("[7/8] Initializing polling jobs...", end="", flush=True)
        try:
            paths = fetch_catalog_paths(session, dev.id)
            summary = _init_jobs_curated(cfg, session, dev.id, paths, interval)
            print(f"   {summary}")
        except Exception as exc:
            print(f"   FAILED")
            print(f"  Error: {exc}")
            return 1

    config_path = getattr(args, "config", "config.yaml")

    # Step 8: Generate Grafana dashboards. Best-effort: a failure here
    # must not invalidate the pipeline — everything the bridge needs to
    # serve gNMI is already in place.
    print("[8/8] Generating Grafana dashboards...", end="", flush=True)
    try:
        from splice.grafana_dashboards import generate_dashboards_for_config
        written = generate_dashboards_for_config(config_path)
        print(f" {len(written)} files")
        for p in written:
            print(f"        {p}")
    except Exception as exc:
        print(" skipped")
        print(f"  Warning: dashboard generation failed: {exc}")
        print(f"  Retry later with: splice --config {config_path} dashboards")

    print()
    print("SETUP COMPLETE")
    print(f"  Next: splice --config {config_path} run")
    return 0


def _cmd_dashboards(args) -> int:
    """Generate Grafana dashboards from the device's YANG catalog."""
    from splice.grafana_dashboards import generate_dashboards_for_config

    try:
        written = generate_dashboards_for_config(
            args.config,
            out_dir=args.out,
            bucket=args.bucket,
            measurement=args.measurement,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        return 1
    for p in written:
        print(p)
    return 0


def _cmd_status(args, cfg, session, dev) -> int:
    """Show live health/poller stats; fall back to DB freshness when the
    bridge (or its health endpoint) is not running."""
    import urllib.error
    import urllib.request

    payload = None
    host = None
    if cfg.health.enabled:
        bind = cfg.health.bind
        if bind == "0.0.0.0":
            host = "127.0.0.1"
        elif bind == "::":
            host = "::1"  # IPv6 wildcard bind → IPv6 loopback probe
        else:
            host = bind
        # IPv6 literals need brackets in URLs.
        url_host = f"[{host}]" if ":" in host else host
        url = f"http://{url_host}:{cfg.health.port}/health"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 503 (degraded) still carries a JSON body.
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = None
        except Exception:
            payload = None

    if payload is not None:
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        source_host = f"[{host}]" if host and ":" in host else host
        print(f"source: http://{source_host}:{cfg.health.port}/health")
        for key in ("db", "netconf", "gnmi"):
            if key in payload:
                print(f"  {key}: {'OK' if payload[key] else 'DEGRADED'}")
        ncs = payload.get("netconf_status") or {}
        if ncs:
            print(f"  circuit_breaker_open: {ncs.get('circuit_breaker_open', False)}")
        for name in ("poller", "pm_poller"):
            stats = payload.get(name)
            if not stats:
                continue
            print(
                f"  {name}: tasks={stats.get('task_count')} "
                f"disabled={stats.get('disabled_count')} "
                f"failure_limit={stats.get('failure_limit')}"
            )
            for interval, sweep in sorted((stats.get("sweeps") or {}).items()):
                age = ""
                completed = sweep.get("completed_at")
                if completed:
                    age = f" ({int(time.time() - completed)}s ago)"
                print(
                    f"    every {interval}s: {sweep.get('ok')} ok / "
                    f"{sweep.get('fail')} fail / {sweep.get('disabled')} skipped, "
                    f"sweep took {sweep.get('duration_sec')}s{age}"
                )
            for item in (stats.get("disabled") or [])[:10]:
                print(f"    disabled: {item['path']} — {item['error']}")
        return 0

    print("bridge health endpoint unreachable — falling back to DB freshness")
    if dev is None:
        print("  no device row in the database — run `setup` first")
        return 1
    rows = session.execute(
        text(
            "SELECT CASE"
            " WHEN updated_at > now() - interval '1 minute' THEN '<1m'"
            " WHEN updated_at > now() - interval '5 minutes' THEN '<5m'"
            " WHEN updated_at > now() - interval '1 hour' THEN '<1h'"
            " ELSE 'older' END AS bucket, count(*)"
            " FROM last_value WHERE device_id = :dev GROUP BY 1 ORDER BY 1"
        ),
        {"dev": dev.id},
    ).fetchall()
    if not rows:
        print("  no telemetry rows in last_value for this device")
        return 1
    if args.json:
        print(json.dumps({"last_value_age": {b: c for b, c in rows}}, indent=2))
        return 0
    for bucket, count in rows:
        print(f"  last_value {bucket}: {count}")
    return 0


def _cmd_doctor(args, cfg):
    """Run pre-flight validation checks."""
    from splice.db.session import make_engine, make_session_factory

    engine = make_engine(cfg.db.url)
    SessionFactory = make_session_factory(cfg.db.url)

    with SessionFactory() as session:
        dev = get_or_create_device(
            session,
            host=cfg.device.host,
            port=cfg.device.port,
            username=cfg.device.username,
            password_ref=None,
            name=cfg.device.name,
        )
        results = run_all_checks(cfg, engine, session, dev.id)

    all_passed = True
    for r in results:
        status = "PASS" if r.passed else "FAIL"
        print(f"[{status}] {r.name}: {r.message}")
        if not r.passed and r.fix:
            print(f"       Fix: {r.fix}")
        if not r.passed:
            all_passed = False

    return 0 if all_passed else 1


def _cmd_simulate_run(args):
    """Run the gNMI server against a simulated device (no DB, no real NETCONF)."""
    from splice.api.device import Device

    profile_name = args.simulate
    print(f"Starting simulated device (profile={profile_name})")

    # Load config for gnmi port if available, otherwise use defaults
    gnmi_port = 9339
    gnmi_bind = "0.0.0.0"
    if getattr(args, "config", None):
        cfg = load_config(args.config)
        gnmi_port = cfg.gnmi.port
        gnmi_bind = cfg.gnmi.bind or "0.0.0.0"

    dev = Device("sim://placeholder", simulate=profile_name)
    try:
        print("Connecting to simulated backend...")
        dev.connect()
        print(f"Catalog: {len(dev.catalog_paths)} paths discovered")
        print(f"Starting gNMI server on {gnmi_bind}:{gnmi_port}")
        dev.serve(port=gnmi_port, bind=gnmi_bind, blocking=True)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        dev.close()
    return 0


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="splice")
    parser.add_argument("--config", required=False, help="Path to YAML config")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show third-party protocol logs (ncclient hello/capability dumps)",
    )
    parser.add_argument(
        "--simulate",
        metavar="PROFILE",
        default=None,
        help="Run against a simulated device instead of real hardware (e.g. 'openroadm')",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # -- New commands --
    p_init = sub.add_parser("init", help="Interactive config generator")
    p_init.add_argument("--non-interactive", action="store_true", help="Skip prompts (use flags/env vars)")
    p_init.add_argument("--name", help="Device name")
    p_init.add_argument("--host", help="Device host/IP")
    p_init.add_argument("--username", help="NETCONF username")
    p_init.add_argument("--password", help="NETCONF password (prefer NETCONF_PASSWORD env var)")
    p_init.add_argument("--port", type=int, default=830, help="NETCONF port")
    p_init.add_argument("--gnmi-port", type=int, default=9339, help="gNMI server port")
    p_init.add_argument("--db-url", help="Database URL")

    p_quick = sub.add_parser(
        "quickstart",
        help="Point at a NETCONF device and get a running gNMI server: "
        "generates devices/<name>/config.yaml, runs setup, then run",
    )
    p_quick.add_argument("--host", required=True, help="Device host/IP")
    p_quick.add_argument("--username", required=True, help="NETCONF username")
    p_quick.add_argument(
        "--password-file",
        help="File containing the NETCONF password (default: NETCONF_PASSWORD env var)",
    )
    p_quick.add_argument("--name", help="Device name (default: derived from host)")
    p_quick.add_argument("--port", type=int, default=830, help="NETCONF port")
    p_quick.add_argument("--gnmi-port", type=int, default=9339, help="gNMI server port")
    p_quick.add_argument(
        "--db-url",
        help="Database URL (default: per-device splice_<name> database, created if missing)",
    )
    p_quick.add_argument(
        "--setup-only", action="store_true",
        help="Generate config and run setup, but do not start the server",
    )

    p_migrate = sub.add_parser("migrate", help="Apply pending database migrations")
    p_migrate.add_argument("--dry-run", action="store_true", help="Show pending migrations without applying")

    p_setup = sub.add_parser("setup", help="Run full discovery pipeline")
    p_setup.add_argument("--interval", type=int, default=None, help="Polling interval in seconds")
    p_setup.add_argument("--include-config", action="store_true", help="Include config paths in catalog")
    p_setup.add_argument("--skip-migrate", action="store_true", help="Skip database migration step")
    p_setup.add_argument("--skip-list-instances", action="store_true", help="Skip list instance resolution")
    p_setup.add_argument("--prune", action="store_true",
                         help="Delete catalog paths (and their aliases/jobs) absent from this build")
    p_setup.add_argument("--force", action="store_true",
                         help="Override the prune safety threshold (new < 50%% of old)")

    sub.add_parser("doctor", help="Run pre-flight validation checks")

    # -- Existing commands --
    sub.add_parser("discover")
    sub.add_parser("fetch-yang")

    p_cat = sub.add_parser("build-catalog")
    p_cat.add_argument("--include-config", action="store_true")
    p_cat.add_argument("--prune", action="store_true",
                       help="Delete catalog paths (and their aliases/jobs) absent from this build")
    p_cat.add_argument("--force", action="store_true",
                       help="Override the prune safety threshold (new < 50%% of old)")

    sub.add_parser("build-aliases")

    p_jobs = sub.add_parser("init-jobs")
    p_jobs.add_argument("--interval", type=int, default=None)

    p_set = sub.add_parser(
        "curate-jobs",
        help="Apply metrics curation to existing jobs (no discovery needed)",
    )
    sub.add_parser("set-job")
    p_set.add_argument("--path", required=True)
    p_set.add_argument("--interval", type=int, required=True)
    p_set.add_argument("--alias", action="store_true")

    p_list = sub.add_parser("list-jobs")

    p_list_keyed = sub.add_parser("list-keyed-paths")
    p_list_keyed.add_argument("--all", action="store_true", help="Expand all catalog paths if no jobs set")
    p_list_keyed.add_argument("--path", help="Filter expanded paths by prefix")

    p_list_inst = sub.add_parser("list-instances")
    p_list_inst.add_argument("--list-path", help="Filter by list path")

    p_refresh = sub.add_parser("refresh-list-instances")
    p_refresh.add_argument("--list-path", help="Limit refresh to a specific list path")

    p_debug_list = sub.add_parser("debug-list-instances")
    p_debug_list.add_argument("--limit", type=int, default=0, help="Max list paths to inspect (0 = no limit)")
    p_debug_list.add_argument("--out", required=True, help="Output directory for debug pack")

    p_set_sel = sub.add_parser("set-job-selector")
    p_set_sel.add_argument("--path", required=True, help="Base canonical path to apply selector")
    p_set_sel.add_argument("--list-path", required=True, help="List path containing keyed instances")
    p_set_sel.add_argument("--selector", required=True, help="Selector expression, e.g. name~'1/2/n2/' and type='transceiver'")
    p_set_sel.add_argument("--interval", type=int, required=True)
    p_set_sel.add_argument("--alias", action="store_true")

    p_find = sub.add_parser("find-paths")
    p_find.add_argument("--pattern", required=True)
    p_find.add_argument("--regex", action="store_true")
    p_find.add_argument("--case-sensitive", action="store_true")
    p_find.add_argument("--semantic", action="store_true",
                        help="Use embedding-based semantic search (requires sentence-transformers)")
    p_find.add_argument("--top", type=int, default=10,
                        help="Number of results for semantic search (default: 10)")

    p_export = sub.add_parser("export")
    p_export.add_argument("--what", choices=["catalog", "jobs"], required=True)
    p_export.add_argument("--format", choices=["json", "csv"], required=True)
    p_export.add_argument("--out", required=True)

    sub.add_parser("run")
    p_status = sub.add_parser(
        "status", help="Show health/poller stats from a running bridge (falls back to DB freshness)"
    )
    p_status.add_argument("--json", action="store_true", help="Print raw JSON")
    p_dash = sub.add_parser(
        "dashboards",
        help="Generate Grafana dashboards (live telemetry + YANG inventory) from the device's YANG catalog",
    )
    p_dash.add_argument("--out", default="dashboards", help="Output directory (default: dashboards/)")
    p_dash.add_argument("--bucket", default="gnmi", help="InfluxDB bucket referenced by queries (default: gnmi)")
    p_dash.add_argument("--measurement", default="telemetry",
                        help="InfluxDB measurement referenced by queries (default: telemetry)")

    sub.add_parser("setup-smoke-test", help="Run setup pipeline without serving, validate discovery and expansion")

    p_e2e = sub.add_parser("setup-e2e-check", help="Verify one expanded path is actually polled (uses existing report by default)")
    p_e2e.add_argument("--run-setup", action="store_true", help="Run setup-smoke-test first (can take a long time)")
    p_e2e.add_argument("--report", default=None, help="Path to setup_report.json (default: next to --config, else ./setup_report.json)")

    p_explain = sub.add_parser("explain-setup", help="Print human-readable summary from setup_report.json")
    p_explain.add_argument("--report", default=None, help="Path to setup_report.json (default: next to --config, else ./setup_report.json)")

    p_mcp = sub.add_parser("mcp", help="Start MCP server on stdio transport")
    p_mcp.add_argument("--policy", default=None, help="Path to set policy YAML file")
    p_mcp.add_argument("--audit-log", default=None, dest="audit_log",
                        help="Path to audit log file (JSON-lines, default: splice_audit.jsonl)")

    p_sub = sub.add_parser("subscribe", help="Subscribe to paths and print updates")
    p_sub.add_argument("--path", required=True, help="Path prefix to subscribe to")
    p_sub.add_argument("--interval", type=int, default=10, help="Poll interval in seconds (default: 10)")
    p_sub.add_argument("--count", type=int, default=0, help="Number of updates (0=unlimited, default: 0)")
    p_sub.add_argument("--format", choices=["json", "kv"], default="kv", help="Output format")

    args = parser.parse_args(argv)

    if not args.verbose:
        # ncclient logs full hello/capability XML at INFO; it drowns CLI output.
        for _noisy in ("ncclient", "ncclient.transport", "ncclient.transport.ssh",
                       "ncclient.transport.session", "ncclient.operations.rpc",
                       "paramiko", "paramiko.transport"):
            logging.getLogger(_noisy).setLevel(logging.WARNING)

    # 'init', 'quickstart' and 'mcp' do not require --config
    if args.cmd == "init":
        return _cmd_init(args)

    if args.cmd == "quickstart":
        return _cmd_quickstart(args)

    if args.cmd == "mcp":
        from splice.mcp_server import main as mcp_main
        mcp_main(policy_path=args.policy, audit_log_path=args.audit_log)
        return 0

    # Simulation mode: use the Python API Device with simulated backend
    if getattr(args, "simulate", None) and args.cmd == "run":
        return _cmd_simulate_run(args)

    # Subscribe command: uses Python API Device, requires --config
    if args.cmd == "subscribe":
        if not args.config:
            parser.error("--config is required for the subscribe command")
        from splice.api.device import Device

        cfg = load_config(args.config)
        password = cfg.device.password or ""
        dev = Device(cfg.device.host, cfg.device.port,
                     cfg.device.username, password,
                     yang_dir=cfg.yang_dir)
        dev.connect()

        count = 0
        try:
            with dev.subscribe(args.path, interval=args.interval) as sub:
                for update_batch in sub:
                    for update in update_batch:
                        if args.format == "json":
                            print(json.dumps(update, indent=2))
                        else:
                            ts = update.get("timestamp_ns", "")
                            path = update.get("path", "")
                            value = update.get("value", "")
                            print(f"[{ts}] {path} = {value}")
                    count += 1
                    if args.count > 0 and count >= args.count:
                        break
        except KeyboardInterrupt:
            print(f"\n{count} updates received.")
        finally:
            dev.close()
        return 0

    # All other commands require --config
    if not args.config:
        parser.error("--config is required for this command")

    cfg = load_config(args.config)

    # Route new commands
    if args.cmd == "migrate":
        return _cmd_migrate(args, cfg)

    if args.cmd == "setup":
        return _cmd_setup(args, cfg)

    if args.cmd == "doctor":
        return _cmd_doctor(args, cfg)

    if args.cmd == "dashboards":
        return _cmd_dashboards(args)

    SessionFactory = make_session_factory(cfg.db.url)

    with SessionFactory() as session:
        if args.cmd == "status":
            # Diagnostic command: must not create a device row as a side
            # effect. A missing row just means setup hasn't run.
            dev = find_device(session, host=cfg.device.host, port=cfg.device.port)
            return _cmd_status(args, cfg, session, dev)

        dev = get_or_create_device(
            session,
            host=cfg.device.host,
            port=cfg.device.port,
            username=cfg.device.username,
            password_ref=None,
            name=cfg.device.name,
        )

        if args.cmd == "discover":
            with _make_manager(cfg) as mgr:
                count = run_discovery(cfg, mgr, session)
            print(f"discovered modules: {count}")
            if cfg.discovery.inspect_components:
                print("component inspection:")
                with _make_manager(cfg) as mgr:
                    _inspect_components(
                        mgr,
                        cfg.discovery.component_pattern,
                        cfg.discovery.component_limit,
                    )
            return 0

        if args.cmd == "fetch-yang":
            reconnect = _make_manager_fn(cfg)
            mgr = _make_manager(cfg)
            try:
                count = run_fetch_yang(cfg, mgr, session, reconnect_fn=reconnect)
            finally:
                try:
                    mgr.close_session()
                except Exception:
                    _force_close_session(mgr)
            print(f"fetched YANG files: {count}")
            return 0

        if args.cmd == "build-catalog":
            count, built_paths = run_build_catalog_with_paths(
                cfg, session, include_config=args.include_config
            )
            print(f"catalog entries: {count}")
            if args.prune:
                _maybe_prune_catalog(session, dev.id, built_paths, args.force)
            list_nodes = fetch_list_nodes(session, dev.id)
            if list_nodes:
                examples = [n["canonical_path"] for n in list_nodes[:10]]
                print(f"keyed list nodes: {len(list_nodes)}")
                for p in examples:
                    print(f"  {p}")
                if len(list_nodes) > len(examples):
                    print("  ...")
                print("run refresh-list-instances to resolve keys for list nodes")
                if cfg.discovery.resolve_all_lists:
                    total = _resolve_all_list_instances(
                        cfg,
                        session,
                        dev,
                        list_nodes,
                        cfg.discovery.list_batch_size,
                        cfg.discovery.list_sleep_sec,
                    )
                    print(f"list instances added: {total}")
            if cfg.discovery.pm_discovery:
                pm_filters = _discover_pm_filters(cfg, session, dev)
                stored = replace_pm_filters(session, dev.id, pm_filters) if pm_filters else 0
                print(f"pm filters stored: {stored}")
            return 0

        if args.cmd == "build-aliases":
            paths = fetch_catalog_paths(session, dev.id)
            count = run_build_aliases(session, dev.id, paths)
            print(f"aliases stored: {count}")
            return 0

        if args.cmd == "init-jobs":
            interval = args.interval or cfg.poller.default_interval_sec
            paths = fetch_catalog_paths(session, dev.id)
            summary = _init_jobs_curated(cfg, session, dev.id, paths, interval)
            print(f"jobs: {summary}")
            return 0

        if args.cmd == "curate-jobs":
            if not (cfg.metrics.enabled and (cfg.metrics.include or [])):
                print(
                    "metrics curation is not configured — add a `metrics:` "
                    "section with enabled: true and include patterns."
                )
                return 2
            paths = fetch_catalog_paths(session, dev.id)
            summary = _init_jobs_curated(
                cfg, session, dev.id, paths, cfg.metrics.default_interval_sec
            )
            print(f"curation applied: {summary}")
            return 0

        if args.cmd == "set-job":
            path = args.path
            if args.alias:
                alias_map = fetch_alias_map(session, dev.id)
                path = alias_map.get(path, path)
            count = upsert_jobs(session, dev.id, {path: args.interval})
            print(f"jobs updated: {count}")
            return 0

        if args.cmd == "list-jobs":
            jobs = fetch_job_intervals(session, dev.id)
            for p, interval in sorted(jobs.items()):
                print(f"{p} {interval}")
            return 0

        if args.cmd == "refresh-list-instances":
            list_nodes = fetch_list_nodes(session, dev.id)
            if args.list_path:
                list_nodes = [n for n in list_nodes if n["canonical_path"] == args.list_path]
            if not list_nodes:
                print("no list nodes found")
                return 2
            client = NetconfClient(
                host=cfg.device.host,
                port=cfg.device.port,
                username=cfg.device.username,
                password=cfg.device.password or "",
            )
            pool = _make_pool_from_config(client, cfg)
            resolved = discover_list_instances(
                fetch=lambda fx: pool.get(fx).data_xml,
                list_nodes=list_nodes,
            )
            list_paths = [n["canonical_path"] for n in list_nodes]
            count = replace_list_instances(session, dev.id, resolved, list_paths=list_paths)
            print(f"list instances added: {count}")
            return 0

        if args.cmd == "debug-list-instances":
            os.environ.setdefault("GNMI_LIST_INSTANCE_DEBUG", "1")
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)

            list_nodes = fetch_list_nodes(session, dev.id)
            if not list_nodes:
                print("no list nodes found")
                return 2
            list_nodes = sorted(list_nodes, key=lambda n: n["canonical_path"])
            if args.limit and args.limit > 0:
                list_nodes = list_nodes[: args.limit]

            list_keys_map = {
                n["canonical_path"]: list(n.get("list_keys") or []) for n in list_nodes
            }

            client = NetconfClient(
                host=cfg.device.host,
                port=cfg.device.port,
                username=cfg.device.username,
                password=cfg.device.password or "",
            )
            try:
                mgr = client.manager()
                capabilities = list(getattr(mgr, "server_capabilities", []) or [])
                xpath_supported = any(
                    "urn:ietf:params:netconf:capability:xpath" in cap for cap in capabilities
                )

                report = {
                    "generated_at": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "device": {
                        "id": dev.id,
                        "host": cfg.device.host,
                        "port": cfg.device.port,
                        "username": cfg.device.username,
                        "name": cfg.device.name,
                    },
                    "server_capabilities": capabilities,
                    "xpath_supported": xpath_supported,
                    "list_paths": list_keys_map,
                    "successes": [],
                    "failures": [],
                    "top_failures": [],
                    "fallback_matrix": {},
                    "top_failure_selection": "first 3 failures in sorted list_path order",
                }

                top_failure_paths: list[str] = []
                aborted = False
                abort_reason = None
                total = len(list_nodes)
                for idx, node in enumerate(list_nodes, start=1):
                    path = str(node["canonical_path"])
                    list_keys = list(node.get("list_keys") or [])
                    namespace_map = node.get("namespace_map") or {}
                    if not list_keys:
                        continue
                    print(f"[{idx}/{total}] list instance debug: {path}")

                    filter_xml = build_list_instance_filter_xml(path, namespace_map, list_keys)
                    try:
                        attempt_a = _run_list_instance_attempt(
                            client,
                            lambda m, fx: m.get(fx),
                            filter_xml,
                            path,
                            list_keys,
                            namespace_map,
                        )
                    except NetconfDisconnectedError as exc:
                        aborted = True
                        abort_reason = str(exc)
                        print("NETCONF disconnected; aborting debug run")
                        break
                    if attempt_a["success"]:
                        report["successes"].append(
                            {"list_path": path, "instance_count": attempt_a["instance_count"]}
                        )
                        continue

                    failure_entry = {
                        "list_path": path,
                        "instance_count": attempt_a["instance_count"],
                        "exception": attempt_a["exception"],
                        "netconf_error": attempt_a["error"],
                    }
                    report["failures"].append(failure_entry)

                    if len(top_failure_paths) >= 3:
                        continue

                    top_failure_paths.append(path)
                    report["top_failures"].append(path)
                    list_dir = out_dir / f"list_{len(top_failure_paths):03d}_{_safe_slug(path)}"
                    list_dir.mkdir(parents=True, exist_ok=True)
                    _write_text(list_dir / "list_path.txt", path)

                    attempts = {}

                    def record_attempt(label: str, prefix: str, attempt: dict):
                        req_path = list_dir / f"{prefix}_request.xml"
                        _write_text(req_path, attempt.get("filter_xml"))
                        if attempt.get("response_xml"):
                            resp_path = list_dir / f"{prefix}_response.xml"
                            _write_text(resp_path, attempt.get("response_xml"))
                        else:
                            resp_path = list_dir / f"{prefix}_response_missing.txt"
                            _write_text(resp_path, attempt.get("exception") or "no response xml")
                        attempts[label] = {
                            "success": attempt["success"],
                            "instance_count": attempt["instance_count"],
                            "start_time": attempt["start_time"],
                            "end_time": attempt["end_time"],
                            "duration_sec": attempt["duration_sec"],
                            "request_file": str(req_path.relative_to(out_dir)),
                            "response_file": str(resp_path.relative_to(out_dir)),
                            "exception": attempt["exception"],
                            "netconf_error": attempt["error"],
                        }

                    record_attempt("A", "attempt_A_keys_only", attempt_a)

                    # B: shallow list entry filter (no explicit key leaves)
                    filter_b = build_list_entry_filter_xml(path, namespace_map)
                    try:
                        attempt_b = _run_list_instance_attempt(
                            client,
                            lambda m, fx: m.get(fx),
                            filter_b,
                            path,
                            list_keys,
                            namespace_map,
                        )
                    except NetconfDisconnectedError as exc:
                        aborted = True
                        abort_reason = str(exc)
                        print("NETCONF disconnected; aborting debug run")
                        break
                    record_attempt("B", "attempt_B_shallow_entry", attempt_b)

                    # C: keys-only using get-config (running)
                    try:
                        attempt_c = _run_list_instance_attempt(
                            client,
                            lambda m, fx: m.get_config("running", fx),
                            filter_xml,
                            path,
                            list_keys,
                            namespace_map,
                        )
                    except NetconfDisconnectedError as exc:
                        aborted = True
                        abort_reason = str(exc)
                        print("NETCONF disconnected; aborting debug run")
                        break
                    record_attempt("C", "attempt_C_get_config", attempt_c)

                    # D: xpath filter if supported
                    if xpath_supported:
                        filter_d = build_xpath_filter_xml(path, namespace_map)
                        try:
                            attempt_d = _run_list_instance_attempt(
                                client,
                                lambda m, fx: m.get(fx),
                                filter_d,
                                path,
                                list_keys,
                                namespace_map,
                            )
                        except NetconfDisconnectedError as exc:
                            aborted = True
                            abort_reason = str(exc)
                            print("NETCONF disconnected; aborting debug run")
                            break
                        record_attempt("D", "attempt_D_xpath", attempt_d)
                    else:
                        attempts["D"] = {
                            "skipped": True,
                            "reason": "xpath capability not advertised",
                        }

                    report["fallback_matrix"][path] = attempts
                if aborted:
                    report["aborted"] = True
                    report["abort_reason"] = abort_reason
            finally:
                client.close()

            report_path = out_dir / "report.json"
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, sort_keys=True)
            print(f"debug report written: {report_path}")
            return 0

        if args.cmd == "set-job-selector":
            list_instances = fetch_list_instances(session, dev.id)
            instances = list_instances.get(args.list_path, [])
            if not instances:
                print("no list instances found; run refresh-list-instances")
                return 2
            try:
                clauses, ops = _parse_selector(args.selector)
            except ValueError as e:
                print(str(e))
                return 2

            filtered = [i for i in instances if _match_selector(i, clauses, ops)]
            if not filtered:
                print("no instances matched")
                return 2

            path = args.path
            if args.alias:
                alias_map = fetch_alias_map(session, dev.id)
                path = alias_map.get(path, path)

            list_keys = fetch_list_keys(session, dev.id)
            entry = fetch_catalog_map(session, dev.id).get(strip_predicates(path))
            if not entry:
                print("path not found in catalog")
                return 2
            jobs = {}
            for inst in filtered:
                expanded_path = apply_list_keys(
                    path,
                    {f"{args.list_path}/{k}": str(v) for k, v in inst.items()},
                    list_keys,
                )
                jobs[expanded_path] = args.interval
            count = upsert_jobs(session, dev.id, jobs)
            print(f"jobs updated: {count}")
            return 0

        if args.cmd == "list-keyed-paths":
            catalog = fetch_catalog_map(session, dev.id)
            jobs = fetch_job_intervals(session, dev.id)
            list_keys = fetch_list_keys(session, dev.id)

            client = NetconfClient(
                host=cfg.device.host,
                port=cfg.device.port,
                username=cfg.device.username,
                password=cfg.device.password or "",
            )
            pool = _make_pool_from_config(client, cfg)

            list_instances = fetch_list_instances(session, dev.id)
            if list_keys and not list_instances:
                list_nodes = fetch_list_nodes(session, dev.id)
                list_nodes = _filter_list_nodes_for_jobs(list_nodes, jobs, list_keys)
                if list_nodes:
                    try:
                        resolved = discover_list_instances(
                            fetch=lambda fx: pool.get(fx).data_xml,
                            list_nodes=list_nodes,
                        )
                        if resolved:
                            list_paths = [n["canonical_path"] for n in list_nodes]
                            replace_list_instances(session, dev.id, resolved, list_paths=list_paths)
                            list_instances = resolved
                    except Exception:
                        logging.exception("failed to resolve list instances")

            if not jobs and not args.all:
                print("no jobs set; pass --all to expand full catalog")
                return 2

            expanded = set()
            base_paths = jobs.keys() if jobs else catalog.keys()
            for path in base_paths:
                base_path = strip_predicates(path)
                if base_path not in catalog:
                    continue
                for new_path, _ in expand_path(base_path, list_keys, list_instances):
                    expanded.add(new_path)
            for p in sorted(expanded):
                if args.path and not p.startswith(args.path):
                    continue
                print(p)
            return 0

        if args.cmd == "list-instances":
            instances = fetch_list_instances(session, dev.id)
            for list_path, entries in sorted(instances.items()):
                if args.list_path and args.list_path != list_path:
                    continue
                for entry in entries:
                    print(f"{list_path} {entry}")
            return 0

        if args.cmd == "find-paths":
            if args.semantic:
                from splice.path_index import (
                    PathIndex,
                    build_path_entries,
                    is_search_available,
                )
                if not is_search_available():
                    print("No embedding provider installed; ranking by keyword (BM25) match only.")
                    print("For semantic ranking: pip install -e '.[search]'")
                # Suppress noisy library logs during index build
                import logging as _logging
                import os as _os
                _os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
                _os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
                for _name in ("httpx", "httpcore", "huggingface_hub",
                              "sentence_transformers", "transformers",
                              "safetensors"):
                    _logging.getLogger(_name).setLevel(_logging.ERROR)
                cat_map = fetch_catalog_map(session, dev.id)
                entries = build_path_entries(cat_map)
                # Cache the index on disk to avoid re-embedding on every call
                cache_dir = Path(cfg.yang_dir).parent / ".search_cache"
                cache_path = str(cache_dir / "path_index")
                # Fingerprint: hash of sorted path set to detect content changes
                import hashlib
                current_fingerprint = hashlib.md5(
                    "\n".join(sorted(e.path for e in entries)).encode()
                ).hexdigest()
                idx = None
                if cache_dir.exists():
                    try:
                        idx = PathIndex.load(cache_path)
                        cached_fingerprint = hashlib.md5(
                            "\n".join(sorted(e.path for e in idx.entries)).encode()
                        ).hexdigest()
                        if cached_fingerprint != current_fingerprint:
                            idx = None  # catalog content changed, rebuild
                    except Exception:
                        idx = None
                if idx is None:
                    print(f"Building search index over {len(entries)} paths...")
                    idx = PathIndex(entries)
                    idx.build()
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    idx.save(cache_path)
                    print("Index cached for future searches.")
                results = idx.search(args.pattern, top_k=args.top)
                print()
                for entry, score in results:
                    print(f"  {score:.3f}  {entry.path}")
                return 0

            paths = fetch_catalog_paths(session, dev.id)
            if args.regex:
                flags = 0 if args.case_sensitive else re.IGNORECASE
                try:
                    pattern = re.compile(args.pattern, flags)
                except re.error as e:
                    print(f"invalid regex: {e}")
                    return 2
                matched = [p for p in paths if pattern.search(p)]
            else:
                needle = args.pattern if args.case_sensitive else args.pattern.lower()
                matched = [
                    p
                    for p in paths
                    if (p if args.case_sensitive else p.lower()).find(needle) != -1
                ]
            for p in sorted(matched):
                print(p)
            return 0

        if args.cmd == "export":
            if args.what == "catalog":
                rows = []
                cat = fetch_catalog_map(session, dev.id)
                for p, v in cat.items():
                    rows.append({"canonical_path": p, **v})
                if args.format == "json":
                    export_json(args.out, rows)
                else:
                    export_csv(args.out, rows, ["canonical_path", "keyword", "config", "data_type"])
                return 0
            if args.what == "jobs":
                rows = []
                jobs = fetch_job_intervals(session, dev.id)
                for p, interval in jobs.items():
                    rows.append({"canonical_path": p, "interval_sec": interval})
                if args.format == "json":
                    export_json(args.out, rows)
                else:
                    export_csv(args.out, rows, ["canonical_path", "interval_sec"])
                return 0

        if args.cmd == "run":
            if cfg.poller.batch_mode == "module":
                catalog = fetch_catalog_map_with_module(session, dev.id)
            else:
                catalog = fetch_catalog_map(session, dev.id)
            jobs = fetch_job_intervals(session, dev.id)
            type_map = fetch_type_map(session, dev.id)
            list_keys = fetch_list_keys(session, dev.id)
            list_keys_norm = {normalize_path(k): v for k, v in list_keys.items()}
            expansion_mode = (cfg.gnmi.expansion_mode or "full").lower()

            base_catalog = catalog
            base_jobs = jobs

            client = NetconfClient(
                host=cfg.device.host,
                port=cfg.device.port,
                username=cfg.device.username,
                password=cfg.device.password or "",
            )
            pool = _make_pool_from_config(client, cfg)

            # Setup phase tracking
            setup_start_time = datetime.datetime.now()
            discovery_results = []
            skipped_paths = []
            base_paths_actually_expanded = 0
            discovery_source = "none"

            list_instances = fetch_list_instances(session, dev.id)

            # In FULL mode, always run fresh discovery; in direct/lazy mode use cached only
            should_run_discovery = list_keys and expansion_mode == "full"

            # Log catalog keyed list paths
            keyed_list_count = len([k for k, v in list_keys.items() if v]) if list_keys else 0
            print(f"Setup: catalog keyed list paths: {keyed_list_count}")

            if should_run_discovery:
                list_nodes = fetch_list_nodes(session, dev.id)
                list_nodes = _filter_list_nodes_for_jobs(list_nodes, jobs, list_keys)
                if list_nodes:
                    try:
                        # Initialize debug writer if enabled
                        debug_writer = None
                        if cfg.discovery.discovery_debug:
                            debug_writer = DiscoveryDebugWriter(
                                output_dir=cfg.discovery.debug_output_dir,
                                enabled=True,
                            )
                            print(f"Discovery debug enabled, output: {cfg.discovery.debug_output_dir}")

                        # Check for XPath capability
                        xpath_supported = False
                        try:
                            client.connect()
                            mgr = client.manager()
                            caps = list(getattr(mgr, "server_capabilities", []) or [])
                            xpath_supported = any("xpath" in cap for cap in caps)
                        except Exception:
                            pass

                        print(f"Discovering list instances for {len(list_nodes)} paths...")
                        resolved, discovery_results = discover_list_instances_adaptive(
                            fetch_get=lambda fx: pool.get(fx).data_xml,
                            fetch_get_config=lambda fx: pool.get_config("running", fx).data_xml,
                            list_nodes=list_nodes,
                            xpath_supported=xpath_supported,
                            debug_writer=debug_writer,
                            progress_callback=lambda idx, path: print(f"  [{idx+1}/{len(list_nodes)}] {path}") if idx % 10 == 0 else None,
                        )

                        # Log discovery summary
                        succeeded = sum(1 for r in discovery_results if r.status == "success")
                        empty = sum(1 for r in discovery_results if r.status == "empty")
                        failed = sum(1 for r in discovery_results if r.status == "failed")
                        total_instances = sum(r.instance_count for r in discovery_results)
                        print(f"Setup: list instance discovery: attempted={len(discovery_results)} succeeded={succeeded} empty={empty} failed={failed} total_instances={total_instances}")

                        if resolved:
                            list_paths = [n["canonical_path"] for n in list_nodes]
                            replace_list_instances(session, dev.id, resolved, list_paths=list_paths)
                            list_instances = resolved
                            discovery_source = "fresh"
                    except Exception:
                        logging.exception("failed to resolve list instances")
            elif list_instances:
                # Using cached instances
                discovery_source = "cached"
                cached_count = sum(len(v) for v in list_instances.values())
                print(f"Setup: list instance discovery: using {cached_count} cached instances from {len(list_instances)} list paths")

            def expand_catalog_and_jobs(base_catalog, base_jobs, instances):
                nonlocal skipped_paths, base_paths_actually_expanded
                if not instances or not list_keys:
                    return base_catalog, base_jobs
                expanded_catalog = {}
                expanded_jobs = {}
                total_expanded = 0
                paths_expanded_count = 0
                max_per_base = cfg.gnmi.max_expanded_paths_per_base
                max_total = cfg.gnmi.max_total_expanded_paths

                if base_jobs:
                    for path, interval in base_jobs.items():
                        base_path = strip_predicates(path)
                        entry = base_catalog.get(base_path)
                        if not entry:
                            continue
                        if "[" in path:
                            instance_map = parse_predicates(path)
                            filter_xml = build_filter_xml_for_path(
                                path,
                                entry.get("namespace_map"),
                                list_keys,
                                instance_map,
                            )
                            new_entry = dict(entry)
                            new_entry["filter_xml"] = filter_xml
                            expanded_catalog[path] = new_entry
                            expanded_jobs[path] = interval
                            total_expanded += 1
                            paths_expanded_count += 1
                            continue
                        # Use guarded expansion
                        result = expand_path_guarded(base_path, list_keys, instances, max_per_base)
                        if result.skipped:
                            skipped_paths.append({
                                "path": base_path,
                                "reason": result.skip_reason,
                                "estimated_size": result.estimated_size,
                            })
                            continue
                        if total_expanded + result.actual_size > max_total:
                            skipped_paths.append({
                                "path": base_path,
                                "reason": f"total_limit_exceeded_{total_expanded}+{result.actual_size}>{max_total}",
                                "estimated_size": result.estimated_size,
                            })
                            continue
                        for new_path, instance_map in result.expanded_paths:
                            filter_xml = build_filter_xml_for_path(
                                new_path,
                                entry.get("namespace_map"),
                                list_keys,
                                instance_map,
                            )
                            new_entry = dict(entry)
                            new_entry["filter_xml"] = filter_xml
                            expanded_catalog[new_path] = new_entry
                            expanded_jobs[new_path] = interval
                        total_expanded += result.actual_size
                        paths_expanded_count += 1
                    base_paths_actually_expanded = paths_expanded_count
                    return expanded_catalog, expanded_jobs

                for base_path, entry in base_catalog.items():
                    result = expand_path_guarded(base_path, list_keys, instances, max_per_base)
                    if result.skipped:
                        skipped_paths.append({
                            "path": base_path,
                            "reason": result.skip_reason,
                            "estimated_size": result.estimated_size,
                        })
                        continue
                    if total_expanded + result.actual_size > max_total:
                        skipped_paths.append({
                            "path": base_path,
                            "reason": f"total_limit_exceeded_{total_expanded}+{result.actual_size}>{max_total}",
                            "estimated_size": result.estimated_size,
                        })
                        continue
                    for new_path, instance_map in result.expanded_paths:
                        filter_xml = build_filter_xml_for_path(
                            new_path,
                            entry.get("namespace_map"),
                            list_keys,
                            instance_map,
                        )
                        new_entry = dict(entry)
                        new_entry["filter_xml"] = filter_xml
                        expanded_catalog[new_path] = new_entry
                    total_expanded += result.actual_size
                    paths_expanded_count += 1
                base_paths_actually_expanded = paths_expanded_count
                return expanded_catalog, base_jobs

            def direct_catalog_and_jobs(base_catalog, base_jobs, instances):
                if not base_jobs:
                    return base_catalog, base_jobs
                expanded_catalog = {}
                expanded_jobs = {}
                for path, interval in base_jobs.items():
                    base_path = strip_predicates(path)
                    entry = base_catalog.get(base_path)
                    if not entry:
                        continue
                    if "[" in path:
                        instance_map = parse_predicates(path)
                        filter_xml = build_filter_xml_for_path(
                            path,
                            entry.get("namespace_map"),
                            list_keys,
                            instance_map,
                        )
                        new_entry = dict(entry)
                        new_entry["filter_xml"] = filter_xml
                        expanded_catalog[path] = new_entry
                    else:
                        expanded_catalog[path] = entry
                    expanded_jobs[path] = interval
                return expanded_catalog, expanded_jobs

            def build_tasks(expanded_catalog, expanded_jobs):
                if expanded_jobs:
                    expanded_catalog = {k: v for k, v in expanded_catalog.items() if k in expanded_jobs}
                if cfg.poller.batch_enabled:
                    if cfg.poller.batch_mode == "module":
                        tasks = plan_module_batched_queries(
                            expanded_catalog,
                            expanded_jobs,
                            default_interval_sec=cfg.poller.default_interval_sec,
                        )
                    else:
                        tasks = plan_batched_queries(
                            expanded_catalog,
                            expanded_jobs,
                            default_interval_sec=cfg.poller.default_interval_sec,
                        )
                else:
                    tasks = plan_queries(
                        expanded_catalog,
                        expanded_jobs,
                        default_interval_sec=cfg.poller.default_interval_sec,
                    )
                return expanded_catalog, expanded_jobs, tasks

            expansion_state = {
                "mode": "direct" if expansion_mode in ("direct", "lazy") else "full",
                "full_expanded": False,
            }
            expand_lock = threading.Lock()

            def select_expand_fn():
                return expand_catalog_and_jobs if expansion_state["mode"] == "full" else direct_catalog_and_jobs

            catalog, jobs = select_expand_fn()(base_catalog, base_jobs, list_instances)
            catalog, jobs, tasks = build_tasks(catalog, jobs)

            def fetch(filter_xml: str) -> str:
                reply = pool.get(filter_xml)
                return reply.data_xml

            def parse(xml: str):
                # parse_xml_to_kv attaches per-instance list-key predicates while
                # walking the XML, so multiple instances of the same keyed list
                # stay distinct (the old post-hoc re-keying via a global value map
                # collapsed every instance onto the last-seen key).
                return parse_xml_to_kv(
                    xml,
                    type_map=type_map,
                    preserve_namespaces=cfg.poller.preserve_namespaces,
                    list_keys=list_keys_norm,
                )

            cache_store = MemoryCache(ttl_sec=cfg.cache.ttl_sec)

            kafka_publisher = KafkaPublisher(
                enabled=cfg.kafka.enabled,
                brokers=cfg.kafka.brokers,
                client_id=cfg.kafka.client_id,
                topic_prefix=cfg.kafka.topic_prefix,
                publish_poll_updates=cfg.kafka.publish_poll_updates,
                publish_trigger_events=cfg.kafka.publish_trigger_events,
                acks=cfg.kafka.acks,
                compression=cfg.kafka.compression,
                linger_ms=cfg.kafka.linger_ms,
            )

            def ingest(updates, emit_poll_events: bool = True):
                cache_store.ingest(updates)
                try:
                    with SessionFactory() as s:
                        upsert_last_values(s, dev.id, updates)
                        if cfg.history.enabled and cfg.history.storage == "db":
                            insert_history(s, dev.id, updates)
                except Exception:
                    logging.exception("failed to persist updates to database")
                if cfg.history.enabled and cfg.history.storage == "file":
                    if cfg.history.file_path:
                        try:
                            archive_to_file(cfg.history.file_path, updates)
                        except Exception:
                            logging.exception("failed to archive history to file")
                if emit_poll_events and cfg.kafka.enabled and updates:
                    try:
                        kafka_publisher.publish_poll_updates(
                            cfg.device.name or cfg.device.host,
                            updates,
                        )
                    except Exception:
                        logging.exception("failed to publish polling updates to kafka")

            poller = NetconfPoller(
                fetch=fetch,
                parse=parse,
                ingest=lambda updates: ingest(updates, emit_poll_events=True),
                tasks=tasks,
                task_throttle_sec=cfg.poller.task_throttle_ms / 1000.0,
                task_failure_limit=cfg.poller.task_failure_limit,
            )
            poller.start()

            pm_filters = fetch_pm_filters(session, dev.id)
            if (
                cfg.pm.enabled
                and cfg.discovery.pm_discovery
                and cfg.discovery.pm_refresh_on_run
                and pm_filters
            ):
                pm_filters = _refresh_pm_filters_for_drift(
                    cfg, session, dev, pm_filters,
                    fetch=lambda fx: pool.get(fx).data_xml,
                )
            pm_poller = None
            if cfg.pm.enabled and pm_filters:
                pm_tasks = [
                    QueryTask(
                        canonical_path=row.root_path,
                        filter_xml=row.filter_xml,
                        interval_sec=cfg.pm.interval_sec,
                    )
                    for row in pm_filters
                ]

                def parse_pm(xml: str):
                    return parse_pm_current_data(
                        xml,
                        live_token=cfg.pm.live_token,
                        path_prefix=cfg.pm.path_prefix,
                    )

                pm_poller = NetconfPoller(
                    fetch=fetch,
                    parse=parse_pm,
                    ingest=lambda updates: ingest(updates, emit_poll_events=True),
                    tasks=pm_tasks,
                    task_throttle_sec=cfg.poller.task_throttle_ms / 1000.0,
                    task_failure_limit=cfg.poller.task_failure_limit,
                )
                pm_poller.start()

            def normalize_instances(instances):
                normalized = {}
                for list_path, entries in instances.items():
                    normalized[list_path] = sorted(
                        [tuple(sorted((k, str(v)) for k, v in entry.items())) for entry in entries]
                    )
                return normalized

            task_update_lock = threading.Lock()
            refresh_stop = threading.Event()
            refresh_thr = None
            if cfg.poller.list_refresh_sec > 0 and list_keys:
                list_nodes = fetch_list_nodes(session, dev.id)
                list_nodes = _filter_list_nodes_for_jobs(list_nodes, base_jobs, list_keys)
                if list_nodes:
                    def refresh_loop():
                        nonlocal list_instances, catalog, jobs, tasks
                        list_paths = [n["canonical_path"] for n in list_nodes]
                        try:
                            while not refresh_stop.wait(cfg.poller.list_refresh_sec):
                                try:
                                    resolved = discover_list_instances(
                                        fetch=lambda fx: pool.get(fx).data_xml,
                                        list_nodes=list_nodes,
                                    )
                                    if not resolved:
                                        continue
                                    with task_update_lock:
                                        current_norm = normalize_instances(list_instances)
                                        resolved_norm = normalize_instances(resolved)
                                    if resolved_norm == current_norm:
                                        continue
                                    with SessionFactory() as s:
                                        replace_list_instances(s, dev.id, resolved, list_paths=list_paths)
                                    with task_update_lock:
                                        list_instances = resolved
                                        catalog, jobs = select_expand_fn()(base_catalog, base_jobs, list_instances)
                                        catalog, jobs, tasks = build_tasks(catalog, jobs)
                                    poller.update_tasks(tasks)
                                    logging.info(
                                        "refreshed list instances for %s (paths=%d)",
                                        cfg.device.name or cfg.device.host,
                                        len(list_paths),
                                    )
                                except Exception:
                                    logging.exception("failed to refresh list instances")
                        finally:
                            pass

                    refresh_thr = threading.Thread(
                        target=refresh_loop,
                        name="list-instance-refresh",
                        daemon=True,
                    )
                    refresh_thr.start()

            if cfg.cache.warm_start:
                print(f"Warming cache with {len(tasks)} tasks...")
                for t in tasks:
                    try:
                        xml = fetch(t.filter_xml)
                        ingest(parse(xml))
                    except Exception:
                        continue
                print("Cache warm complete")

            # Generate setup report
            discovery_results_dicts = [
                {
                    "list_path": r.list_path,
                    "status": r.status,
                    "attempt_used": r.attempt_used,
                    "instance_count": r.instance_count,
                    "duration_ms": r.duration_ms,
                    "rpc_error": r.rpc_error,
                    "attempts_tried": r.attempts_tried,
                }
                for r in discovery_results
            ] if discovery_results else []

            # Compute cached instance count for reporting
            cached_instance_count = sum(len(v) for v in list_instances.values()) if list_instances else 0

            # Build discovered_lists_top with sample keys
            discovered_lists_top = []
            if list_instances:
                for list_path, entries in sorted(list_instances.items(), key=lambda x: -len(x[1])):
                    sample_keys = [dict(e) for e in entries[:5]]
                    discovered_lists_top.append({
                        "list_path": list_path,
                        "instance_count": len(entries),
                        "sample_keys": sample_keys,
                    })
                    if len(discovered_lists_top) >= 20:
                        break

            # Build polled_base_paths and polled_expanded_paths_sample
            polled_base_paths = sorted(set(strip_predicates(t.canonical_path) for t in tasks))
            polled_expanded_paths_sample = [t.canonical_path for t in tasks[:50]]

            # Print expansion summary
            print(f"Setup: expansion: base_paths={base_paths_actually_expanded} expanded_paths={len(catalog)} skipped={len(skipped_paths)} (max_per_base={cfg.gnmi.max_expanded_paths_per_base}, max_total={cfg.gnmi.max_total_expanded_paths})")

            # Print poller summary
            print(f"Setup: poller: tasks_scheduled={len(tasks)}")

            setup_report = create_setup_report(
                device_name=cfg.device.name or cfg.device.host,
                device_host=cfg.device.host,
                started_at=setup_start_time,
                discovery_results=discovery_results_dicts,
                skipped_paths=skipped_paths,
                total_expanded_paths=len(catalog),
                polling_tasks=len(tasks),
                base_paths_expanded=base_paths_actually_expanded,
                discovery_source=discovery_source,
                cached_instance_count=cached_instance_count,
                discovered_lists_top=discovered_lists_top,
                polled_base_paths=polled_base_paths,
                polled_expanded_paths_sample=polled_expanded_paths_sample,
            )
            report_path = _setup_report_path(args)
            write_setup_report(setup_report, report_path)
            print(f"Setup report written to {report_path}")

            # Print setup summary
            if skipped_paths:
                print(f"WARNING: {len(skipped_paths)} paths skipped due to expansion limits")
                for sp in skipped_paths[:5]:
                    print(f"  - {sp['path']}: {sp['reason']}")
                if len(skipped_paths) > 5:
                    print(f"  ... and {len(skipped_paths) - 5} more (see setup_report.json)")

            print(f"Setup complete: {len(catalog)} expanded paths, {len(tasks)} polling tasks")

            alias_map = fetch_alias_map(session, dev.id)
            models = fetch_models(session, dev.id)
            supported = [gnmi_pb2.ModelData(**m) for m in models]

            def maybe_expand_full():
                nonlocal catalog, jobs, tasks
                if expansion_mode != "lazy":
                    return
                with expand_lock:
                    if expansion_state["full_expanded"]:
                        return
                    if not list_keys:
                        expansion_state["full_expanded"] = True
                        expansion_state["mode"] = "full"
                        return
                    catalog, jobs = expand_catalog_and_jobs(base_catalog, base_jobs, list_instances)
                    catalog, jobs, tasks = build_tasks(catalog, jobs)
                    poller.update_tasks(tasks)
                    expansion_state["full_expanded"] = True
                    expansion_state["mode"] = "full"
                    logging.info("completed lazy expansion for %s", cfg.device.name or cfg.device.host)

            class LiveCache(CacheView):
                def __init__(self, sess_factory, device_id, alias_map,
                             pm_root_paths=None, pm_path_prefix="/pm"):
                    super().__init__({}, alias_map)
                    self._sf = sess_factory
                    self._device_id = device_id
                    self._pm_root_paths = pm_root_paths or set()
                    self._pm_prefix_norm = normalize_path(pm_path_prefix) if pm_root_paths else ""

                def match_prefix(self, prefix: str):
                    maybe_expand_full()
                    data = {u["path"]: u for u in cache_store.snapshot()}
                    if not data:
                        with self._sf() as s:
                            data = fetch_last_values(s, self._device_id)
                    # reuse CacheView normalization for prefix filtering
                    canonical_prefix = self._alias_map.get(prefix, prefix)
                    pref_norm = self._normalize(canonical_prefix)
                    # Check if query overlaps with any PM root path
                    include_pm = False
                    if self._pm_root_paths and self._pm_prefix_norm:
                        for pm_root in self._pm_root_paths:
                            if pm_root.startswith(pref_norm) or pref_norm.startswith(pm_root):
                                include_pm = True
                                break
                    out = []
                    for p, rec in data.items():
                        p_norm = self._normalize(p)
                        # Boundary-aware prefix match (shared with the API
                        # stack's CacheView) so '/system' does not match
                        # '/system-backup', while '/pm/otnpm-data' still
                        # matches keyed '/pm/otnpm-data[interface=x]/...'.
                        if is_path_prefix(p_norm, pref_norm):
                            out.append(rec)
                        elif include_pm and is_path_prefix(p_norm, self._pm_prefix_norm):
                            out.append(rec)
                    return out

                def all_updates(self):
                    maybe_expand_full()
                    data = {u["path"]: u for u in cache_store.snapshot()}
                    if not data:
                        with self._sf() as s:
                            data = fetch_last_values(s, self._device_id)
                    return list(data.values())

                def get_version(self) -> int:
                    return cache_store.get_version()

                def wait_for_update(self, last_version: int, timeout: float) -> int:
                    return cache_store.wait_for_update(last_version, timeout)

            pm_root_paths_norm = set()
            if cfg.pm.enabled and pm_filters:
                # Derive PM roots from the catalog, not from pm_filters:
                # collapsed filters use the fanout list as root_path, and
                # treating that whole list subtree as PM-related would pull
                # /pm records into every catalog query under it.
                for path in base_catalog.keys():
                    pm_root = _pm_root_from_path(path)
                    if pm_root:
                        pm_root_paths_norm.add(normalize_path(pm_root))

            cache = LiveCache(
                SessionFactory, dev.id, alias_map,
                pm_root_paths=pm_root_paths_norm,
                pm_path_prefix=cfg.pm.path_prefix,
            )

            def _refresh_runtime_tasks_from_db_jobs():
                nonlocal base_jobs, catalog, jobs, tasks
                with SessionFactory() as s:
                    base_jobs = fetch_job_intervals(s, dev.id)
                with task_update_lock:
                    catalog, jobs = select_expand_fn()(base_catalog, base_jobs, list_instances)
                    catalog, jobs, tasks = build_tasks(catalog, jobs)
                poller.update_tasks(tasks)

            trigger_handler = None
            if cfg.trigger.enabled:
                trigger_executor = NetconfTriggerExecutor(
                    pool=pool,
                    catalog_map=base_catalog,
                    alias_map=alias_map,
                    list_keys=list_keys,
                    parse_reply=parse,
                    max_paths_per_request=cfg.trigger.max_paths_per_request,
                )

                def trigger_handler(
                    *,
                    paths,
                    publish_kafka=None,
                    auto_onboard=None,
                    interval_sec=None,
                    source="gnmi",
                ):
                    request_id = str(uuid.uuid4())
                    do_auto_onboard = (
                        cfg.trigger.auto_onboard if auto_onboard is None else bool(auto_onboard)
                    )
                    interval = (
                        cfg.trigger.default_interval_sec
                        if interval_sec is None
                        else int(interval_sec)
                    )
                    if interval <= 0:
                        raise TriggerValidationError("interval_sec must be > 0")

                    result = trigger_executor.execute(paths)
                    if result.updates:
                        ingest(result.updates, emit_poll_events=False)

                    onboarded_paths = []
                    if do_auto_onboard and result.requested_paths:
                        onboard_jobs = {p: interval for p in result.requested_paths}
                        with SessionFactory() as s:
                            upsert_jobs(s, dev.id, onboard_jobs)
                        onboarded_paths = sorted(onboard_jobs.keys())
                        try:
                            _refresh_runtime_tasks_from_db_jobs()
                        except Exception:
                            logging.exception("failed to refresh runtime poller tasks after trigger auto-onboard")

                    do_publish_kafka = (
                        bool(cfg.kafka.enabled and cfg.kafka.publish_trigger_events)
                        if publish_kafka is None
                        else bool(publish_kafka and cfg.kafka.enabled and cfg.kafka.publish_trigger_events)
                    )
                    kafka_published_count = 0
                    if do_publish_kafka and result.updates:
                        kafka_published_count = kafka_publisher.publish_trigger_updates(
                            cfg.device.name or cfg.device.host,
                            request_id,
                            result.updates,
                        )

                    return {
                        "request_id": request_id,
                        "device": cfg.device.name or cfg.device.host,
                        "path_count": len(result.requested_paths),
                        "updates": result.updates,
                        "auto_onboarded_paths": onboarded_paths,
                        "kafka_published_count": kafka_published_count,
                        "errors": result.errors,
                        "source": source,
                    }

            set_handler = None
            if cfg.gnmi.set_enabled:
                refresh_after_set = None
                if cfg.gnmi.set_auto_refresh:
                    def refresh_after_set(mutations):
                        # Best-effort: immediate delete invalidation + targeted read-back.
                        # Poller may still race and overwrite with slightly older data.
                        delete_paths = [
                            normalize_path(m.canonical_path)
                            for m in mutations
                            if m.operation == "delete"
                        ]
                        if delete_paths:
                            removed_cache = cache_store.evict_paths(
                                delete_paths, include_descendants=True
                            )
                            removed_db = 0
                            try:
                                with SessionFactory() as s:
                                    removed_db = delete_last_values(
                                        s,
                                        dev.id,
                                        delete_paths,
                                        include_descendants=True,
                                    )
                            except Exception:
                                logging.exception(
                                    "set auto-refresh delete invalidation failed"
                                )
                            else:
                                logging.info(
                                    "set auto-refresh invalidated deletes cache=%d db=%d",
                                    removed_cache,
                                    removed_db,
                                )

                        seen_paths = set()
                        for m in mutations:
                            if m.operation == "delete":
                                continue
                            if m.canonical_path in seen_paths:
                                continue
                            seen_paths.add(m.canonical_path)
                            try:
                                instance_map = parse_predicates(m.canonical_path)
                                filter_xml = build_filter_xml_for_path(
                                    m.canonical_path,
                                    m.namespace_map,
                                    list_keys,
                                    instance_map,
                                )
                                xml = pool.get_config("running", filter_xml).data_xml
                                updates = parse(xml)
                                if updates:
                                    ingest(updates)
                            except Exception:
                                logging.exception(
                                    "set auto-refresh failed for path=%s",
                                    m.canonical_path,
                                )

                set_executor = NetconfSetExecutor(
                    pool=pool,
                    catalog_map=base_catalog,
                    alias_map=alias_map,
                    list_keys=list_keys,
                    datastore_preference=cfg.gnmi.set_datastore,
                    use_lock=cfg.gnmi.set_use_lock,
                    post_apply_refresh=refresh_after_set,
                )
                # Prime and cache capabilities at startup; reuse after reconnects.
                caps = pool.get_server_capabilities()
                logging.info(
                    "gNMI Set enabled (datastore=%s, auto_refresh=%s, capabilities=%d)",
                    cfg.gnmi.set_datastore,
                    cfg.gnmi.set_auto_refresh,
                    len(caps),
                )
                set_handler = set_executor.execute
            server = serve(
                cache,
                bind=cfg.gnmi.bind,
                port=cfg.gnmi.port,
                heartbeat_sec=cfg.gnmi.heartbeat_sec,
                supported_models=supported,
                max_runtime_sec=cfg.gnmi.max_runtime_sec,
                list_keys=list_keys_norm,
                set_handler=set_handler,
                trigger_handler=trigger_handler,
            )
            print(f"gNMI server ready on {cfg.gnmi.bind}:{cfg.gnmi.port}")
            listener = None
            if cfg.notifications.enabled:
                try:
                    client.connect()
                    flt = None
                    if cfg.notifications.paths:
                        # use first path filter if provided
                        p = cfg.notifications.paths[0]
                        # map alias to canonical if possible
                        p = alias_map.get(p, p)
                        entry = catalog.get(p)
                        if entry and entry.get("filter_xml"):
                            flt = entry.get("filter_xml")
                    try:
                        if flt:
                            client.create_subscription(filter=flt)
                        else:
                            client.create_subscription()
                    except Exception:
                        logging.exception("failed to create NETCONF notification subscription")
                    listener = NotificationListener(client, parse=parse, ingest=ingest, poll_sec=cfg.notifications.poll_sec)
                    listener.start()
                except Exception:
                    logging.exception("failed to start NETCONF notification listener")
                    listener = None
            trigger_api_srv = None
            if cfg.trigger_api.enabled:
                trigger_api_srv = TriggerAPIServer(
                    cfg.trigger_api.bind,
                    cfg.trigger_api.port,
                    cfg.trigger_api.bearer_token or "",
                    trigger_handler,
                )
                trigger_api_srv.start()
                print(f"Trigger API listening on {cfg.trigger_api.bind}:{cfg.trigger_api.port}")
            health_srv = None
            if cfg.health.enabled:
                def health_fn():
                    ok = True
                    detail = {"db": True, "netconf": True, "gnmi": True}
                    try:
                        with SessionFactory() as s:
                            s.execute(text("SELECT 1"))
                    except Exception:
                        detail["db"] = False
                        ok = False
                    # Use passive pool status check (no network calls)
                    pool_status = pool.get_status()
                    detail["netconf_status"] = pool_status
                    detail["netconf"] = not pool_status.get("circuit_breaker_open", False)
                    if not detail["netconf"]:
                        ok = False
                    detail["poller"] = poller.stats()
                    if pm_poller is not None:
                        detail["pm_poller"] = pm_poller.stats()
                    return ok, detail
                health_srv = HealthServer(cfg.health.bind, cfg.health.port, health_fn)
                health_srv.start()
            def shutdown(*_args):
                poller.stop()
                if pm_poller:
                    pm_poller.stop()
                refresh_stop.set()
                if refresh_thr and refresh_thr.is_alive():
                    refresh_thr.join(timeout=2)
                if listener:
                    listener.stop()
                if trigger_api_srv:
                    trigger_api_srv.stop()
                if health_srv:
                    health_srv.stop()
                try:
                    server.stop(0)
                except Exception:
                    pass
                kafka_publisher.close()
                client.close()

            signal.signal(signal.SIGINT, shutdown)
            signal.signal(signal.SIGTERM, shutdown)

            # Release the startup transaction: this session stays open for the
            # whole server lifetime and would otherwise sit idle-in-transaction,
            # blocking vacuum and concurrent migrations. expire_on_commit must
            # be off, or the next `dev.id` access (poller ingest closures, on
            # poller threads) would lazy-refresh on this session and silently
            # open a new never-ending transaction.
            session.expire_on_commit = False
            session.commit()

            print(f"gNMI server listening on {cfg.gnmi.bind}:{cfg.gnmi.port}")
            server.wait_for_termination()
            return 0

        if args.cmd == "setup-smoke-test":
            # Run the same setup as 'run' but without starting the server
            if cfg.poller.batch_mode == "module":
                catalog = fetch_catalog_map_with_module(session, dev.id)
            else:
                catalog = fetch_catalog_map(session, dev.id)
            jobs = fetch_job_intervals(session, dev.id)
            list_keys = fetch_list_keys(session, dev.id)
            expansion_mode = (cfg.gnmi.expansion_mode or "full").lower()

            base_catalog = catalog
            base_jobs = jobs

            client = NetconfClient(
                host=cfg.device.host,
                port=cfg.device.port,
                username=cfg.device.username,
                password=cfg.device.password or "",
            )
            pool = _make_pool_from_config(client, cfg)
            try:

                # Setup phase tracking
                setup_start_time = datetime.datetime.now()
                discovery_results = []
                skipped_paths = []
                base_paths_actually_expanded = 0
                discovery_source = "none"

                list_instances = fetch_list_instances(session, dev.id)

                # In FULL mode, always run fresh discovery; in direct/lazy mode use cached only
                should_run_discovery = list_keys and expansion_mode == "full"

                # Log catalog keyed list paths
                keyed_list_count = len([k for k, v in list_keys.items() if v]) if list_keys else 0
                print(f"Setup: catalog keyed list paths: {keyed_list_count}")

                if should_run_discovery:
                    list_nodes = fetch_list_nodes(session, dev.id)
                    list_nodes = _filter_list_nodes_for_jobs(list_nodes, jobs, list_keys)
                    if list_nodes:
                        try:
                            # Initialize debug writer if enabled
                            debug_writer = None
                            if cfg.discovery.discovery_debug:
                                debug_writer = DiscoveryDebugWriter(
                                    output_dir=cfg.discovery.debug_output_dir,
                                    enabled=True,
                                )
                                print(f"Discovery debug enabled, output: {cfg.discovery.debug_output_dir}")

                            # Check for XPath capability
                            xpath_supported = False
                            try:
                                client.connect()
                                mgr = client.manager()
                                caps = list(getattr(mgr, "server_capabilities", []) or [])
                                xpath_supported = any("xpath" in cap for cap in caps)
                            except Exception:
                                pass

                            print(f"Discovering list instances for {len(list_nodes)} paths...")
                            resolved, discovery_results = discover_list_instances_adaptive(
                                fetch_get=lambda fx: pool.get(fx).data_xml,
                                fetch_get_config=lambda fx: pool.get_config("running", fx).data_xml,
                                list_nodes=list_nodes,
                                xpath_supported=xpath_supported,
                                debug_writer=debug_writer,
                                progress_callback=lambda idx, path: print(f"  [{idx+1}/{len(list_nodes)}] {path}") if idx % 10 == 0 else None,
                            )

                            # Log discovery summary
                            succeeded = sum(1 for r in discovery_results if r.status == "success")
                            empty = sum(1 for r in discovery_results if r.status == "empty")
                            failed = sum(1 for r in discovery_results if r.status == "failed")
                            total_instances = sum(r.instance_count for r in discovery_results)
                            print(f"Setup: list instance discovery: attempted={len(discovery_results)} succeeded={succeeded} empty={empty} failed={failed} total_instances={total_instances}")

                            if resolved:
                                list_paths = [n["canonical_path"] for n in list_nodes]
                                replace_list_instances(session, dev.id, resolved, list_paths=list_paths)
                                list_instances = resolved
                                discovery_source = "fresh"
                        except Exception:
                            logging.exception("failed to resolve list instances")
                elif list_instances:
                    # Using cached instances
                    discovery_source = "cached"
                    cached_count = sum(len(v) for v in list_instances.values())
                    print(f"Setup: list instance discovery: using {cached_count} cached instances from {len(list_instances)} list paths")

                # Expansion function (simplified inline version)
                def smoke_expand_catalog_and_jobs(base_cat, base_jbs, instances):
                    nonlocal skipped_paths, base_paths_actually_expanded
                    if not instances or not list_keys:
                        return base_cat, base_jbs
                    expanded_catalog = {}
                    expanded_jobs = {}
                    total_expanded = 0
                    paths_expanded_count = 0
                    max_per_base = cfg.gnmi.max_expanded_paths_per_base
                    max_total = cfg.gnmi.max_total_expanded_paths

                    if base_jbs:
                        for path, interval in base_jbs.items():
                            base_path = strip_predicates(path)
                            entry = base_cat.get(base_path)
                            if not entry:
                                continue
                            if "[" in path:
                                instance_map = parse_predicates(path)
                                filter_xml = build_filter_xml_for_path(
                                    path,
                                    entry.get("namespace_map"),
                                    list_keys,
                                    instance_map,
                                )
                                new_entry = dict(entry)
                                new_entry["filter_xml"] = filter_xml
                                expanded_catalog[path] = new_entry
                                expanded_jobs[path] = interval
                                total_expanded += 1
                                paths_expanded_count += 1
                                continue
                            result = expand_path_guarded(base_path, list_keys, instances, max_per_base)
                            if result.skipped:
                                skipped_paths.append({
                                    "path": base_path,
                                    "reason": result.skip_reason,
                                    "estimated_size": result.estimated_size,
                                })
                                continue
                            if total_expanded + result.actual_size > max_total:
                                skipped_paths.append({
                                    "path": base_path,
                                    "reason": f"total_limit_exceeded_{total_expanded}+{result.actual_size}>{max_total}",
                                    "estimated_size": result.estimated_size,
                                })
                                continue
                            for new_path, instance_map in result.expanded_paths:
                                filter_xml = build_filter_xml_for_path(
                                    new_path,
                                    entry.get("namespace_map"),
                                    list_keys,
                                    instance_map,
                                )
                                new_entry = dict(entry)
                                new_entry["filter_xml"] = filter_xml
                                expanded_catalog[new_path] = new_entry
                                expanded_jobs[new_path] = interval
                            total_expanded += result.actual_size
                            paths_expanded_count += 1
                        base_paths_actually_expanded = paths_expanded_count
                        return expanded_catalog, expanded_jobs

                    for base_path, entry in base_cat.items():
                        result = expand_path_guarded(base_path, list_keys, instances, max_per_base)
                        if result.skipped:
                            skipped_paths.append({
                                "path": base_path,
                                "reason": result.skip_reason,
                                "estimated_size": result.estimated_size,
                            })
                            continue
                        if total_expanded + result.actual_size > max_total:
                            skipped_paths.append({
                                "path": base_path,
                                "reason": f"total_limit_exceeded_{total_expanded}+{result.actual_size}>{max_total}",
                                "estimated_size": result.estimated_size,
                            })
                            continue
                        for new_path, instance_map in result.expanded_paths:
                            filter_xml = build_filter_xml_for_path(
                                new_path,
                                entry.get("namespace_map"),
                                list_keys,
                                instance_map,
                            )
                            new_entry = dict(entry)
                            new_entry["filter_xml"] = filter_xml
                            expanded_catalog[new_path] = new_entry
                        total_expanded += result.actual_size
                        paths_expanded_count += 1
                    base_paths_actually_expanded = paths_expanded_count
                    return expanded_catalog, base_jbs

                # Run expansion
                if expansion_mode == "full":
                    catalog, jobs = smoke_expand_catalog_and_jobs(base_catalog, base_jobs, list_instances)
                else:
                    catalog, jobs = base_catalog, base_jobs

                # Build tasks
                if jobs:
                    catalog = {k: v for k, v in catalog.items() if k in jobs}
                if cfg.poller.batch_enabled:
                    if cfg.poller.batch_mode == "module":
                        tasks = plan_module_batched_queries(
                            catalog,
                            jobs,
                            default_interval_sec=cfg.poller.default_interval_sec,
                        )
                    else:
                        tasks = plan_batched_queries(
                            catalog,
                            jobs,
                            default_interval_sec=cfg.poller.default_interval_sec,
                        )
                else:
                    tasks = plan_queries(
                        catalog,
                        jobs,
                        default_interval_sec=cfg.poller.default_interval_sec,
                    )

                # Print expansion summary
                print(f"Setup: expansion: base_paths={base_paths_actually_expanded} expanded_paths={len(catalog)} skipped={len(skipped_paths)} (max_per_base={cfg.gnmi.max_expanded_paths_per_base}, max_total={cfg.gnmi.max_total_expanded_paths})")

                # Print poller summary
                print(f"Setup: poller: tasks_scheduled={len(tasks)}")

                # Generate setup report
                discovery_results_dicts = [
                    {
                        "list_path": r.list_path,
                        "status": r.status,
                        "attempt_used": r.attempt_used,
                        "instance_count": r.instance_count,
                        "duration_ms": r.duration_ms,
                        "rpc_error": r.rpc_error,
                        "attempts_tried": r.attempts_tried,
                    }
                    for r in discovery_results
                ] if discovery_results else []

                cached_instance_count = sum(len(v) for v in list_instances.values()) if list_instances else 0

                # Build discovered_lists_top with sample keys
                discovered_lists_top = []
                if list_instances:
                    for list_path, entries in sorted(list_instances.items(), key=lambda x: -len(x[1])):
                        sample_keys = [dict(e) for e in entries[:5]]
                        discovered_lists_top.append({
                            "list_path": list_path,
                            "instance_count": len(entries),
                            "sample_keys": sample_keys,
                        })
                        if len(discovered_lists_top) >= 20:
                            break

                # Build polled_base_paths and polled_expanded_paths_sample
                polled_base_paths = sorted(set(strip_predicates(t.canonical_path) for t in tasks))
                polled_expanded_paths_sample = [t.canonical_path for t in tasks[:50]]

                setup_report = create_setup_report(
                    device_name=cfg.device.name or cfg.device.host,
                    device_host=cfg.device.host,
                    started_at=setup_start_time,
                    discovery_results=discovery_results_dicts,
                    skipped_paths=skipped_paths,
                    total_expanded_paths=len(catalog),
                    polling_tasks=len(tasks),
                    base_paths_expanded=base_paths_actually_expanded,
                    discovery_source=discovery_source,
                    cached_instance_count=cached_instance_count,
                    discovered_lists_top=discovered_lists_top,
                    polled_base_paths=polled_base_paths,
                    polled_expanded_paths_sample=polled_expanded_paths_sample,
                )
                write_setup_report(setup_report, _setup_report_path(args))
                print(f"Setup report written to {_setup_report_path(args)}")

                # Print final summary
                print("")
                print("=" * 60)
                print("SETUP SMOKE TEST RESULTS")
                print("=" * 60)
                list_paths_attempted = len(discovery_results)
                polling_tasks_count = len(tasks)
                print(f"  list_paths_attempted: {list_paths_attempted}")
                print(f"  polling_tasks: {polling_tasks_count}")
                print(f"  discovery_source: {discovery_source}")
                print(f"  expansion_mode: {expansion_mode}")

                # Determine success/failure
                errors = []
                if expansion_mode == "full" and list_paths_attempted == 0 and discovery_source != "cached":
                    errors.append("list_paths_attempted=0 (discovery did not run)")
                if polling_tasks_count == 0:
                    errors.append("polling_tasks=0 (no tasks to poll)")

            finally:
                client.close()
            if errors:
                print("")
                print("FAILED:")
                for err in errors:
                    print(f"  - {err}")
                print("=" * 60)
                return 1
            else:
                print("")
                print("PASSED: Setup smoke test completed successfully")
                print("=" * 60)
                return 0

        if args.cmd == "explain-setup":
            import json as json_module
            report_path = _resolve_report_for_read(args)
            try:
                with open(report_path, "r") as f:
                    report = json_module.load(f)
            except FileNotFoundError:
                print(f"ERROR: Report file not found: {report_path}")
                print("Run 'setup-smoke-test' or 'run' first to generate the report.")
                return 1
            except json_module.JSONDecodeError as e:
                print(f"ERROR: Invalid JSON in report file: {e}")
                return 1

            print("")
            print("=" * 70)
            print(f"SETUP REPORT: {report.get('device_name', 'unknown')} ({report.get('device_host', 'unknown')})")
            print("=" * 70)
            print(f"Duration: {report.get('duration_sec', 0):.1f}s")
            print(f"Discovery source: {report.get('discovery_source', 'unknown')}")
            print("")

            # 1. Successfully discovered lists
            print("-" * 70)
            print("DISCOVERED LISTS (successful)")
            print("-" * 70)
            discovered_lists = report.get("discovered_lists_top", [])
            if discovered_lists:
                for item in discovered_lists:
                    list_path = item.get("list_path", "?")
                    instance_count = item.get("instance_count", 0)
                    sample_keys = item.get("sample_keys", [])
                    print(f"  {list_path}")
                    print(f"    instances: {instance_count}")
                    if sample_keys:
                        print(f"    sample keys:")
                        for sk in sample_keys[:5]:
                            print(f"      {sk}")
                    print("")
            else:
                # Fallback to discovery_results
                discovery_results = report.get("discovery_results", [])
                succeeded = [r for r in discovery_results if r.get("status") == "success" and r.get("instance_count", 0) > 0]
                if succeeded:
                    for r in succeeded[:20]:
                        print(f"  {r.get('list_path', '?')}: {r.get('instance_count', 0)} instances")
                else:
                    print("  (none - discovery may have used cached instances)")
            print("")

            # 2. Failed discoveries
            print("-" * 70)
            print("FAILED DISCOVERIES")
            print("-" * 70)
            discovery_results = report.get("discovery_results", [])
            failed = [r for r in discovery_results if r.get("status") == "failed"]
            if failed:
                for r in failed[:10]:
                    list_path = r.get("list_path", "?")
                    rpc_error = r.get("rpc_error", "")
                    print(f"  {list_path}")
                    if rpc_error:
                        # Truncate long error messages
                        error_short = rpc_error[:200] + "..." if len(rpc_error) > 200 else rpc_error
                        print(f"    error: {error_short}")
                    print("")
                if len(failed) > 10:
                    print(f"  ... and {len(failed) - 10} more failures")
            else:
                print("  (none)")
            print("")

            # 3. Skipped paths (due to guardrails)
            print("-" * 70)
            print("SKIPPED PATHS (expansion guardrails)")
            print("-" * 70)
            skipped_paths = report.get("skipped_paths", [])
            if skipped_paths:
                for sp in skipped_paths[:20]:
                    path = sp.get("path", "?")
                    reason = sp.get("reason", "?")
                    print(f"  {path}")
                    print(f"    reason: {reason}")
                if len(skipped_paths) > 20:
                    print(f"  ... and {len(skipped_paths) - 20} more")
            else:
                print("  (none)")
            print("")

            # 4. Polled paths
            print("-" * 70)
            print("POLLED PATHS (scheduled for polling)")
            print("-" * 70)
            polled_base_paths = report.get("polled_base_paths", [])
            polled_sample = report.get("polled_expanded_paths_sample", [])
            print(f"  Base paths: {len(polled_base_paths)}")
            print(f"  Expanded paths (total): {report.get('total_expanded_paths', 0)}")
            print(f"  Polling tasks: {report.get('polling_tasks', 0)}")
            print("")
            if polled_sample:
                print("  Sample expanded paths:")
                for p in polled_sample[:20]:
                    print(f"    {p}")
                if len(polled_sample) > 20:
                    print(f"    ... and {len(polled_sample) - 20} more in sample")
            print("")

            # Summary
            print("=" * 70)
            print("SUMMARY")
            print("=" * 70)
            print(f"  list_paths_attempted: {report.get('list_paths_attempted', 0)}")
            print(f"  list_paths_succeeded: {report.get('list_paths_succeeded', 0)}")
            print(f"  list_paths_empty: {report.get('list_paths_empty', 0)}")
            print(f"  list_paths_failed: {report.get('list_paths_failed', 0)}")
            print(f"  total_instances_discovered: {report.get('total_instances_discovered', 0)}")
            print(f"  base_paths_expanded: {report.get('base_paths_expanded', 0)}")
            print(f"  base_paths_skipped: {report.get('base_paths_skipped', 0)}")
            print(f"  total_expanded_paths: {report.get('total_expanded_paths', 0)}")
            print(f"  polling_tasks: {report.get('polling_tasks', 0)}")
            print("=" * 70)
            return 0

        if args.cmd == "setup-e2e-check":
            import subprocess
            import json as json_module

            print("=" * 70)
            print("SETUP END-TO-END CHECK")
            print("=" * 70)
            print("")

            report_path = _resolve_report_for_read(args)

            # Step 1: Optionally run setup-smoke-test
            if args.run_setup:
                print("Step 1: Running setup-smoke-test (this may take a while)...")
                # Stream output so user sees progress
                proc = subprocess.Popen(
                    [sys.executable, __file__, "--config", args.config, "setup-smoke-test"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                for line in proc.stdout:
                    print(f"  {line}", end="")
                proc.wait()
                if proc.returncode != 0:
                    print("FAILED: setup-smoke-test failed")
                    return 1
                print("  setup-smoke-test passed")
                print("")
            else:
                print("Step 1: Skipping setup-smoke-test (use --run-setup to run it)")
                print(f"  Using existing report: {report_path}")
                print("")

            # Step 2: Load the report
            print("Step 2: Loading report...")
            try:
                with open(report_path, "r") as f:
                    report = json_module.load(f)
            except FileNotFoundError:
                print(f"FAILED: Report file not found: {report_path}")
                print("Run 'setup-smoke-test' first or use --run-setup flag")
                return 1
            except Exception as e:
                print(f"FAILED: Could not load report: {e}")
                return 1
            print(f"  report loaded ({report.get('device_name', '?')})")
            print("")

            # Step 3: Find a discovered list with >0 instances (prefer interface-like)
            print("Step 3: Selecting a discovered list...")
            discovered_lists = report.get("discovered_lists_top", [])

            # Fallback: if discovered_lists_top is empty, try to build from discovery_results
            if not discovered_lists:
                print("  (discovered_lists_top empty, trying discovery_results...)")
                discovery_results = report.get("discovery_results", [])
                for r in discovery_results:
                    if r.get("status") == "success" and r.get("instance_count", 0) > 0:
                        discovered_lists.append({
                            "list_path": r.get("list_path"),
                            "instance_count": r.get("instance_count", 0),
                            "sample_keys": [],  # No sample keys in old reports
                        })

            # Fallback: if still empty, try to fetch from database
            if not discovered_lists:
                print("  (discovery_results empty, fetching from database...)")
                list_instances = fetch_list_instances(session, dev.id)
                if list_instances:
                    for list_path, entries in sorted(list_instances.items(), key=lambda x: -len(x[1])):
                        sample_keys = [dict(e) for e in entries[:5]]
                        discovered_lists.append({
                            "list_path": list_path,
                            "instance_count": len(entries),
                            "sample_keys": sample_keys,
                        })
                        if len(discovered_lists) >= 20:
                            break

            if not discovered_lists:
                print("FAILED: No discovered lists in report or database")
                return 1

            # Prefer lists with "interface" in the name
            selected = None
            for item in discovered_lists:
                if item.get("instance_count", 0) > 0:
                    if "interface" in item.get("list_path", "").lower():
                        selected = item
                        break
            if not selected:
                # Fall back to any list with instances
                for item in discovered_lists:
                    if item.get("instance_count", 0) > 0:
                        selected = item
                        break
            if not selected:
                print("FAILED: No discovered list has >0 instances")
                return 1

            list_path = selected["list_path"]
            sample_keys = selected.get("sample_keys", [])

            # If no sample_keys in report, fetch from database
            if not sample_keys:
                print(f"  (no sample keys in report, fetching from database...)")
                list_instances = fetch_list_instances(session, dev.id)
                if list_path in list_instances:
                    sample_keys = [dict(e) for e in list_instances[list_path][:5]]

            if not sample_keys:
                print(f"FAILED: Selected list {list_path} has no sample keys (not in database either)")
                return 1

            first_key = sample_keys[0]
            print(f"  selected list: {list_path}")
            print(f"  instance count: {selected.get('instance_count', '?')}")
            print(f"  first instance keys: {first_key}")
            print("")

            # Step 4: Construct expanded path
            print("Step 4: Constructing expanded path...")
            # Build predicate string from keys
            predicates = "".join(f"[{k}={v}]" for k, v in first_key.items())
            expanded_path = f"{list_path}{predicates}"
            print(f"  expanded path: {expanded_path}")
            print("")

            # Step 5: Verify path is in polling tasks
            print("Step 5: Verifying path is in polling tasks...")
            polled_sample = report.get("polled_expanded_paths_sample", [])
            polled_base_paths = report.get("polled_base_paths", [])

            # Check if the expanded path or a path starting with it is in the sample
            found = False
            matching_path = None
            for p in polled_sample:
                if p.startswith(list_path) and all(f"[{k}={v}]" in p or f"[{k}='{v}']" in p for k, v in first_key.items()):
                    found = True
                    matching_path = p
                    break

            if not found:
                # Check if base path at least is polled
                if list_path in polled_base_paths or strip_predicates(list_path) in polled_base_paths:
                    print(f"  NOTE: Base path {list_path} is in polled paths, but expanded path not in sample")
                    print(f"  (This may be OK if there are many expanded paths)")
                    found = True
                    matching_path = list_path

            if found:
                print(f"  FOUND: {matching_path or expanded_path}")
            else:
                print(f"  WARNING: Exact expanded path not found in polling sample")
                print(f"  (This may be OK if guardrails limited expansion)")

            print("")

            # Step 6: Print gnmic command
            print("Step 6: gNMI validation command")
            print("-" * 70)
            gnmi_port = report.get("device_host", "localhost")
            # Try to extract port from config
            gnmi_port_num = 9339
            try:
                gnmi_cfg = cfg.gnmi
                gnmi_port_num = gnmi_cfg.port
            except Exception:
                pass

            # Convert path to gNMI format (strip namespace prefixes for alias)
            gnmi_path = expanded_path
            # Try to simplify: remove p1:, p2:, etc. prefixes
            gnmi_path_simple = re.sub(r'/p\d+:', '/', gnmi_path)
            gnmi_path_simple = gnmi_path_simple.lstrip('/')

            print(f"Run this command to validate gNMI output:")
            print("")
            print(f"  gnmic -a {report.get('device_host', 'localhost')}:{gnmi_port_num} \\")
            print(f"    --insecure \\")
            print(f"    get --path '/{gnmi_path_simple}'")
            print("")
            print("Or with full canonical path:")
            print("")
            print(f"  gnmic -a {report.get('device_host', 'localhost')}:{gnmi_port_num} \\")
            print(f"    --insecure \\")
            print(f"    get --path '{expanded_path}'")
            print("")

            print("=" * 70)
            print("E2E CHECK PASSED")
            print("=" * 70)
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
