# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Telemetry service — polls path values via gNMI Get or DB fallback."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from splice.tui.services.device_registry import DeviceEntry

log = logging.getLogger(__name__)


HISTORY_SIZE = 60  # ~3 minutes at 3s polling


@dataclass
class TelemetryUpdate:
    """A single telemetry value update."""

    path: str
    value: object = None
    timestamp_ns: int = 0
    source: str = "none"  # "gnmi" | "db" | "none"


@dataclass
class Threshold:
    """Per-path alerting thresholds."""

    low: float | None = None
    high: float | None = None


class TelemetryService:
    """Polls watched paths via gNMI Get or DB last_value fallback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._watched: list[str] = []
        self._device: DeviceEntry | None = None
        self._channel = None
        self._stub = None
        self._previous: dict[str, object] = {}  # path → last value for trend
        self._history: dict[str, deque[float]] = {}  # path → ring buffer
        self._thresholds: dict[str, Threshold] = {}  # path → alert thresholds

    def set_device(self, entry: DeviceEntry) -> None:
        """Set the active device, resetting gRPC connection and all state."""
        with self._lock:
            self._device = entry
            self._close_channel_unlocked()
            # Clear all per-device state so stale data doesn't leak across devices
            self._watched.clear()
            self._previous.clear()
            self._history.clear()
            self._thresholds.clear()

    def watch(self, path: str) -> bool:
        """Add a path to the watch list. Returns True if newly added.

        Returns False if the path is already watched. Note: watching without
        a device is allowed (paths are queued), but poll_once() will no-op.
        """
        with self._lock:
            if path not in self._watched:
                self._watched.append(path)
                return True
            return False

    def unwatch(self, path: str) -> bool:
        """Remove a path from the watch list. Returns True if removed."""
        with self._lock:
            if path in self._watched:
                self._watched.remove(path)
                self._previous.pop(path, None)
                self._history.pop(path, None)
                self._thresholds.pop(path, None)
                return True
            return False

    @property
    def watched_paths(self) -> list[str]:
        with self._lock:
            return list(self._watched)

    def clear(self) -> None:
        """Remove all watched paths and reset device state."""
        with self._lock:
            self._watched.clear()
            self._previous.clear()
            self._history.clear()
            self._thresholds.clear()
            self._device = None
            self._close_channel_unlocked()

    def get_current_value(self, path: str) -> object | None:
        """Return the last polled value for a path."""
        with self._lock:
            return self._previous.get(path)

    def get_trend(self, path: str, current_value: object) -> str:
        """Return trend indicator comparing current to previous value."""
        with self._lock:
            prev = self._previous.get(path)
            self._previous[path] = current_value
        if prev is None or current_value is None:
            return " "
        try:
            prev_f = float(prev)
            curr_f = float(current_value)
            if curr_f > prev_f:
                return "\u2191"  # ↑
            elif curr_f < prev_f:
                return "\u2193"  # ↓
            else:
                return "\u2192"  # →
        except (ValueError, TypeError):
            if prev != current_value:
                return "\u0394"  # Δ
            return "\u2192"  # →

    # -- Value history -------------------------------------------------------

    def record_value(self, path: str, value: object) -> None:
        """Record a numeric value in the per-path ring buffer."""
        try:
            fval = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            if path not in self._history:
                self._history[path] = deque(maxlen=HISTORY_SIZE)
            self._history[path].append(fval)

    def get_history(self, path: str) -> list[float]:
        """Return recent numeric values for a path."""
        with self._lock:
            return list(self._history.get(path, []))

    # -- Threshold alerts ----------------------------------------------------

    def set_threshold(self, path: str, low: float | None = None, high: float | None = None) -> None:
        with self._lock:
            self._thresholds[path] = Threshold(low=low, high=high)

    def get_threshold(self, path: str) -> Threshold | None:
        with self._lock:
            return self._thresholds.get(path)

    def clear_threshold(self, path: str) -> None:
        with self._lock:
            self._thresholds.pop(path, None)

    def check_threshold(self, path: str, value: object) -> str | None:
        """Returns 'high', 'low', or None."""
        with self._lock:
            th = self._thresholds.get(path)
        if th is None:
            return None
        try:
            fval = float(value)
        except (TypeError, ValueError):
            return None
        if th.high is not None and fval >= th.high:
            return "high"
        if th.low is not None and fval <= th.low:
            return "low"
        return None

    # -- DB history ----------------------------------------------------------

    def fetch_db_history(self, path: str, limit: int = 100) -> list[dict]:
        """Fetch telemetry_history rows for a path from DB."""
        if not self._device or not self._device.db_url:
            return []
        try:
            from sqlalchemy import select

            from splice.db.models import TelemetryHistory
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(self._device.db_url)
            try:
                device = lookup_device(session, self._device)
                if device is None:
                    return []
                rows = (
                    session.execute(
                        select(TelemetryHistory)
                        .where(
                            TelemetryHistory.device_id == device.id,
                            TelemetryHistory.canonical_path == path,
                        )
                        .order_by(TelemetryHistory.timestamp_ns.desc())
                        .limit(limit)
                    )
                    .scalars()
                    .all()
                )
                return [
                    {
                        "value": r.value_json,
                        "timestamp_ns": r.timestamp_ns,
                        "created_at": str(r.created_at) if r.created_at else "-",
                    }
                    for r in rows
                ]
            finally:
                session.close()
        except Exception as exc:
            log.debug("DB history query failed: %s", exc)
            return []

    def poll_once(self) -> list[TelemetryUpdate]:
        """Fetch current values for all watched paths. Thread-safe."""
        with self._lock:
            if not self._watched or self._device is None:
                return []

        # Try gNMI first
        updates = self._poll_gnmi()
        if updates is not None:
            return updates

        # Fall back to DB
        updates = self._poll_db()
        if updates is not None:
            return updates

        # Neither available — return empty updates
        return [TelemetryUpdate(path=p) for p in self._watched]

    def get_status(self) -> str:
        """Return current connection status string."""
        if self._device is None:
            return "No device"
        if self._stub is not None:
            return f"gNMI :{self._device.gnmi_port}"
        if self._device.db_url:
            return "DB fallback"
        return "Disconnected"

    # -- gNMI backend --------------------------------------------------------

    def _poll_gnmi(self) -> list[TelemetryUpdate] | None:
        """Try to poll via gNMI Get. Returns None if unavailable."""
        try:
            stub = self._get_or_create_stub()
            if stub is None:
                return None

            from splice import gnmi_pb2

            # Build GetRequest with all watched paths (strip namespace prefixes)
            paths = []
            for p in self._watched:
                paths.append(_path_from_str(_strip_prefixes(p)))

            request = gnmi_pb2.GetRequest(path=paths, encoding=0)  # 0 = JSON
            response = stub.Get(request, timeout=5.0)

            # Parse response
            updates = {p: TelemetryUpdate(path=p) for p in self._watched}
            for notification in response.notification:
                for update in notification.update:
                    path_str = _path_to_str(update.path)
                    val = _extract_value(update.val)
                    ts = notification.timestamp
                    if path_str in updates:
                        updates[path_str] = TelemetryUpdate(
                            path=path_str, value=val,
                            timestamp_ns=ts, source="gnmi",
                        )
                    else:
                        # Try matching without leading /
                        for watched in self._watched:
                            if _paths_match(watched, path_str):
                                updates[watched] = TelemetryUpdate(
                                    path=watched, value=val,
                                    timestamp_ns=ts, source="gnmi",
                                )
                                break

            return list(updates.values())

        except Exception as exc:
            log.debug("gNMI poll failed: %s", exc)
            self._close_channel()
            return None

    def _get_or_create_stub(self):
        """Lazily create gRPC channel and stub."""
        with self._lock:
            if self._stub is not None:
                return self._stub
            if self._device is None:
                return None
            try:
                import grpc
                from splice import gnmi_pb2_grpc

                target = f"localhost:{self._device.gnmi_port}"
                self._channel = grpc.insecure_channel(target)
                self._stub = gnmi_pb2_grpc.gNMIStub(self._channel)
                # Quick connectivity check
                grpc.channel_ready_future(self._channel).result(timeout=2.0)
                log.debug("gNMI channel connected to %s", target)
                return self._stub
            except Exception as exc:
                log.debug("gNMI channel failed: %s", exc)
                self._close_channel_unlocked()
                return None

    def close(self) -> None:
        """Public cleanup — close gRPC channel."""
        self._close_channel()

    def _close_channel(self) -> None:
        """Close gRPC channel if open (acquires lock)."""
        with self._lock:
            self._close_channel_unlocked()

    def _close_channel_unlocked(self) -> None:
        """Close gRPC channel if open (caller must hold lock)."""
        self._stub = None
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                log.debug("Error closing gRPC channel", exc_info=True)
            self._channel = None

    # -- DB backend ----------------------------------------------------------

    def _poll_db(self) -> list[TelemetryUpdate] | None:
        """Try to poll via DB last_value table. Returns None if unavailable."""
        if not self._device or not self._device.db_url:
            return None
        try:
            from splice.db.last_value_repository import fetch_last_values
            from splice.tui.services._db import get_session, lookup_device

            session = get_session(self._device.db_url)
            try:
                device = lookup_device(session, self._device)
                if device is None:
                    return None

                last_values = fetch_last_values(session, device.id)

                updates = []
                for p in self._watched:
                    if p in last_values:
                        lv = last_values[p]
                        updates.append(TelemetryUpdate(
                            path=p,
                            value=lv["value"],
                            timestamp_ns=lv["timestamp_ns"],
                            source="db",
                        ))
                    else:
                        updates.append(TelemetryUpdate(path=p))
                return updates
            finally:
                session.close()
        except Exception as exc:
            log.debug("DB poll failed: %s", exc)
            return None


# -- Path helpers -------------------------------------------------------------
# Reuse the gNMI server's canonical conversions so the TUI parses/serializes
# keyed paths IDENTICALLY to the bridge. The previous local copies rendered key
# values DECODED while the server serves them percent-ENCODED, so watched keyed
# paths (e.g. /pm/...[interface=1%2F2%2Fn1%2Fot400]) never matched the response
# and telemetry showed no value.
from splice.gnmi_server import _path_from_str, _path_to_str  # noqa: E402


def _extract_value(typed_value) -> object:
    """Extract Python value from gNMI TypedValue."""
    which = typed_value.WhichOneof("value")
    if which is None:
        return None
    if which == "json_val":
        import json
        try:
            return json.loads(typed_value.json_val)
        except Exception:
            return typed_value.json_val.decode("utf-8", errors="replace")
    if which == "json_ietf_val":
        import json
        try:
            return json.loads(typed_value.json_ietf_val)
        except Exception:
            return typed_value.json_ietf_val.decode("utf-8", errors="replace")
    return getattr(typed_value, which)


def _strip_prefixes(path: str) -> str:
    """Remove YANG namespace prefixes (e.g., p1:foo/p2:bar → foo/bar).

    Strips only element-name prefixes, never anything inside a [predicate]
    (an encoded key value never contains ':', but a decoded one might, e.g.
    an interface named 'eth0:1' — those must be left intact)."""
    import re

    out = []
    for seg in path.strip("/").split("/"):
        if "[" in seg:
            name, rest = seg.split("[", 1)
            name = re.sub(r'^[A-Za-z0-9_-]+:', '', name)
            out.append(f"{name}[{rest}")
        else:
            out.append(re.sub(r'^[A-Za-z0-9_-]+:', '', seg))
    return "/".join(out)


def _paths_match(a: str, b: str) -> bool:
    """True if two path strings denote the same path, tolerating namespace
    prefixes, a leading '/', and percent-encoding differences in key values
    (the bridge serves encoded keys; a watched path may be decoded)."""
    from urllib.parse import unquote

    return unquote(_strip_prefixes(a)) == unquote(_strip_prefixes(b))
