# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import fnmatch
import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

import splice.gnmi_pb2 as gnmi_pb2
from splice.api._catalog import InMemoryCatalog
from splice.api._errors import (
    ConnectionError,
    DeviceError,
    DiscoveryError,
    PathNotFoundError,
    PathNotWritableError,
    SetError,
    SubscriptionError,
)
from splice.api._subscription import Subscription
from splice.cache_store import MemoryCache
from splice.gnmi_server import _path_from_str, _typed_value
from splice.netconf.client import CircuitBreakerOpen, NetconfClient, NetconfClientPool
from splice.netconf.poller import parse_xml_to_kv
from splice.pathing import is_path_prefix, normalize_path

log = logging.getLogger(__name__)


class Device:
    """Programmatic Python API for a single NETCONF device.

    Provides connect/discover, get, set, subscribe, and optional gRPC
    serving -- all without requiring a database or YAML config files.

    Quick start (full pipeline)::

        device = Device("10.0.0.1", username="admin", password="secret")
        device.connect()
        data = device.get("/interfaces/interface")
        device.close()

    Granular steps (skip expensive discovery)::

        device = Device("10.0.0.1", username="admin", password="secret",
                        yang_dir="/path/to/cached/yang")
        device.connect_netconf()          # SSH only
        device.build_catalog()            # reuse existing YANG files
        data = device.get("/interfaces/interface")

    Context-manager::

        with Device("10.0.0.1", username="admin", password="secret") as dev:
            data = dev.get("/interfaces/interface")
    """

    def __init__(
        self,
        host: str,
        port: int = 830,
        username: str = "admin",
        password: str = "",
        yang_dir: Optional[str] = None,
        cache_ttl_sec: int = 120,
        default_poll_interval_sec: int = 30,
        include_config: bool = True,
        reconnect_delay_sec: float = 2.0,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown_sec: float = 60.0,
        kafka_brokers: Optional[str] = None,
        kafka_topic_prefix: str = "splice",
        batch_polling: bool = True,
        simulate: Optional[str] = None,
    ) -> None:
        # Detect sim:// URI scheme  (e.g. "sim://openroadm"). An explicit
        # simulate= kwarg wins over the host scheme — otherwise a caller that
        # passes a placeholder host plus simulate="openroadm" (e.g. the CLI's
        # --simulate path) would be hijacked by the host's "placeholder".
        self._simulate: Optional[str] = simulate
        if host.startswith("sim://"):
            self._simulate = self._simulate or host[len("sim://"):]
            host = "sim://localhost"

        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._include_config = include_config
        self._default_poll_interval = default_poll_interval_sec
        self._batch_polling = batch_polling
        self._kafka_brokers = kafka_brokers
        self._kafka_topic_prefix = kafka_topic_prefix

        # YANG directory -- use a temp dir if not supplied
        self._yang_dir_provided = yang_dir is not None
        self._yang_dir = yang_dir or ""
        self._tmp_yang_dir: Optional[str] = None

        # Core objects -- created during connect()
        self._client: Optional[NetconfClient] = None
        self._pool: Optional[NetconfClientPool] = None
        self._catalog = InMemoryCatalog()
        self._cache = MemoryCache(ttl_sec=cache_ttl_sec)
        self._kafka: Any = None
        self._grpc_server: Any = None
        self._subscriptions: List[Subscription] = []
        self._connected = False

        # Discovery state
        self._discovered_modules: Optional[List[Dict]] = None
        self._path_index: Any = None

        # Client config
        self._reconnect_delay_sec = reconnect_delay_sec
        self._circuit_breaker_threshold = circuit_breaker_threshold
        self._circuit_breaker_cooldown_sec = circuit_breaker_cooldown_sec

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(
        self,
        *,
        skip_discover: bool = False,
        skip_fetch_yang: bool = False,
        skip_catalog: bool = False,
    ) -> None:
        """Connect to the NETCONF device and run the discovery pipeline.

        By default runs all steps:

        1. Establish NETCONF session
        2. Discover YANG modules
        3. Fetch YANG schemas to disk
        4. Build in-memory catalog
        5. Initialize default polling jobs
        6. Optionally create Kafka publisher

        Use keyword arguments to skip expensive steps when reusing cached
        data (e.g. a pre-populated ``yang_dir``):

        - ``skip_discover=True`` -- skip module discovery (step 2).
          Requires either a previous ``discover()`` call or
          ``skip_fetch_yang=True`` as well.
        - ``skip_fetch_yang=True`` -- skip YANG schema fetch (step 3).
          Requires ``yang_dir`` to already contain ``.yang`` files.
        - ``skip_catalog=True`` -- skip catalog build (steps 4-5).
          You must call ``build_catalog()`` later before using
          ``get()``, ``set()``, or ``subscribe()``.
        """
        self.connect_netconf()

        if not skip_discover:
            self.discover()

        if not skip_fetch_yang:
            if self._discovered_modules is None:
                raise DiscoveryError(
                    "cannot fetch YANG schemas without discovered modules; "
                    "call discover() first or use skip_fetch_yang=True"
                )
            self.fetch_yang()

        if not skip_catalog:
            self.build_catalog()

        # Kafka publisher (optional)
        if self._kafka_brokers and self._kafka is None:
            from splice.kafka_publisher import KafkaPublisher

            self._kafka = KafkaPublisher(
                enabled=True,
                brokers=self._kafka_brokers,
                topic_prefix=self._kafka_topic_prefix,
            )

    def connect_netconf(self) -> None:
        """Establish the NETCONF SSH session only (step 1).

        Creates ``NetconfClient`` (or ``SimulatedNetconfClient`` when in
        simulation mode) and ``NetconfClientPool``.  Does not run any
        discovery.  After this call, low-level pool methods are available
        but ``get()``/``set()``/``subscribe()`` will not work until
        ``build_catalog()`` has been called.
        """
        if self._pool is not None:
            return  # already connected

        if self._simulate:
            from splice.simulator import make_simulated_client

            self._client = make_simulated_client(self._simulate)
        else:
            self._client = NetconfClient(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
            )
        self._pool = NetconfClientPool(
            client=self._client,
            reconnect_delay_sec=self._reconnect_delay_sec,
            circuit_breaker_threshold=self._circuit_breaker_threshold,
            circuit_breaker_cooldown_sec=self._circuit_breaker_cooldown_sec,
        )
        try:
            self._pool.safe_connect()
        except Exception as exc:
            self._client = None
            self._pool = None
            raise ConnectionError(f"NETCONF connect failed: {exc}") from exc

        # Resolve yang_dir
        if not self._yang_dir:
            self._tmp_yang_dir = tempfile.mkdtemp(prefix="splice_yang_")
            self._yang_dir = self._tmp_yang_dir

    def discover(self) -> List[Dict]:
        """Discover YANG modules from the device (step 2).

        Returns the list of discovered module dicts and stores them
        internally for ``fetch_yang()`` to use.

        Requires ``connect_netconf()`` to have been called first.
        """
        if self._pool is None:
            raise ConnectionError("NETCONF not connected; call connect_netconf() first")

        from splice.api._discovery import discover_modules

        try:
            source, modules = discover_modules(self._pool)
        except Exception as exc:
            raise DiscoveryError(f"module discovery failed: {exc}") from exc

        self._discovered_modules = modules
        log.info("discovered %d modules via %s", len(modules), source)
        return modules

    def fetch_yang(self, throttle_sec: float = 0.3) -> int:
        """Fetch YANG schemas from the device to ``yang_dir`` (step 3).

        Skips files that already exist on disk, so this is safe to call
        repeatedly.  Returns the number of schemas written/found.

        Requires ``discover()`` to have been called first (or modules
        to be set via ``_discovered_modules``).
        """
        if self._pool is None:
            raise ConnectionError("NETCONF not connected; call connect_netconf() first")
        if self._discovered_modules is None:
            raise DiscoveryError(
                "no modules discovered; call discover() first"
            )

        from splice.api._discovery import fetch_yang_schemas

        try:
            written = fetch_yang_schemas(
                self._pool, self._discovered_modules, self._yang_dir,
                throttle_sec=throttle_sec,
            )
        except Exception as exc:
            raise DiscoveryError(f"YANG schema fetch failed: {exc}") from exc

        log.info("fetched %d YANG schemas to %s", written, self._yang_dir)
        return written

    def build_catalog(self) -> None:
        """Build in-memory catalog from YANG files on disk (step 4).

        Reads all ``.yang`` files from ``yang_dir``, builds the catalog,
        alias map, type map, and list keys, then initializes default
        polling jobs.

        This method does **not** require a NETCONF connection -- it only
        needs YANG files on disk.  This makes it possible to:

        - Pre-populate ``yang_dir`` and skip discovery entirely
        - Call ``connect(skip_discover=True, skip_fetch_yang=True)``
          followed by ``build_catalog()`` for instant startup
        """
        from splice.api._discovery import build_catalog_in_memory

        if not self._yang_dir:
            raise DiscoveryError("yang_dir is not set")

        try:
            self._catalog = build_catalog_in_memory(
                self._yang_dir, include_config=self._include_config
            )
        except Exception as exc:
            raise DiscoveryError(f"catalog build failed: {exc}") from exc

        if self._discovered_modules:
            self._catalog.modules = self._discovered_modules

        log.info(
            "catalog: %d paths, %d aliases",
            len(self._catalog.catalog_map),
            len(self._catalog.alias_map),
        )

        self._catalog.init_default_jobs(self._default_poll_interval)
        self._connected = True

    def close(self) -> None:
        """Tear down all resources."""
        for sub in list(self._subscriptions):
            try:
                sub.cancel()
            except Exception:
                pass
        self._subscriptions.clear()

        if self._grpc_server is not None:
            try:
                self._grpc_server.stop(grace=2)
            except Exception:
                pass
            self._grpc_server = None

        if self._kafka is not None:
            try:
                self._kafka.close()
            except Exception:
                pass
            self._kafka = None

        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._pool = None

        if self._tmp_yang_dir and os.path.isdir(self._tmp_yang_dir):
            import shutil

            shutil.rmtree(self._tmp_yang_dir, ignore_errors=True)
            self._tmp_yang_dir = None

        self._connected = False

    def __enter__(self) -> Device:
        self.connect()
        return self

    def __exit__(self, *args) -> bool:
        self.close()
        return False

    def _require_connected(self) -> None:
        if not self._connected or self._pool is None:
            raise ConnectionError("device is not connected; call connect() first")

    # ------------------------------------------------------------------
    # Direct read (trigger)
    # ------------------------------------------------------------------

    def get(self, path: str) -> List[Dict]:
        """Read data from the device via a direct NETCONF get.

        Accepts canonical, alias, or keyed paths.  Returns a list of
        update dicts: ``[{"path": ..., "value": ..., "timestamp_ns": ...}, ...]``
        """
        self._require_connected()
        from splice.netconf.trigger_executor import (
            NetconfTriggerExecutor,
            TriggerValidationError,
        )

        list_keys_norm = {normalize_path(k): v for k, v in self._catalog.list_keys.items()}
        executor = NetconfTriggerExecutor(
            pool=self._pool,
            catalog_map=self._catalog.catalog_map,
            alias_map=self._catalog.alias_map,
            list_keys=self._catalog.list_keys,
            parse_reply=lambda xml: parse_xml_to_kv(
                xml, self._catalog.type_map, list_keys=list_keys_norm
            ),
        )
        try:
            result = executor.execute([path])
        except TriggerValidationError as exc:
            raise PathNotFoundError(str(exc)) from exc
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            raise DeviceError(f"get failed: {exc}") from exc

        if result.updates:
            self._cache.ingest(result.updates)
        if result.errors:
            log.warning("get errors: %s", result.errors)
        return result.updates

    # ------------------------------------------------------------------
    # Write config (set)
    # ------------------------------------------------------------------

    def set(
        self,
        path: str,
        value: Any,
        operation: str = "merge",
        datastore: str = "auto",
        use_lock: bool = False,
    ) -> Dict:
        """Write a configuration value via NETCONF edit-config.

        Returns a dict with ``path`` and ``operation`` on success.
        """
        self._require_connected()
        from splice.expander import strip_predicates
        from splice.netconf.set_executor import (
            NetconfSetExecutor,
            SetPreconditionError,
        )
        from splice.netconf.set_request import (
            SetValidationError,
            SetUnsupportedError,
        )

        # Validate path is writable config
        base = strip_predicates(path)
        norm = normalize_path(base)
        entry = None
        for cat_path, cat_entry in self._catalog.catalog_map.items():
            if normalize_path(cat_path) == norm or cat_path == base:
                entry = cat_entry
                break
        # Also check via alias map
        if entry is None:
            canonical = self._catalog.alias_map.get(base)
            if canonical:
                entry = self._catalog.catalog_map.get(canonical)

        if entry is not None and not entry.get("config", False):
            raise PathNotWritableError(f"path is read-only (config=false): {path}")

        # Build SetRequest
        path_elem = _path_from_str(path)
        typed_val = _typed_value(value)

        VALID_OPS = ("merge", "replace", "delete")
        op = operation.lower()
        if op not in VALID_OPS:
            raise SetError(
                f"unsupported operation: {operation!r} (expected one of {VALID_OPS})"
            )

        request = gnmi_pb2.SetRequest()
        if op == "delete":
            request.delete.append(path_elem)
        elif op == "replace":
            request.replace.append(
                gnmi_pb2.Update(path=path_elem, val=typed_val)
            )
        else:
            request.update.append(
                gnmi_pb2.Update(path=path_elem, val=typed_val)
            )

        executor = NetconfSetExecutor(
            pool=self._pool,
            catalog_map=self._catalog.catalog_map,
            alias_map=self._catalog.alias_map,
            list_keys=self._catalog.list_keys,
            datastore_preference=datastore,
            use_lock=use_lock,
        )
        try:
            response = executor.execute(request)
        except (SetValidationError, SetUnsupportedError) as exc:
            raise SetError(str(exc)) from exc
        except SetPreconditionError as exc:
            raise SetError(str(exc)) from exc
        except Exception as exc:
            raise SetError(f"set failed: {exc}") from exc

        return {"path": path, "operation": operation, "timestamp_ns": response.timestamp}

    # ------------------------------------------------------------------
    # Subscribe (polling)
    # ------------------------------------------------------------------

    def subscribe(
        self,
        *paths: str,
        interval: int = 30,
        batch: bool = True,
    ) -> Subscription:
        """Subscribe to periodic polling updates for the given paths.

        Returns a ``Subscription`` iterator.  Each iteration yields the
        full snapshot of updates received in the latest poll cycle.
        """
        self._require_connected()
        from splice.planner import plan_batched_queries, plan_queries

        # Build a catalog subset for the requested paths
        sub_catalog: Dict[str, Dict] = {}
        sub_intervals: Dict[str, int] = {}
        for p in paths:
            norm = normalize_path(p)
            for cat_path, cat_entry in self._catalog.catalog_map.items():
                if is_path_prefix(normalize_path(cat_path), norm):
                    sub_catalog[cat_path] = cat_entry
                    sub_intervals[cat_path] = interval
            # Also check aliases
            canonical = self._catalog.alias_map.get(p)
            if canonical and canonical in self._catalog.catalog_map:
                sub_catalog[canonical] = self._catalog.catalog_map[canonical]
                sub_intervals[canonical] = interval

        if not sub_catalog:
            raise SubscriptionError(f"no catalog paths match: {paths}")

        planner = plan_batched_queries if batch else plan_queries
        tasks = planner(sub_catalog, sub_intervals, default_interval_sec=interval)

        if not tasks:
            raise SubscriptionError(f"no polling tasks generated for: {paths}")

        def fetch(filter_xml: str) -> str:
            reply = self._pool.get(filter_xml)
            return reply.data_xml

        list_keys_norm = {normalize_path(k): v for k, v in self._catalog.list_keys.items()}

        def parse(xml: str) -> List[Dict]:
            return parse_xml_to_kv(xml, self._catalog.type_map, list_keys=list_keys_norm)

        sub = Subscription(
            fetch=fetch,
            parse=parse,
            tasks=tasks,
            shared_cache=self._cache,
        )
        self._subscriptions.append(sub)
        return sub

    # ------------------------------------------------------------------
    # gRPC server
    # ------------------------------------------------------------------

    def serve(
        self,
        port: int = 9339,
        bind: str = "0.0.0.0",
        heartbeat_sec: int = 10,
        set_enabled: bool = True,
        blocking: bool = False,
    ) -> None:
        """Start a gRPC gNMI server backed by this device's cache.

        If ``blocking=True`` the call blocks until the server is stopped.
        """
        self._require_connected()
        from splice.gnmi_server import serve as gnmi_serve

        # Build a live CacheView backed by our MemoryCache
        cache_view = _LiveCacheView(self._cache, self._catalog.alias_map)

        # Trigger handler for direct reads through gRPC
        def trigger_handler(paths, **kwargs):
            all_updates: List[Dict] = []
            for p in paths:
                all_updates.extend(self.get(p))
            return all_updates

        # Set handler
        set_handler = None
        if set_enabled:
            from splice.netconf.set_executor import NetconfSetExecutor

            _set_exec = NetconfSetExecutor(
                pool=self._pool,
                catalog_map=self._catalog.catalog_map,
                alias_map=self._catalog.alias_map,
                list_keys=self._catalog.list_keys,
            )
            set_handler = _set_exec.execute

        # Build supported models list
        models = []
        try:
            for m in self._catalog.supported_models():
                models.append(
                    gnmi_pb2.ModelData(
                        name=m["name"],
                        organization=m.get("organization", ""),
                        version=m.get("version", ""),
                    )
                )
        except Exception:
            pass

        self._grpc_server = gnmi_serve(
            cache_view=cache_view,
            bind=bind,
            port=port,
            heartbeat_sec=heartbeat_sec,
            supported_models=models or None,
            list_keys=self._catalog.list_keys,
            set_handler=set_handler,
            trigger_handler=trigger_handler,
        )
        log.info("gNMI server listening on %s:%d", bind, port)

        if blocking:
            self._grpc_server.wait_for_termination()

    # ------------------------------------------------------------------
    # Informational
    # ------------------------------------------------------------------

    @property
    def catalog_paths(self) -> List[str]:
        return self._catalog.canonical_paths()

    def find_paths(self, pattern: str) -> List[str]:
        """Return catalog paths matching a glob pattern."""
        return [
            p for p in self._catalog.canonical_paths()
            if fnmatch.fnmatch(normalize_path(p), pattern)
            or fnmatch.fnmatch(p, pattern)
        ]

    @property
    def connection_status(self) -> Dict:
        if self._pool is None:
            return {"connected": False}
        status = self._pool.get_status()
        status["connected"] = self._connected
        return status

    def search_paths(self, query: str, top_k: int = 10) -> List[tuple]:
        """Search catalog paths using natural language semantic search.

        Returns list of (path, score) tuples ranked by relevance.
        Ranking is hybrid: BM25 keyword match (always available) blended
        with embedding similarity when sentence-transformers or the
        openai package is installed.

        Example::

            results = device.search_paths("Q-factor optical quality")
            for path, score in results:
                print(f"{score:.3f}  {path}")
        """
        from splice.path_index import PathIndex, build_path_entries

        # Build and cache the index on first call
        if not hasattr(self, "_path_index") or self._path_index is None:
            entries = build_path_entries(self._catalog)
            if not entries:
                return []
            self._path_index = PathIndex(entries)
            self._path_index.build()

        results = self._path_index.search(query, top_k=top_k)
        return [(entry.path, score) for entry, score in results]

    def doctor(self) -> Dict:
        """Run diagnostic checks on the device connection.

        Returns dict with:
        - connected: bool
        - circuit_breaker: dict (state, failures, cooldown)
        - catalog_paths: int (number of discovered paths)
        - yang_modules: int
        - capabilities: list[str]
        """
        result: Dict[str, Any] = {
            "connected": self._connected,
            "circuit_breaker": {
                "state": "closed",
                "failures": 0,
                "cooldown": 0.0,
            },
            "catalog_paths": len(self._catalog.catalog_map),
            "yang_modules": len(self._catalog.modules),
            "capabilities": [],
        }

        if self._pool is not None:
            status = self._pool.get_status()
            cb_open = status.get("circuit_breaker_open", False)
            result["circuit_breaker"] = {
                "state": "open" if cb_open else "closed",
                "failures": status.get("consecutive_failures", 0),
                "cooldown": status.get("circuit_breaker_seconds_remaining", 0.0),
            }
            try:
                result["capabilities"] = self._pool.get_server_capabilities()
            except Exception:
                pass

        return result

    def export_catalog(self, format: str = "json") -> Any:
        """Export the device catalog.

        Args:
            format: "json" returns JSON string, "dict" returns list of dicts
        """
        import json

        entries: List[Dict] = []
        for path, info in self._catalog.catalog_map.items():
            entry: Dict[str, Any] = {"path": path}
            entry.update(info)
            # Add description from description_map if not already present
            if "description" not in entry or not entry.get("description"):
                desc = self._catalog.description_map.get(path)
                if desc:
                    entry["description"] = desc
            entries.append(entry)

        if format == "dict":
            return entries

        return json.dumps(entries, indent=2, default=str)

    def cached_data(self) -> List[Dict]:
        return self._cache.snapshot()


class _LiveCacheView:
    """CacheView-compatible adapter backed by a live MemoryCache."""

    def __init__(self, cache: MemoryCache, alias_map: Dict[str, str]) -> None:
        self._cache = cache
        self._alias_map = alias_map

    def match_prefix(self, prefix: str) -> List[Dict]:
        canonical = self._alias_map.get(prefix, prefix)
        return self._cache.match_prefix(canonical)

    def all_updates(self) -> List[Dict]:
        return self._cache.all_updates()

    def get_version(self) -> int:
        return self._cache.get_version()

    def wait_for_update(self, last_version: int, timeout: float) -> int:
        return self._cache.wait_for_update(last_version, timeout)
