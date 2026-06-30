# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Optional

import yaml


@dataclass
class DeviceConfig:
    host: str
    port: int = 830
    username: str = "netconf"
    password: Optional[str] = None
    password_file: Optional[str] = None
    name: Optional[str] = None


@dataclass
class DBConfig:
    url: str


@dataclass
class GNMIConfig:
    bind: str = "0.0.0.0"
    port: int = 9339
    heartbeat_sec: int = 10
    max_runtime_sec: int | None = None
    expansion_mode: str = "direct"  # full|direct|lazy
    max_expanded_paths_per_base: int = 10000
    max_total_expanded_paths: int = 100000
    set_enabled: bool = False
    set_datastore: str = "auto"  # auto|running|candidate
    set_auto_refresh: bool = False
    set_use_lock: bool = False


@dataclass
class TriggerConfig:
    enabled: bool = False
    auto_onboard: bool = True
    default_interval_sec: int = 30
    max_paths_per_request: int = 50
    netconf_timeout_sec: float = 10.0


@dataclass
class TriggerAPIConfig:
    enabled: bool = False
    bind: str = "0.0.0.0"
    port: int = 9812
    bearer_token: Optional[str] = None


@dataclass
class KafkaConfig:
    enabled: bool = False
    brokers: str = ""
    client_id: str = "splice"
    topic_prefix: str = "splice"
    publish_poll_updates: bool = True
    publish_trigger_events: bool = True
    acks: str = "all"
    compression: Optional[str] = "lz4"
    linger_ms: int = 20


@dataclass
class HealthConfig:
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 9809


@dataclass
class NotificationsConfig:
    enabled: bool = False
    poll_sec: int = 1
    paths: list | None = None


@dataclass
class CacheConfig:
    ttl_sec: int = 60
    warm_start: bool = True


@dataclass
class HistoryConfig:
    enabled: bool = False
    storage: str = "db"  # db|file
    file_path: str | None = None


@dataclass
class PollerConfig:
    default_interval_sec: int = 30
    batch_enabled: bool = True
    rules: list | None = None
    batch_mode: str = "top"  # top|module
    preserve_namespaces: bool = False
    list_refresh_sec: int = 0
    task_throttle_ms: int = 50
    # Drop a task from the poll rotation after N consecutive failures
    # (0 = never drop). Circuit-breaker pauses do not count.
    task_failure_limit: int = 5
    # Resilience / lockout-safety settings
    reconnect_delay_sec: float = 2.0
    circuit_breaker_threshold: int = 5
    circuit_breaker_cooldown_sec: float = 60.0
    auth_failure_cooldown_sec: float = 300.0  # Long cooldown for auth failures


@dataclass
class DiscoveryConfig:
    inspect_components: bool = False
    component_limit: int = 20
    component_pattern: Optional[str] = None
    resolve_all_lists: bool = False
    list_batch_size: int = 50
    list_sleep_sec: float = 0.5
    resume_batch: int = 1
    heartbeat_sec: float = 0.0
    pm_discovery: bool = False
    pm_max_paths: int = 25
    pm_fanout: bool = False
    pm_fanout_limit: int = 0
    pm_selector_list_path: Optional[str] = None
    pm_selector: Optional[str] = None
    # Re-resolve fanout list instances at `run` start and rebuild PM filters
    # when the device's instances drifted (e.g. re-provisioned line ports).
    pm_refresh_on_run: bool = True
    pm_adapter_modules: list | None = None
    discovery_debug: bool = False
    debug_output_dir: str = "./debug_netconf"


@dataclass
class PMConfig:
    enabled: bool = False
    interval_sec: int = 30
    live_token: str = "interval-indefinite"
    path_prefix: str = "/pm"


@dataclass
class MetricsConfig:
    """Curated polling: when enabled with include patterns, only catalog
    paths matching a pattern get polling jobs (instead of the whole catalog).
    PM paths (/pm/...) come from the PM poller and are unaffected."""

    enabled: bool = False
    # Items: {"pattern": str, "regex": bool = False, "interval_sec": int}
    # or bare strings (substring match). Matched case-insensitively against
    # both module-qualified and prefix-stripped path forms.
    include: list | None = None
    default_interval_sec: int = 10
    # Disable existing jobs that match no pattern (kept, not deleted).
    disable_uncurated: bool = False


