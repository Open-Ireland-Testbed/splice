# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import logging
import re
import time
from typing import Iterable

log = logging.getLogger(__name__)


def _sanitize_token(raw: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw.strip())
    return s.strip("._-") or "default"


class KafkaPublisher:
    """Best-effort Kafka publisher for polling and trigger events."""

    def __init__(
        self,
        *,
        enabled: bool,
        brokers: str = "",
        client_id: str = "splice",
        topic_prefix: str = "splice",
        publish_poll_updates: bool = True,
        publish_trigger_events: bool = True,
        acks: str = "all",
        compression: str | None = "lz4",
        linger_ms: int = 20,
        producer=None,
    ) -> None:
        self.enabled = bool(enabled)
        self.publish_poll_updates_enabled = bool(publish_poll_updates)
        self.publish_trigger_events_enabled = bool(publish_trigger_events)
        self.topic_prefix = _sanitize_token(topic_prefix or "splice")
        self._producer = None
        if not self.enabled:
            return

        if producer is not None:
            self._producer = producer
            return

        try:
            from confluent_kafka import Producer
        except Exception as exc:
            raise RuntimeError(
                "kafka.enabled=true requires confluent-kafka package"
            ) from exc

        cfg = {
            "bootstrap.servers": brokers,
            "client.id": client_id,
            "acks": acks,
            "linger.ms": int(linger_ms),
        }
        if compression:
            cfg["compression.type"] = compression
        self._producer = Producer(cfg)

    def _topic(self, device_name: str, event_type: str) -> str:
        return f"{self.topic_prefix}.{_sanitize_token(device_name)}.{event_type}"

    def _publish(self, topic: str, payload: dict) -> bool:
        if not self.enabled or self._producer is None:
            return False
        try:
            self._producer.produce(topic=topic, value=json.dumps(payload))
            self._producer.poll(0)
            return True
        except Exception:
            log.exception("kafka produce failed topic=%s", topic)
            return False

    def publish_poll_updates(self, device_name: str, updates: Iterable[dict]) -> int:
        if not self.enabled or not self.publish_poll_updates_enabled:
            return 0
        topic = self._topic(device_name, "poll")
        sent = 0
        now_ns = time.time_ns()
        for u in updates:
            payload = {
                "event_type": "poll_update",
                "source": "poller",
                "device": device_name,
                "path": u.get("path"),
                "value": u.get("value"),
                "timestamp_ns": int(u.get("timestamp_ns", 0) or 0),
                "event_time_unix_ns": now_ns,
                "meta": u.get("meta"),
            }
            if self._publish(topic, payload):
                sent += 1
        return sent

    def publish_trigger_updates(
        self, device_name: str, request_id: str, updates: Iterable[dict]
    ) -> int:
        if not self.enabled or not self.publish_trigger_events_enabled:
            return 0
        topic = self._topic(device_name, "trigger")
        sent = 0
        now_ns = time.time_ns()
        for u in updates:
            payload = {
                "event_type": "trigger_update",
                "source": "trigger",
                "device": device_name,
                "request_id": request_id,
                "path": u.get("path"),
                "value": u.get("value"),
                "timestamp_ns": int(u.get("timestamp_ns", 0) or 0),
                "event_time_unix_ns": now_ns,
                "meta": u.get("meta"),
            }
            if self._publish(topic, payload):
                sent += 1
        return sent

    def close(self, timeout_sec: float = 2.0) -> None:
        if not self.enabled or self._producer is None:
            return
        try:
            self._producer.flush(timeout=timeout_sec)
        except Exception:
            log.exception("kafka flush failed")
