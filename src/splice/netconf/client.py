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
import threading
import time
from typing import Callable, Optional

from ncclient import manager
from ncclient.transport.errors import AuthenticationError

_DEBUG_ENV = "GNMI_LIST_INSTANCE_DEBUG"


class CircuitBreakerOpen(RuntimeError):
    """Raised when the circuit breaker is open due to consecutive failures."""

    def __init__(self, cooldown_remaining: float, failure_count: int) -> None:
        self.cooldown_remaining = cooldown_remaining
        self.failure_count = failure_count
        super().__init__(
            f"Circuit breaker open — {failure_count} consecutive "
            f"failures, cooling down for {cooldown_remaining:.0f}s more"
        )


def _debug_enabled() -> bool:
    val = os.environ.get(_DEBUG_ENV, "").strip().lower()
    return val not in ("", "0", "false", "no")


def _force_close_session(mgr, timeout_sec: float = 5.0) -> None:
    """Close ncclient manager's SSH transport without sending close-session RPC.

    Always safe to call — swallows all exceptions. Prefers SSHSession.close()
    (which handles transport + channel + state) with a fallback to raw
    transport.close().

    Runs the close in a daemon thread with a join deadline: a wedged SSH
    transport (device link dropped mid-RPC) can make close() block, and this
    is called from inside the pool lock, so a blocking close would freeze the
    entire pool. If the close overruns we abandon the thread (daemon) and move
    on — the socket gets reaped by the OS / GC.
    """
    if mgr is None:
        return

    def _do_close():
        try:
            session = getattr(mgr, "_session", None)
            if session is not None:
                session.close()
                return
        except Exception:
            pass
        try:
            session = getattr(mgr, "_session", None)
            if session is not None:
                transport = getattr(session, "_transport", None)
                if transport is not None:
                    transport.close()
        except Exception:
            pass

    t = threading.Thread(target=_do_close, name="netconf-force-close", daemon=True)
    t.start()
    t.join(timeout=timeout_sec)


class NetconfClient:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        connect_timeout_sec: int = 10,
        rpc_timeout_sec: int = 10,
        keepalive_sec: int = 30,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.connect_timeout_sec = connect_timeout_sec
        self.rpc_timeout_sec = rpc_timeout_sec
        self.keepalive_sec = keepalive_sec
        self._mgr = None
        self._hello_logged = False
        self._lock = threading.RLock()

    def connect(self) -> None:
        with self._lock:
            if self._mgr is not None and getattr(self._mgr, "connected", False):
                return
            # Close stale/dead session before creating a new one
            old_mgr = self._mgr
            self._mgr = None
            if old_mgr is not None:
                _force_close_session(old_mgr)
            self._mgr = manager.connect(
                host=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.connect_timeout_sec,
                hostkey_verify=False,
                allow_agent=False,
                look_for_keys=False,
                keepalive=self.keepalive_sec,
            )
            try:
                self._mgr.timeout = int(self.rpc_timeout_sec)
            except Exception:
                pass
            if _debug_enabled() and not self._hello_logged:
                logger = logging.getLogger(__name__)
                logger.info(
                    "NETCONF session connect host=%s port=%s username=%s connect_timeout=%s rpc_timeout=%s keepalive=%s",
                    self.host,
                    self.port,
                    self.username,
                    self.connect_timeout_sec,
                    self.rpc_timeout_sec,
                    self.keepalive_sec,
                )
                caps = list(getattr(self._mgr, "server_capabilities", []) or [])
                if caps:
                    logger.info("NETCONF server capabilities (%d):\n%s", len(caps), "\n".join(caps))
                else:
                    logger.info("NETCONF server capabilities: none")
                self._hello_logged = True

    def close(self, graceful: bool = True) -> None:
        with self._lock:
            mgr = self._mgr
            self._mgr = None
            if mgr is None:
                return
            if graceful:
                try:
                    mgr.close_session()   # sends RPC + calls session.close()
                    return                 # success — transport already closed
                except Exception:
                    pass                   # RPC failed — fall through to force close
            # Force close the SSH session directly (no RPC)
            _force_close_session(mgr)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()
        return False

    def get(self, filter_xml: str):
        with self._lock:
            self.connect()
            return self._mgr.get(filter_xml)

    def get_config(self, source: str, filter_xml: str):
        with self._lock:
            self.connect()
            return self._mgr.get_config(source=source, filter=filter_xml)

    def dispatch(self, rpc_elem):
        with self._lock:
            self.connect()
            return self._mgr.dispatch(rpc_elem)

    def edit_config(self, target: str, config: str):
        with self._lock:
            self.connect()
            return self._mgr.edit_config(target=target, config=config)

    def commit(self):
        with self._lock:
            self.connect()
            return self._mgr.commit()

    def discard_changes(self):
        with self._lock:
            self.connect()
            return self._mgr.discard_changes()

    def lock(self, target: str):
        with self._lock:
            self.connect()
            return self._mgr.lock(target=target)

    def unlock(self, target: str):
        with self._lock:
            self.connect()
            return self._mgr.unlock(target=target)

    def create_subscription(self, *args, **kwargs):
        with self._lock:
            self.connect()
            return self._mgr.create_subscription(*args, **kwargs)

    def take_notification(self, timeout: int = 1):
        with self._lock:
            if self._mgr is None:
                return None
            return self._mgr.take_notification(timeout=timeout)

    def manager(self):
        with self._lock:
            self.connect()
            return self._mgr


