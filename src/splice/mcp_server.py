# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MCP (Model Context Protocol) server for Splice.

Exposes the bridge's Python API as MCP tools over stdio transport,
allowing AI agents to connect to NETCONF devices, read telemetry,
push configuration, and search paths.

Usage::

    splice mcp
    splice mcp --policy policy.yaml

Or programmatically::

    from splice.mcp_server import create_mcp_server, run_stdio
    server = create_mcp_server()
    run_stdio(server)
"""

from __future__ import annotations

import asyncio
import datetime
import fnmatch
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Set policy
# ---------------------------------------------------------------------------

@dataclass
class SetPolicy:
    """Safety gate for set_config operations.

    Default: deny all writes unless paths are explicitly allowed.
    """

    allowed_paths: List[str] = field(default_factory=list)
    forbidden_paths: List[str] = field(default_factory=list)
    value_constraints: Dict[str, Dict] = field(default_factory=dict)

    def check(self, path: str, value: Any = None) -> Optional[str]:
        """Return None if allowed, or an error message if denied.

        ``path`` may be a single string or several equivalent forms the write
        could resolve to (raw, predicate-stripped, alias-resolved). Evaluating
        every form fail-closed prevents a policy bypass where the check sees
        one canonicalization but the device write applies another:
          - forbidden if ANY form matches a forbidden pattern
          - allowed only if EVERY form is in the allowed list
        """
        candidates = [path] if isinstance(path, str) else [p for p in path if p]

        # Check forbidden first (overrides allows)
        for cand in candidates:
            for pattern in self.forbidden_paths:
                if fnmatch.fnmatch(cand, pattern):
                    return f"path matches forbidden pattern: {pattern}"

        # Check allowed
        if not self.allowed_paths:
            return "no paths are allowed (default deny-all policy)"

        for cand in candidates:
            if not any(fnmatch.fnmatch(cand, pat) for pat in self.allowed_paths):
                return f"path not in allowed list: {cand}"

        # Check value constraints (supports glob patterns in keys). A
        # constraint applies if it matches ANY candidate form.
        if value is not None:
            for constraint_pattern, constraint in self.value_constraints.items():
                if not any(fnmatch.fnmatch(c, constraint_pattern) for c in candidates):
                    continue
                try:
                    numeric = float(value)
                    if "min" in constraint and numeric < constraint["min"]:
                        return (
                            f"value {value} below minimum {constraint['min']} "
                            f"for {constraint_pattern}"
                        )
                    if "max" in constraint and numeric > constraint["max"]:
                        return (
                            f"value {value} above maximum {constraint['max']} "
                            f"for {constraint_pattern}"
                        )
                except (TypeError, ValueError):
                    pass  # non-numeric value, skip numeric constraints
                break  # first matching constraint wins

        return None

    @classmethod
    def from_dict(cls, data: Dict) -> SetPolicy:
        return cls(
            allowed_paths=data.get("allowed_paths", []),
            forbidden_paths=data.get("forbidden_paths", []),
            value_constraints=data.get("value_constraints", {}),
        )

    @classmethod
    def from_yaml(cls, path: str) -> SetPolicy:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls.from_dict(data.get("set_policy", data))


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

_ERROR_MAP = {
    "AUTH_LOCKOUT": {
        "retryable": True,
        "retry_after_sec": 300.0,
        "remediation_hint": (
            "Device locked out due to authentication failures. "
            "Wait 5 minutes before retrying."
        ),
    },
    "SESSION_LIMIT": {
        "retryable": True,
        "retry_after_sec": 60.0,
        "remediation_hint": (
            "Device has reached its maximum concurrent session limit. "
            "Close unused sessions or wait."
        ),
    },
    "DEVICE_UNREACHABLE": {
        "retryable": True,
        "retry_after_sec": 10.0,
        "remediation_hint": "Check network connectivity to the device.",
    },
    "CIRCUIT_BREAKER_OPEN": {
        "retryable": True,
        "retry_after_sec": 60.0,
        "remediation_hint": (
            "Circuit breaker is open after repeated failures. "
            "Wait for cooldown period."
        ),
    },
    "PATH_NOT_FOUND": {
        "retryable": False,
        "retry_after_sec": None,
        "remediation_hint": (
            "The requested path is not in the device catalog. "
            "Use find_paths to search for valid paths."
        ),
    },
    "SET_REJECTED": {
        "retryable": False,
        "retry_after_sec": None,
        "remediation_hint": (
            "The device rejected the configuration change. "
            "Check the path is writable and the value is valid."
        ),
    },
    "SET_POLICY_DENIED": {
        "retryable": False,
        "retry_after_sec": None,
        "remediation_hint": (
            "Set operation denied by policy. Check allowed_paths "
            "in the MCP server's set policy configuration."
        ),
    },
    "DISCOVERY_TIMEOUT": {
        "retryable": True,
        "retry_after_sec": 30.0,
        "remediation_hint": (
            "Discovery took too long. The device may be slow to "
            "respond. Try connecting with skip_discover=True."
        ),
    },
    "DEVICE_NOT_CONNECTED": {
        "retryable": False,
        "retry_after_sec": None,
        "remediation_hint": (
            "No active session for this device_id. "
            "Use connect_device first."
        ),
    },
    "RATE_LIMITED": {
        "retryable": True,
        "retry_after_sec": 60.0,
        "remediation_hint": (
            "Too many RPCs to this device in the current window. "
            "Wait before retrying."
        ),
    },
    "SEARCH_UNAVAILABLE": {
        "retryable": False,
        "retry_after_sec": None,
        "remediation_hint": (
            "Semantic search requires sentence-transformers. "
            "Install with: pip install splice[search]"
        ),
    },
}


def _make_error(
    error_code: str,
    message: str,
    retry_after_override: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a structured error response dict."""
    info = _ERROR_MAP.get(error_code, {
        "retryable": False,
        "retry_after_sec": None,
        "remediation_hint": "",
    })
    return {
        "status": "error",
        "error": True,
        "error_code": error_code,
        "message": message,
        "retryable": info["retryable"],
        "retry_after_sec": retry_after_override or info["retry_after_sec"],
        "remediation_hint": info["remediation_hint"],
    }