@dataclass
class AppConfig:
    device: DeviceConfig
    db: DBConfig
    yang_dir: str = "./yang"
    gnmi: GNMIConfig = field(default_factory=GNMIConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    trigger_api: TriggerAPIConfig = field(default_factory=TriggerAPIConfig)
    kafka: KafkaConfig = field(default_factory=KafkaConfig)
    poller: PollerConfig = field(default_factory=PollerConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    pm: PMConfig = field(default_factory=PMConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    health: HealthConfig = field(default_factory=HealthConfig)


def load_config(path: str) -> AppConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}

    dev = raw.get("device", {})
    password_file = dev.get("password_file")
    password = dev.get("password") or os.environ.get("NETCONF_PASSWORD")
    if password_file:
        try:
            with open(os.path.expanduser(password_file), "r", encoding="utf-8") as f:
                password = f.read().strip()
        except FileNotFoundError as e:
            raise FileNotFoundError(f"password_file not found: {password_file}") from e
    db = raw.get("db", {})
    gnmi = raw.get("gnmi", {})
    trigger = raw.get("trigger", {})
    trigger_api = raw.get("trigger_api", {})
    kafka = raw.get("kafka", {})
    poller = raw.get("poller", {})
    discovery = raw.get("discovery", {})
    pm = raw.get("pm", {})
    metrics = raw.get("metrics", {})
    notifications = raw.get("notifications", {})
    cache = raw.get("cache", {})
    history = raw.get("history", {})
    health = raw.get("health", {})

    cfg = AppConfig(
        device=DeviceConfig(
            host=str(dev.get("host")),
            port=int(dev.get("port", 830)),
            username=str(dev.get("username", "netconf")),
            password=password,
            password_file=password_file,
            name=dev.get("name"),
        ),
        db=DBConfig(url=str(db.get("url"))),
        yang_dir=str(raw.get("yang_dir", "./yang")),
        gnmi=GNMIConfig(
            bind=str(gnmi.get("bind", "0.0.0.0")),
            port=int(gnmi.get("port", 9339)),
            heartbeat_sec=int(gnmi.get("heartbeat_sec", 10)),
            max_runtime_sec=gnmi.get("max_runtime_sec"),
            expansion_mode=str(gnmi.get("expansion_mode", "direct")),
            max_expanded_paths_per_base=int(gnmi.get("max_expanded_paths_per_base", 10000)),
            max_total_expanded_paths=int(gnmi.get("max_total_expanded_paths", 100000)),
            set_enabled=bool(gnmi.get("set_enabled", False)),
            set_datastore=str(gnmi.get("set_datastore", "auto")),
            set_auto_refresh=bool(gnmi.get("set_auto_refresh", False)),
            set_use_lock=bool(gnmi.get("set_use_lock", False)),
        ),
        trigger=TriggerConfig(
            enabled=bool(trigger.get("enabled", False)),
            auto_onboard=bool(trigger.get("auto_onboard", True)),
            default_interval_sec=int(trigger.get("default_interval_sec", 30)),
            max_paths_per_request=int(trigger.get("max_paths_per_request", 50)),
            netconf_timeout_sec=float(trigger.get("netconf_timeout_sec", 10.0)),
        ),
        trigger_api=TriggerAPIConfig(
            enabled=bool(trigger_api.get("enabled", False)),
            bind=str(trigger_api.get("bind", "0.0.0.0")),
            port=int(trigger_api.get("port", 9812)),
            bearer_token=trigger_api.get("bearer_token"),
        ),
        kafka=KafkaConfig(
            enabled=bool(kafka.get("enabled", False)),
            brokers=str(kafka.get("brokers", "")),
            client_id=str(kafka.get("client_id", "splice")),
            topic_prefix=str(kafka.get("topic_prefix", "splice")),
            publish_poll_updates=bool(kafka.get("publish_poll_updates", True)),
            publish_trigger_events=bool(kafka.get("publish_trigger_events", True)),
            acks=str(kafka.get("acks", "all")),
            compression=kafka.get("compression", "lz4"),
            linger_ms=int(kafka.get("linger_ms", 20)),
        ),
        poller=PollerConfig(
            default_interval_sec=int(poller.get("default_interval_sec", 30)),
            batch_enabled=bool(poller.get("batch_enabled", True)),
            rules=poller.get("rules"),
            batch_mode=str(poller.get("batch_mode", "top")),
            preserve_namespaces=bool(poller.get("preserve_namespaces", False)),
            list_refresh_sec=int(poller.get("list_refresh_sec", 0)),
            task_throttle_ms=int(poller.get("task_throttle_ms", 50)),
            task_failure_limit=int(poller.get("task_failure_limit", 5)),
            reconnect_delay_sec=float(poller.get("reconnect_delay_sec", 2.0)),
            circuit_breaker_threshold=int(poller.get("circuit_breaker_threshold", 5)),
            circuit_breaker_cooldown_sec=float(poller.get("circuit_breaker_cooldown_sec", 60.0)),
            auth_failure_cooldown_sec=float(poller.get("auth_failure_cooldown_sec", 300.0)),
        ),
        discovery=DiscoveryConfig(
            inspect_components=bool(discovery.get("inspect_components", False)),
            component_limit=int(discovery.get("component_limit", 20)),
            component_pattern=discovery.get("component_pattern"),
            resolve_all_lists=bool(discovery.get("resolve_all_lists", False)),
            list_batch_size=int(discovery.get("list_batch_size", 50)),
            list_sleep_sec=float(discovery.get("list_sleep_sec", 0.5)),
            resume_batch=int(discovery.get("resume_batch", 1)),
            heartbeat_sec=float(discovery.get("heartbeat_sec", 0.0)),
            pm_discovery=bool(discovery.get("pm_discovery", False)),
            pm_max_paths=int(discovery.get("pm_max_paths", 25)),
            pm_fanout=bool(discovery.get("pm_fanout", False)),
            pm_fanout_limit=int(discovery.get("pm_fanout_limit", 0)),
            pm_selector_list_path=discovery.get("pm_selector_list_path"),
            pm_selector=discovery.get("pm_selector"),
            pm_refresh_on_run=bool(discovery.get("pm_refresh_on_run", True)),
            pm_adapter_modules=discovery.get("pm_adapter_modules"),
            discovery_debug=bool(discovery.get("discovery_debug", False)),
            debug_output_dir=str(discovery.get("debug_output_dir", "./debug_netconf")),
        ),
        pm=PMConfig(
            enabled=bool(pm.get("enabled", False)),
            interval_sec=int(pm.get("interval_sec", 30)),
            live_token=str(pm.get("live_token", "interval-indefinite")),
            path_prefix=str(pm.get("path_prefix", "/pm")),
        ),
        metrics=MetricsConfig(
            enabled=bool(metrics.get("enabled", False)),
            include=metrics.get("include"),
            default_interval_sec=int(metrics.get("default_interval_sec", 10)),
            disable_uncurated=bool(metrics.get("disable_uncurated", False)),
        ),
        notifications=NotificationsConfig(
            enabled=bool(notifications.get("enabled", False)),
            poll_sec=int(notifications.get("poll_sec", 1)),
            paths=notifications.get("paths"),
        ),
        cache=CacheConfig(
            ttl_sec=int(cache.get("ttl_sec", 60)),
            warm_start=bool(cache.get("warm_start", True)),
        ),
        history=HistoryConfig(
            enabled=bool(history.get("enabled", False)),
            storage=str(history.get("storage", "db")),
            file_path=history.get("file_path"),
        ),
        health=HealthConfig(
            enabled=bool(health.get("enabled", True)),
            bind=str(health.get("bind", "0.0.0.0")),
            port=int(health.get("port", 9809)),
        ),
    )
    if cfg.trigger_api.enabled and not cfg.trigger.enabled:
        raise ValueError("trigger_api.enabled=true requires trigger.enabled=true")
    if cfg.trigger_api.enabled and not cfg.trigger_api.bearer_token:
        raise ValueError("trigger_api.enabled=true requires trigger_api.bearer_token")
    if cfg.kafka.enabled and not cfg.kafka.brokers:
        raise ValueError("kafka.enabled=true requires kafka.brokers")
    return cfg