class NetconfClientPool:
    """Single-session pool with reconnect-once-on-failure semantics.

    On an RPC failure the pool closes the dead session, waits
    ``reconnect_delay_sec``, reconnects, and retries the RPC **once**.
    If the retry also fails, the error propagates to the caller
    immediately — no further connections are attempted for that call.

    A circuit breaker tracks consecutive failures across all callers.
    After ``circuit_breaker_threshold`` consecutive failures the pool
    refuses new RPCs for ``circuit_breaker_cooldown_sec``, preventing
    session storms that trigger device auth lockouts.

    Auth failures immediately open the circuit for a longer cooldown
    (``auth_failure_cooldown_sec``) to prevent account lockouts.
    """

    def __init__(self, client: NetconfClient,
                 reconnect_delay_sec: float = 2.0,
                 circuit_breaker_threshold: int = 5,
                 circuit_breaker_cooldown_sec: float = 60.0,
                 auth_failure_cooldown_sec: float = 300.0) -> None:
        self.client = client
        self.reconnect_delay_sec = reconnect_delay_sec
        self._log = logging.getLogger(__name__)
        # Circuit breaker: after N consecutive failures, stop trying for a cooldown
        self._cb_threshold = circuit_breaker_threshold
        self._cb_cooldown = circuit_breaker_cooldown_sec
        self._auth_cooldown = auth_failure_cooldown_sec
        self._consecutive_failures = 0
        self._cb_open_until: float = 0.0  # timestamp when circuit breaker resets
        self._auth_failure_count: int = 0
        self._lock = threading.Lock()
        self._server_capabilities_cache: list[str] | None = None

    def safe_connect(self) -> None:
        """Connect with circuit breaker check to prevent hammering locked devices."""
        with self._lock:
            self._check_circuit_breaker()
        try:
            self.client.connect()
        except Exception as exc:
            with self._lock:
                is_auth = self._is_auth_error(exc)
                self._record_failure(exc, is_auth=is_auth)
            raise
        with self._lock:
            self._record_success()

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        """Detect failures that mean "stop connecting" — auth rejections AND
        device-side access/session-limit conditions.

        These all share one property: retrying makes them worse (account
        lockout, session exhaustion). They must open the long cooldown, not be
        retried like a transient network blip. Vendor wording varies, so match
        the load-bearing phrases each emits (ADVA/ConfD: "access denied",
        "too many sessions", "session limit"; Lumentum/others: "permission
        denied", "lock-denied", "resource denied").
        """
        if isinstance(exc, AuthenticationError):
            return True
        msg = str(exc).lower()
        if "authentication" in msg and "failed" in msg:
            return True
        lockout_signatures = (
            "access denied",
            "access-denied",
            "permission denied",
            "authorization failed",
            "not authorized",
            "unauthorized",
            "too many sessions",
            "session limit",
            "session-limit",
            "maximum number of sessions",
            "resource denied",
            "lock-denied",
            "license limit",
        )
        return any(sig in msg for sig in lockout_signatures)

    def _check_circuit_breaker(self) -> None:
        """Raise if the circuit breaker is open (too many consecutive failures)."""
        now = time.time()
        if now < self._cb_open_until:
            remaining = self._cb_open_until - now
            raise CircuitBreakerOpen(
                cooldown_remaining=remaining,
                failure_count=self._consecutive_failures,
            )
        if self._cb_open_until > 0 and self._consecutive_failures > 0:
            # Cooldown expired — allow one attempt through
            self._log.info("Circuit breaker cooldown expired, allowing retry")
            self._consecutive_failures = 0
            self._auth_failure_count = 0
            self._cb_open_until = 0.0

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._auth_failure_count = 0

    def _record_failure(self, exc: Exception, is_auth: bool = False) -> None:
        self._consecutive_failures += 1
        if is_auth:
            self._auth_failure_count += 1
            cooldown = self._auth_cooldown
            self._log.error(
                "AUTH FAILURE — Circuit breaker OPEN for auth failure, "
                "pausing connections for %.0fs (long cooldown to prevent lockout)",
                cooldown,
            )
        elif self._consecutive_failures >= self._cb_threshold:
            cooldown = self._cb_cooldown
            self._log.error(
                "Circuit breaker OPEN — %d consecutive failures, "
                "pausing connections for %.0fs",
                self._consecutive_failures, cooldown,
            )
        else:
            return
        self._cb_open_until = time.time() + cooldown

    def get_status(self) -> dict:
        """Return passive pool status for health checks (no network calls).

        Deliberately lock-free: the big ``_lock`` is held across NETCONF RPCs
        (single shared session), so acquiring it here would make /health hang
        behind an in-flight poll for up to rpc_timeout_sec. Each field below is
        a single int/float — reads are atomic under the GIL; a momentarily
        stale health snapshot is an acceptable trade for a health endpoint that
        never blocks.
        """
        now = time.time()
        cb_open_until = self._cb_open_until
        return {
            "consecutive_failures": self._consecutive_failures,
            "auth_failure_count": self._auth_failure_count,
            "circuit_breaker_open": now < cb_open_until,
            "circuit_breaker_seconds_remaining": max(0.0, cb_open_until - now),
        }

    def _call_rpc(self, op_name: str, fn: Callable[[], object]):
        with self._lock:
            self._check_circuit_breaker()
            try:
                result = fn()
                self._record_success()
                return result
            except Exception as e:
                self._log.warning("NETCONF %s failed: %s", op_name, e)
                self.client.close(graceful=False)
                if self._is_auth_error(e):
                    self._log.error("Authentication failed — not retrying (possible device lockout)")
                    self._record_failure(e, is_auth=True)
                    raise
                delay = self.reconnect_delay_sec
        # Release lock during sleep so other threads aren't blocked
        self._log.info("Reconnecting in %.1fs...", delay)
        time.sleep(delay)
        with self._lock:
            self._check_circuit_breaker()
            try:
                result = fn()
                self._record_success()
                return result
            except Exception as e2:
                self._log.warning("NETCONF %s retry failed: %s", op_name, e2)
                self.client.close(graceful=False)
                is_auth2 = self._is_auth_error(e2)
                self._record_failure(e2, is_auth=is_auth2)
                raise

    def get(self, filter_xml: str):
        return self._call_rpc("get", lambda: self.client.get(filter_xml))

    def get_config(self, source: str, filter_xml: str):
        return self._call_rpc("get_config", lambda: self.client.get_config(source, filter_xml))

    def edit_config(self, target: str, config: str):
        return self._call_rpc("edit_config", lambda: self.client.edit_config(target, config))

    def commit(self):
        return self._call_rpc("commit", self.client.commit)

    def discard_changes(self):
        return self._call_rpc("discard_changes", self.client.discard_changes)

    def lock(self, target: str):
        return self._call_rpc("lock", lambda: self.client.lock(target))

    def unlock(self, target: str):
        return self._call_rpc("unlock", lambda: self.client.unlock(target))

    def get_server_capabilities(self) -> list[str]:
        with self._lock:
            if self._server_capabilities_cache is not None:
                return list(self._server_capabilities_cache)
            self._check_circuit_breaker()
            try:
                mgr = self.client.manager()
                caps = list(getattr(mgr, "server_capabilities", []) or [])
                self._server_capabilities_cache = caps
                self._record_success()
                return list(caps)
            except Exception as e:
                self._log.warning("NETCONF capabilities failed: %s", e)
                self.client.close(graceful=False)
                if self._is_auth_error(e):
                    self._log.error("Authentication failed — not retrying (possible device lockout)")
                    self._record_failure(e, is_auth=True)
                    raise
                delay = self.reconnect_delay_sec
        # Release the lock during the reconnect sleep so polling and Set on
        # other threads aren't blocked (matches _call_rpc's contract).
        self._log.info("Reconnecting in %.1fs...", delay)
        time.sleep(delay)
        with self._lock:
            if self._server_capabilities_cache is not None:
                return list(self._server_capabilities_cache)
            self._check_circuit_breaker()
            try:
                mgr = self.client.manager()
                caps = list(getattr(mgr, "server_capabilities", []) or [])
                self._server_capabilities_cache = caps
                self._record_success()
                return list(caps)
            except Exception as e2:
                self._log.warning("NETCONF capabilities retry failed: %s", e2)
                self.client.close(graceful=False)
                is_auth2 = self._is_auth_error(e2)
                self._record_failure(e2, is_auth=is_auth2)
                raise