def _classify_exception(exc: Exception) -> str:
    """Map an exception to an error code string."""
    from splice.api._errors import (
        ConnectionError as BridgeConnectionError,
        DiscoveryError,
        PathNotFoundError,
        PathNotWritableError,
        SetError,
        SubscriptionError,
    )
    from splice.netconf.client import CircuitBreakerOpen

    if isinstance(exc, CircuitBreakerOpen):
        return "CIRCUIT_BREAKER_OPEN"

    # Defense-in-depth: check chained cause for CircuitBreakerOpen
    if exc.__cause__ is not None and isinstance(exc.__cause__, CircuitBreakerOpen):
        return "CIRCUIT_BREAKER_OPEN"

    msg = str(exc).lower()

    if isinstance(exc, PathNotFoundError):
        return "PATH_NOT_FOUND"
    if isinstance(exc, PathNotWritableError):
        return "SET_REJECTED"
    if isinstance(exc, SetError):
        return "SET_REJECTED"
    if isinstance(exc, SubscriptionError):
        return "DEVICE_UNREACHABLE"
    if isinstance(exc, DiscoveryError):
        if "timeout" in msg:
            return "DISCOVERY_TIMEOUT"
        return "DEVICE_UNREACHABLE"
    if isinstance(exc, BridgeConnectionError):
        if "auth" in msg or "locked" in msg:
            return "AUTH_LOCKOUT"
        if "circuit" in msg:
            return "CIRCUIT_BREAKER_OPEN"
        if "session" in msg and "limit" in msg:
            return "SESSION_LIMIT"
        return "DEVICE_UNREACHABLE"

    return "DEVICE_UNREACHABLE"


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple per-device RPC counter with a sliding window."""

    def __init__(self, max_per_minute: int = 30):
        self.max_per_minute = max_per_minute
        self._windows: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    def check(self, device_id: str) -> Optional[float]:
        """Return None if allowed, or seconds to wait if rate-limited."""
        with self._lock:
            now = time.monotonic()
            window_start = now - 60.0

            stamps = self._windows.get(device_id, [])
            stamps = [t for t in stamps if t > window_start]
            self._windows[device_id] = stamps

            if len(stamps) >= self.max_per_minute:
                oldest = stamps[0]
                wait = 60.0 - (now - oldest)
                return max(0.1, wait)

            stamps.append(now)
            return None

    def clear(self, device_id: str) -> None:
        with self._lock:
            self._windows.pop(device_id, None)


# ---------------------------------------------------------------------------
# Session pool
# ---------------------------------------------------------------------------

@dataclass
class _DeviceSession:
    device_id: str
    device: Any  # splice.api.Device
    host: str
    port: int
    last_used: float = field(default_factory=time.monotonic)
    created_at: float = field(default_factory=time.monotonic)


class SessionPool:
    """Manages active Device sessions with idle timeout."""

    def __init__(self, idle_timeout_min: float = 30.0):
        self._sessions: Dict[str, _DeviceSession] = {}
        self.idle_timeout_sec = idle_timeout_min * 60.0
        self._lock = threading.Lock()

    def add(self, session: _DeviceSession) -> None:
        with self._lock:
            self._sessions[session.device_id] = session

    def add_if_absent(self, session: _DeviceSession) -> bool:
        """Atomically add a session only if device_id is not already present.

        Returns True if the session was added, False if one already existed.
        """
        with self._lock:
            if session.device_id in self._sessions:
                return False
            self._sessions[session.device_id] = session
            return True

    def get(self, device_id: str) -> Optional[_DeviceSession]:
        with self._lock:
            s = self._sessions.get(device_id)
            if s is not None:
                s.last_used = time.monotonic()
            return s

    def remove(self, device_id: str) -> Optional[_DeviceSession]:
        with self._lock:
            return self._sessions.pop(device_id, None)

    def all_sessions(self) -> List[_DeviceSession]:
        with self._lock:
            return list(self._sessions.values())

    def cleanup_idle(self) -> List[str]:
        """Remove sessions idle beyond the timeout. Returns removed IDs."""
        with self._lock:
            now = time.monotonic()
            to_remove = []
            for did, s in self._sessions.items():
                if (now - s.last_used) > self.idle_timeout_sec:
                    to_remove.append(did)
            for did in to_remove:
                s = self._sessions.pop(did, None)
                if s is not None:
                    try:
                        s.device.close()
                    except Exception:
                        pass
        return to_remove


# ---------------------------------------------------------------------------
# Audit logger
# ---------------------------------------------------------------------------

_SENSITIVE_KEYS = frozenset({"password", "passwd", "secret", "token", "credential"})


class AuditLogger:
    """Records every MCP tool call as JSON-lines to a file and Python logging."""

    def __init__(self, path: str = "splice_audit.jsonl"):
        self.path = path
        self._log = logging.getLogger(__name__ + ".audit")
        self._lock = threading.Lock()

    @staticmethod
    def _sanitize_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy of *params* with sensitive values redacted."""
        sanitized = {}
        for key, value in params.items():
            if key.lower() in _SENSITIVE_KEYS:
                sanitized[key] = "***REDACTED***"
            else:
                sanitized[key] = value
        return sanitized

    def log_call(
        self,
        tool_name: str,
        params: Dict[str, Any],
        result: Dict[str, Any],
        duration_ms: float,
    ) -> Dict[str, Any]:
        """Write one audit entry. Returns the entry dict for testing."""
        safe_params = self._sanitize_params(params)

        status = "error" if result.get("error") else "success"
        error_code = result.get("error_code") if status == "error" else None
        device_id = (
            params.get("device_id")
            or result.get("device_id")
        )

        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tool": tool_name,
            "device_id": device_id,
            "params": safe_params,
            "status": status,
            "duration_ms": round(duration_ms, 2),
        }
        if error_code is not None:
            entry["error_code"] = error_code

        line = json.dumps(entry, default=str)
        self._log.info("%s", line)

        try:
            with self._lock:
                with open(self.path, "a") as f:
                    f.write(line + "\n")
        except OSError as exc:
            self._log.warning("failed to write audit log to %s: %s", self.path, exc)

        return entry


# ---------------------------------------------------------------------------
# MCP server factory
# ---------------------------------------------------------------------------

def create_mcp_server(
    *,
    set_policy: Optional[SetPolicy] = None,
    max_rpcs_per_minute: int = 30,
    idle_timeout_min: float = 30.0,
    audit_log_path: Optional[str] = None,
) -> Any:
    """Create and return a FastMCP server instance with all tools registered.

    Returns the FastMCP server object (call ``run_stdio(server)`` to start).
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP(
        "splice",
        instructions=(
            "Splice MCP server. Provides tools to connect to "
            "NETCONF devices, read telemetry, push configuration, and search "
            "YANG catalog paths. Use connect_device first, then other tools "
            "with the returned device_id."
        ),
    )

    policy = set_policy or SetPolicy()
    pool = SessionPool(idle_timeout_min=idle_timeout_min)
    limiter = RateLimiter(max_per_minute=max_rpcs_per_minute)
    audit = AuditLogger(path=audit_log_path or "splice_audit.jsonl")
    _search_indices: Dict[str, Any] = {}  # device_id -> PathIndex cache

    # -- Helpers --

    def _get_session(device_id: str) -> _DeviceSession:
        # Piggyback idle session cleanup on every tool invocation
        removed = pool.cleanup_idle()
        if removed:
            log.info("cleaned up %d idle session(s): %s", len(removed), removed)
        s = pool.get(device_id)
        if s is None:
            raise _ToolError("DEVICE_NOT_CONNECTED", f"no session for device_id={device_id}")
        return s

    def _check_rate(device_id: str) -> None:
        wait = limiter.check(device_id)
        if wait is not None:
            raise _ToolError("RATE_LIMITED", "rate limit exceeded", retry_after=wait)

    # -- Tools --

    @mcp.tool(
        name="connect_device",
        description=(
            "Connect to a NETCONF device. Returns a device_id to use with "
            "other tools. Connection includes YANG discovery and catalog "
            "build which may take a few minutes for large devices."
        ),
    )
    def connect_device(
        host: str,
        port: int = 830,
        username: str = "admin",
        password: str = "",
        device_id: str = "",
        yang_dir: str = "",
        skip_discover: bool = False,
        skip_fetch_yang: bool = False,
        skip_catalog: bool = False,
        simulate: str = "",
    ) -> dict:
        """Connect to a NETCONF device and return a device_id.

        When ``simulate`` is provided (e.g. "openroadm"), connects to a
        simulated NETCONF backend instead of real hardware.
        """
        t0 = time.monotonic()
        params = dict(host=host, port=port, username=username, password=password,
                      device_id=device_id, yang_dir=yang_dir,
                      skip_discover=skip_discover, skip_fetch_yang=skip_fetch_yang,
                      skip_catalog=skip_catalog, simulate=simulate)
        from splice.api.device import Device

        # Accept "sim://<profile>" as host too — agents pass it naturally
        # since the Python API and CLI both use that form.
        if not simulate and host.startswith("sim://"):
            simulate = host[len("sim://"):]

        did = device_id or (
            f"sim_{simulate}_{uuid.uuid4().hex[:8]}" if simulate
            else f"{host}_{port}_{uuid.uuid4().hex[:8]}"
        )

        # Check if already connected
        existing = pool.get(did)
        if existing is not None:
            result = {
                "device_id": did,
                "status": "already_connected",
                "host": existing.host,
                "port": existing.port,
            }
            audit.log_call("connect_device", params, result, (time.monotonic() - t0) * 1000)
            return result

        try:
            if simulate:
                dev = Device(f"sim://{simulate}")
            else:
                dev = Device(
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                    yang_dir=yang_dir or None,
                )
            dev.connect(
                skip_discover=skip_discover,
                skip_fetch_yang=skip_fetch_yang,
                skip_catalog=skip_catalog,
            )
        except Exception as exc:
            code = _classify_exception(exc)
            result = _make_error(code, str(exc))
            audit.log_call("connect_device", params, result, (time.monotonic() - t0) * 1000)
            return result

        effective_host = f"sim://{simulate}" if simulate else host
        session = _DeviceSession(
            device_id=did,
            device=dev,
            host=effective_host,
            port=port,
        )
        if not pool.add_if_absent(session):
            # Another concurrent connect beat us; close our new session
            try:
                dev.close()
            except Exception:
                pass
            existing = pool.get(did)
            result = {
                "device_id": did,
                "status": "already_connected",
                "host": existing.host if existing else effective_host,
                "port": existing.port if existing else port,
            }
            audit.log_call("connect_device", params, result, (time.monotonic() - t0) * 1000)
            return result

        result = {
            "device_id": did,
            "status": "connected",
            "host": effective_host,
            "port": port,
            "catalog_paths": len(dev.catalog_paths),
        }
        if skip_catalog:
            result["note"] = "catalog not built; call build_catalog or find_paths may be empty"
        audit.log_call("connect_device", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="disconnect_device",
        description="Close a device session and release resources.",
    )
    def disconnect_device(device_id: str) -> dict:
        """Disconnect a device session."""
        t0 = time.monotonic()
        params = dict(device_id=device_id)
        session = pool.remove(device_id)
        if session is None:
            result = _make_error("DEVICE_NOT_CONNECTED", f"no session for device_id={device_id}")
            audit.log_call("disconnect_device", params, result, (time.monotonic() - t0) * 1000)
            return result
        try:
            session.device.close()
        except Exception as exc:
            log.warning("error closing device %s: %s", device_id, exc)
        limiter.clear(device_id)
        result = {"device_id": device_id, "status": "disconnected"}
        audit.log_call("disconnect_device", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="get_telemetry",
        description=(
            "Read current telemetry values for a path or glob pattern from "
            "a connected device. Returns a list of path/value updates."
        ),
    )
    def get_telemetry(device_id: str, path: str) -> dict:
        """Read data from a device via NETCONF get."""
        t0 = time.monotonic()
        params = dict(device_id=device_id, path=path)
        try:
            session = _get_session(device_id)
            _check_rate(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("get_telemetry", params, result, (time.monotonic() - t0) * 1000)
            return result

        try:
            updates = session.device.get(path)
        except Exception as exc:
            code = _classify_exception(exc)
            result = _make_error(code, str(exc))
            audit.log_call("get_telemetry", params, result, (time.monotonic() - t0) * 1000)
            return result

        result = {"device_id": device_id, "path": path, "updates": updates}
        audit.log_call("get_telemetry", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="set_config",
        description=(
            "Push a configuration value to a device via NETCONF edit-config. "
            "Subject to set policy (deny-all by default). Use dry_run=True "
            "to validate without applying."
        ),
    )
    def set_config(
        device_id: str,
        path: str,
        value: str,
        operation: str = "merge",
        dry_run: bool = False,
    ) -> dict:
        """Write configuration to a device."""
        t0 = time.monotonic()
        params = dict(device_id=device_id, path=path, value=value,
                      operation=operation, dry_run=dry_run)
        try:
            session = _get_session(device_id)
            _check_rate(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("set_config", params, result, (time.monotonic() - t0) * 1000)
            return result

        # Evaluate the policy against EVERY form the write could resolve to —
        # the device.set apply path uses the raw path while the executor does
        # its own alias resolution, so checking a single canonicalization would
        # let a crafted path form slip past forbidden/allowed rules.
        from splice.expander import strip_predicates
        alias_map = session.device._catalog.alias_map
        bare = strip_predicates(path)
        candidate_paths = {
            path,                      # exactly what device.set receives
            bare,                      # predicate-stripped
            alias_map.get(bare, bare),     # alias-resolved (stripped)
            alias_map.get(path, path),     # alias-resolved (raw)
        }
        denial = policy.check(list(candidate_paths), value)
        if denial is not None:
            result = _make_error("SET_POLICY_DENIED", denial)
            audit.log_call("set_config", params, result, (time.monotonic() - t0) * 1000)
            return result

        if dry_run:
            result = {
                "device_id": device_id,
                "path": path,
                "value": value,
                "operation": operation,
                "dry_run": True,
                "policy_check": "passed",
            }
            audit.log_call("set_config", params, result, (time.monotonic() - t0) * 1000)
            return result

        try:
            result = session.device.set(path, value, operation=operation)
        except Exception as exc:
            code = _classify_exception(exc)
            result = _make_error(code, str(exc))
            audit.log_call("set_config", params, result, (time.monotonic() - t0) * 1000)
            return result

        result = {
            "device_id": device_id,
            **result,
        }
        audit.log_call("set_config", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="find_paths",
        description=(
            "Search the YANG catalog paths for a connected device using "
            "a glob pattern. Returns matching canonical paths."
        ),
    )
    def find_paths(device_id: str, pattern: str) -> dict:
        """Search catalog paths by glob pattern."""
        t0 = time.monotonic()
        params = dict(device_id=device_id, pattern=pattern)
        try:
            session = _get_session(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("find_paths", params, result, (time.monotonic() - t0) * 1000)
            return result

        matches = session.device.find_paths(pattern)
        result = {
            "device_id": device_id,
            "pattern": pattern,
            "matches": matches,
            "count": len(matches),
        }
        audit.log_call("find_paths", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="list_devices",
        description="List all connected device sessions and their status.",
    )
    def list_devices() -> dict:
        """Show all connected devices."""
        t0 = time.monotonic()
        params: Dict[str, Any] = {}

        now = time.monotonic()
        devices = []
        for s in pool.all_sessions():
            devices.append({
                "device_id": s.device_id,
                "host": s.host,
                "port": s.port,
                "connected": s.device._connected,
                "catalog_paths": len(s.device.catalog_paths) if s.device._connected else 0,
                "idle_sec": round(now - s.last_used, 1),
                "uptime_sec": round(now - s.created_at, 1),
            })
        result = {"devices": devices, "count": len(devices)}
        audit.log_call("list_devices", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="device_health",
        description=(
            "Get health status for a connected device including connection "
            "state, circuit breaker status, and timing information."
        ),
    )
    def device_health(device_id: str) -> dict:
        """Get device connection health."""
        t0 = time.monotonic()
        params = dict(device_id=device_id)
        try:
            session = _get_session(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("device_health", params, result, (time.monotonic() - t0) * 1000)
            return result

        status = session.device.connection_status
        now = time.monotonic()
        status["device_id"] = device_id
        status["idle_sec"] = round(now - session.last_used, 1)
        status["uptime_sec"] = round(now - session.created_at, 1)
        audit.log_call("device_health", params, status, (time.monotonic() - t0) * 1000)
        return status

    @mcp.tool(
        name="list_capabilities",
        description=(
            "List readable and writable YANG paths for a connected device. "
            "Optionally filter by a glob pattern."
        ),
    )
    def list_capabilities(device_id: str, pattern: str = "*") -> dict:
        """Show readable/writable catalog paths."""
        t0 = time.monotonic()
        params = dict(device_id=device_id, pattern=pattern)
        try:
            session = _get_session(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("list_capabilities", params, result, (time.monotonic() - t0) * 1000)
            return result

        dev = session.device
        catalog_map = dev._catalog.catalog_map

        readable = []
        writable = []
        for path, entry in catalog_map.items():
            if pattern != "*" and not fnmatch.fnmatch(path, pattern):
                continue
            info = {"path": path, "data_type": entry.get("data_type", "")}
            readable.append(info)
            if entry.get("config", False):
                writable.append(info)

        result = {
            "device_id": device_id,
            "pattern": pattern,
            "readable": readable,
            "readable_count": len(readable),
            "writable": writable,
            "writable_count": len(writable),
        }
        audit.log_call("list_capabilities", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="search_paths",
        description=(
            "Search device catalog paths using natural language. "
            "Hybrid ranking: BM25 keyword match blended with embedding "
            "similarity when an embedding provider is installed "
            "(pip install splice[search]); keyword-only otherwise. "
            "Example queries: 'Q-factor', 'FEC bit error rate', 'optical power'."
        ),
    )
    def search_paths(device_id: str, query: str, top_k: int = 10) -> dict:
        """Search catalog paths using hybrid BM25 + semantic ranking."""
        t0 = time.monotonic()
        params = dict(device_id=device_id, query=query, top_k=top_k)
        try:
            session = _get_session(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("search_paths", params, result, (time.monotonic() - t0) * 1000)
            return result

        from splice.path_index import PathIndex, build_path_entries

        try:
            # Build or retrieve cached index
            if device_id not in _search_indices:
                entries = build_path_entries(session.device._catalog)
                if not entries:
                    result = {
                        "device_id": device_id,
                        "query": query,
                        "results": [],
                        "count": 0,
                        "note": "device catalog is empty",
                    }
                    audit.log_call("search_paths", params, result, (time.monotonic() - t0) * 1000)
                    return result
                idx = PathIndex(entries)
                idx.build()
                _search_indices[device_id] = idx

            idx = _search_indices[device_id]
            hits = idx.search(query, top_k=top_k)

            results = []
            for entry, score in hits:
                results.append({
                    "path": entry.path,
                    "score": round(score, 4),
                    "module": entry.module,
                    "keyword": entry.keyword,
                    "data_type": entry.data_type,
                    "config": entry.config,
                    "description": entry.description,
                })

        except Exception as exc:
            code = _classify_exception(exc)
            result = _make_error(code, f"search failed: {exc}")
            audit.log_call("search_paths", params, result, (time.monotonic() - t0) * 1000)
            return result

        result = {
            "device_id": device_id,
            "query": query,
            "results": results,
            "count": len(results),
        }
        if not idx.has_embeddings:
            result["note"] = (
                "no embedding provider installed; results ranked by keyword "
                "(BM25) match only. For semantic ranking: "
                "pip install splice[search]"
            )
        audit.log_call("search_paths", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="subscribe_once",
        description=(
            "Subscribe to a path and return current values (ONCE mode). "
            "Returns all matching telemetry values for the given path prefix. "
            "Equivalent to a gNMI Subscribe with ONCE mode."
        ),
    )
    def subscribe_once(device_id: str, path: str) -> dict:
        """One-shot subscription: read current values for a path."""
        t0 = time.monotonic()
        params = dict(device_id=device_id, path=path)
        try:
            session = _get_session(device_id)
            _check_rate(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("subscribe_once", params, result, (time.monotonic() - t0) * 1000)
            return result

        try:
            updates = session.device.get(path)
        except Exception as exc:
            code = _classify_exception(exc)
            result = _make_error(code, str(exc))
            audit.log_call("subscribe_once", params, result, (time.monotonic() - t0) * 1000)
            return result

        result = {
            "device_id": device_id,
            "path": path,
            "updates": updates,
            "count": len(updates),
            "sync": True,
        }
        audit.log_call("subscribe_once", params, result, (time.monotonic() - t0) * 1000)
        return result

    @mcp.tool(
        name="doctor",
        description=(
            "Run diagnostic checks on a connected device. Returns connection "
            "state, circuit breaker status, catalog statistics, and session info."
        ),
    )
    def doctor(device_id: str) -> dict:
        """Run diagnostic checks on a connected device."""
        t0 = time.monotonic()
        params = dict(device_id=device_id)
        try:
            session = _get_session(device_id)
        except _ToolError as e:
            result = e.to_dict()
            audit.log_call("doctor", params, result, (time.monotonic() - t0) * 1000)
            return result

        now = time.monotonic()
        dev = session.device

        # Connection status
        conn_status = dev.connection_status

        # Catalog stats
        catalog_map = dev._catalog.catalog_map
        config_paths = sum(1 for e in catalog_map.values() if e.get("config", False))
        modules = dev._catalog.modules if dev._catalog.modules else []

        # Check cache health
        cached_updates = len(dev.cached_data())

        checks = []

        # 1. Connection check
        if conn_status.get("connected"):
            checks.append({"check": "netconf_connection", "status": "ok", "detail": "connected"})
        else:
            checks.append({"check": "netconf_connection", "status": "fail", "detail": "not connected"})

        # 2. Circuit breaker check
        cb_state = conn_status.get("circuit_breaker_state", "unknown")
        if cb_state == "closed" or cb_state == "unknown":
            checks.append({"check": "circuit_breaker", "status": "ok", "detail": cb_state})
        else:
            checks.append({"check": "circuit_breaker", "status": "warn", "detail": cb_state})

        # 3. Catalog check
        path_count = len(catalog_map)
        if path_count > 0:
            checks.append({"check": "catalog", "status": "ok", "detail": f"{path_count} paths"})
        else:
            checks.append({"check": "catalog", "status": "warn", "detail": "catalog is empty"})

        # 4. Cache check
        checks.append({"check": "cache", "status": "ok", "detail": f"{cached_updates} cached updates"})

        all_ok = all(c["status"] == "ok" for c in checks)

        result = {
            "device_id": device_id,
            "overall": "healthy" if all_ok else "degraded",
            "checks": checks,
            "stats": {
                "catalog_paths": path_count,
                "config_paths": config_paths,
                "read_only_paths": path_count - config_paths,
                "modules": len(modules),
                "cached_updates": cached_updates,
                "aliases": len(dev._catalog.alias_map),
            },
            "session": {
                "host": session.host,
                "port": session.port,
                "uptime_sec": round(now - session.created_at, 1),
                "idle_sec": round(now - session.last_used, 1),
            },
            "connection": conn_status,
        }
        audit.log_call("doctor", params, result, (time.monotonic() - t0) * 1000)
        return result

    # Store references for testing
    mcp._gnmi_pool = pool  # type: ignore[attr-defined]
    mcp._gnmi_policy = policy  # type: ignore[attr-defined]
    mcp._gnmi_limiter = limiter  # type: ignore[attr-defined]
    mcp._gnmi_audit = audit  # type: ignore[attr-defined]
    mcp._gnmi_search_indices = _search_indices  # type: ignore[attr-defined]

    return mcp


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

class _ToolError(Exception):
    """Raised inside tool helpers to short-circuit with a structured error."""

    def __init__(self, code: str, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after

    def to_dict(self) -> Dict[str, Any]:
        return _make_error(self.code, str(self), self.retry_after)


# ---------------------------------------------------------------------------
# Stdio runner
# ---------------------------------------------------------------------------

def run_stdio(server: Any) -> None:
    """Run the MCP server on stdio transport (blocking)."""
    asyncio.run(server.run_stdio_async())


def main(
    policy_path: Optional[str] = None,
    audit_log_path: Optional[str] = None,
) -> None:
    """Entry point for ``splice mcp``."""
    policy = None
    if policy_path:
        policy = SetPolicy.from_yaml(policy_path)
    server = create_mcp_server(set_policy=policy, audit_log_path=audit_log_path)
    run_stdio(server)
