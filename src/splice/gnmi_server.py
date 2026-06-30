# Copyright (c) 2026 Trinity College Dublin
#
# This file is part of Splice and is dual-licensed:
#   - under the GNU Affero General Public License v3.0 or later (see LICENSE), or
#   - under a commercial licence (contact infoknex@tcd.ie).
#
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import time
from typing import Dict, Iterable, List, Optional

import grpc
import splice.gnmi_pb2 as gnmi_pb2
import splice.gnmi_pb2_grpc as gnmi_pb2_grpc
from splice.pathing import decode_key_value, encode_key_value, is_path_prefix
from splice.netconf.set_request import SetValidationError, SetUnsupportedError
from splice.netconf.set_executor import SetPreconditionError
from splice.netconf.trigger_executor import TriggerValidationError


TRIGGER_EXTENSION_ID = 61001


def _parse_predicates(seg: str) -> tuple[str, Dict[str, str]]:
    if "[" not in seg:
        return seg, {}
    name, rest = seg.split("[", 1)
    kv = {}
    for block in rest.split("]"):
        # Tolerate multi-bracket form "[a=1][b=2]": blocks after the first
        # keep a leading "[" once the segment is split on "]".
        block = block.strip().lstrip("[")
        if not block:
            continue
        for pair in block.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                k = k.strip()
                if not k:
                    continue  # malformed predicate like '[=v]' — ignore
                kv[k] = decode_key_value(v.strip())
    return name, kv


def _path_from_str(path_str: str) -> "gnmi_pb2.Path":
    if path_str.startswith("/"):
        path_str = path_str[1:]
    elems = [p for p in path_str.split("/") if p]
    out = []
    for e in elems:
        name, keys = _parse_predicates(e)
        out.append(gnmi_pb2.PathElem(name=name, key=keys))
    return gnmi_pb2.Path(elem=out)


def _typed_value(val) -> "gnmi_pb2.TypedValue":
    tv = gnmi_pb2.TypedValue()
    if isinstance(val, bool):
        tv.bool_val = val
    elif isinstance(val, int):
        tv.int_val = val
    elif isinstance(val, float):
        tv.float_val = float(val)
    elif isinstance(val, (bytes, bytearray)):
        tv.bytes_val = bytes(val)
    else:
        tv.string_val = str(val)
    return tv


def _path_to_str(path: "gnmi_pb2.Path", list_keys: Optional[Dict[str, List[str]]] = None) -> str:
    elems = getattr(path, "elem", []) or []
    parts = []
    cur_parts: List[str] = []
    for e in elems:
        name = e.name
        cur_parts.append(name)
        if getattr(e, "key", None):
            items = list(e.key.items())
            if list_keys:
                key_order = list_keys.get("/" + "/".join(cur_parts))
                if key_order:
                    order_index = {k: i for i, k in enumerate(key_order)}
                    items.sort(key=lambda kv: (order_index.get(kv[0], len(order_index)), kv[0]))
                else:
                    items.sort(key=lambda kv: kv[0])
            else:
                items.sort(key=lambda kv: kv[0])
            kv = ",".join(
                [f"{k}={encode_key_value(decode_key_value(str(v)))}" for k, v in items]
            )
            parts.append(f"{name}[{kv}]")
        else:
            parts.append(name)
    return "/" + "/".join(parts)


class CacheView:
    def __init__(self, canonical_map: Dict[str, Dict[str, object]], alias_map: Dict[str, str]):
        self._canonical_map = canonical_map
        self._alias_map = alias_map

    def _normalize(self, path: str) -> str:
        from splice.pathing import normalize_path
        return normalize_path(path)

    def match_prefix(self, prefix: str) -> List[Dict[str, object]]:
        canonical_prefix = self._alias_map.get(prefix, prefix)
        out = []
        pref_norm = self._normalize(canonical_prefix)
        for p, rec in self._canonical_map.items():
            if is_path_prefix(self._normalize(p), pref_norm):
                out.append(rec)
        return out

    def all_updates(self) -> List[Dict[str, object]]:
        return list(self._canonical_map.values())

    def get_version(self) -> int:
        return 0

    def wait_for_update(self, last_version: int, timeout: float) -> int:
        time.sleep(timeout)
        return last_version


class GNMIServicer(gnmi_pb2_grpc.gNMIServicer):
    def __init__(
        self,
        cache_view: CacheView,
        supported_models: Optional[List[gnmi_pb2.ModelData]] = None,
        heartbeat_sec: int = 10,
        list_keys: Optional[Dict[str, List[str]]] = None,
        set_handler=None,
        trigger_handler=None,
    ) -> None:
        self.cache = cache_view
        self.supported_models = supported_models or []
        self.heartbeat_sec = heartbeat_sec
        self.list_keys = list_keys
        self.set_handler = set_handler
        self.trigger_handler = trigger_handler

    def Capabilities(self, request, context):  # type: ignore
        return gnmi_pb2.CapabilityResponse(
            supported_models=self.supported_models,
            supported_encodings=[gnmi_pb2.JSON_IETF, gnmi_pb2.PROTO],
            gNMI_version="0.8.0",
        )

    def Get(self, request, context):  # type: ignore
        try:
            trigger_opts = self._extract_trigger_options(request)
        except TriggerValidationError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return gnmi_pb2.GetResponse()
        prefixes: List[str] = []
        req_prefix = request.prefix if hasattr(request, "prefix") else None
        for p in getattr(request, "path", []) or [None]:
            elems: List[gnmi_pb2.PathElem] = []
            if req_prefix and getattr(req_prefix, "elem", None):
                elems.extend(req_prefix.elem)
            if p and getattr(p, "elem", None):
                elems.extend(p.elem)
            if elems:
                prefixes.append(_path_to_str(gnmi_pb2.Path(elem=elems), self.list_keys))
        if not prefixes:
            prefixes = ["/"]

        if trigger_opts is not None:
            if self.trigger_handler is None:
                context.abort(grpc.StatusCode.UNIMPLEMENTED, "Trigger is disabled")
                return gnmi_pb2.GetResponse()
            try:
                result = self.trigger_handler(
                    paths=prefixes,
                    publish_kafka=trigger_opts.get("publish_kafka"),
                    auto_onboard=trigger_opts.get("auto_onboard"),
                    interval_sec=trigger_opts.get("interval_sec"),
                    source="gnmi",
                )
            except TriggerValidationError as exc:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
                return gnmi_pb2.GetResponse()
            except Exception:
                context.abort(grpc.StatusCode.INTERNAL, "Trigger failed")
                return gnmi_pb2.GetResponse()

            updates = []
            if isinstance(result, dict):
                updates = list(result.get("updates", []))
            elif isinstance(result, list):
                updates = result

            notif = gnmi_pb2.Notification(timestamp=int(time.time() * 1_000_000_000))
            for rec in updates:
                notif.update.append(
                    gnmi_pb2.Update(
                        path=_path_from_str(str(rec.get("path", "/"))),
                        val=_typed_value(rec.get("value")),
                    )
                )
            return gnmi_pb2.GetResponse(notification=[notif])

        notif = gnmi_pb2.Notification(timestamp=int(time.time() * 1_000_000_000))
        for pref in prefixes:
            for r in self.cache.match_prefix(pref):
                notif.update.append(
                    gnmi_pb2.Update(
                        path=_path_from_str(r["path"]),
                        val=_typed_value(r.get("value")),
                    )
                )
        return gnmi_pb2.GetResponse(notification=[notif])

    def _extract_trigger_options(self, request) -> Optional[dict]:
        exts = getattr(request, "extension", None) or []
        for ext in exts:
            try:
                if not ext.HasField("registered_ext"):
                    continue
            except Exception:
                continue

            reg = ext.registered_ext
            if int(getattr(reg, "id", 0)) != TRIGGER_EXTENSION_ID:
                continue

            raw = bytes(getattr(reg, "msg", b"") or b"")
            if not raw:
                raise TriggerValidationError("trigger extension payload is required")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise TriggerValidationError(
                    "trigger extension payload must be valid utf-8 json"
                ) from exc

            if not isinstance(payload, dict):
                raise TriggerValidationError("trigger extension payload must be an object")
            mode = str(payload.get("mode", "")).strip().upper()
            if mode != "TRIGGER":
                raise TriggerValidationError("trigger extension mode must be TRIGGER")

            out = {}
            if "publish_kafka" in payload:
                if not isinstance(payload["publish_kafka"], bool):
                    raise TriggerValidationError("publish_kafka must be boolean")
                out["publish_kafka"] = payload["publish_kafka"]
            if "auto_onboard" in payload:
                if not isinstance(payload["auto_onboard"], bool):
                    raise TriggerValidationError("auto_onboard must be boolean")
                out["auto_onboard"] = payload["auto_onboard"]
            if "interval_sec" in payload:
                try:
                    interval = int(payload["interval_sec"])
                except Exception as exc:
                    raise TriggerValidationError("interval_sec must be integer") from exc
                if interval <= 0:
                    raise TriggerValidationError("interval_sec must be > 0")
                out["interval_sec"] = interval
            return out
        return None

    def Set(self, request, context):  # type: ignore
        if self.set_handler is None:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "Set is disabled")
            return gnmi_pb2.SetResponse()
        try:
            return self.set_handler(request)
        except SetValidationError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            return gnmi_pb2.SetResponse()
        except SetUnsupportedError as exc:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, str(exc))
            return gnmi_pb2.SetResponse()
        except SetPreconditionError as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            return gnmi_pb2.SetResponse()
        except Exception:
            context.abort(grpc.StatusCode.INTERNAL, "Set failed")
            return gnmi_pb2.SetResponse()

    def Subscribe(self, request_iterator, context):  # type: ignore
        try:
            first = next(request_iterator)
        except StopIteration:
            return
        sub_list = first.subscribe
        mode = sub_list.mode

        prefixes: List[str] = []
        req_prefix = getattr(sub_list, "prefix", None)
        subs = list(getattr(sub_list, "subscription", [])) or [None]
        for sub in subs:
            elems: List[gnmi_pb2.PathElem] = []
            if req_prefix and getattr(req_prefix, "elem", None):
                elems.extend(req_prefix.elem)
            if sub and getattr(sub, "path", None) and getattr(sub.path, "elem", None):
                elems.extend(sub.path.elem)
            if elems:
                prefixes.append(_path_to_str(gnmi_pb2.Path(elem=elems), self.list_keys))
        if not prefixes:
            prefixes = ["/"]

        def collect_updates():
            recs = []
            for pref in prefixes:
                recs.extend(self.cache.match_prefix(pref))
            return recs

        def make_update_notif():
            notif = gnmi_pb2.Notification(timestamp=int(time.time() * 1_000_000_000))
            for rec in collect_updates():
                notif.update.append(
                    gnmi_pb2.Update(
                        path=_path_from_str(rec["path"]),
                        val=_typed_value(rec.get("value")),
                    )
                )
            return gnmi_pb2.SubscribeResponse(update=notif)

        if mode == gnmi_pb2.SubscriptionList.ONCE:
            yield make_update_notif()
            yield gnmi_pb2.SubscribeResponse(sync_response=True)
            return

        if mode == gnmi_pb2.SubscriptionList.POLL:
            # Initial data + sync per gNMI spec
            yield make_update_notif()
            yield gnmi_pb2.SubscribeResponse(sync_response=True)
            # Wait for poll requests from the client
            for poll_req in request_iterator:
                if not context.is_active():
                    break
                if poll_req.HasField("poll"):
                    yield make_update_notif()
            return

        stream_mode = gnmi_pb2.SubscriptionMode.SAMPLE
        for sub in sub_list.subscription:
            if sub.mode in (
                gnmi_pb2.SubscriptionMode.SAMPLE,
                gnmi_pb2.SubscriptionMode.ON_CHANGE,
                gnmi_pb2.SubscriptionMode.TARGET_DEFINED,
            ):
                stream_mode = sub.mode
                break

        sample_interval_ns = 5_000_000_000
        for sub in sub_list.subscription:
            if getattr(sub, "sample_interval", 0):
                sample_interval_ns = sub.sample_interval
                break

        # Initial data snapshot + sync_response per gNMI spec
        yield make_update_notif()
        yield gnmi_pb2.SubscribeResponse(sync_response=True)

        next_emit = time.time() + (sample_interval_ns / 1e9)
        # Anchor the heartbeat to now; last_hb=0.0 would make the first
        # iteration fire an immediate empty heartbeat right after sync.
        last_hb = time.time()
        last_version = self.cache.get_version()

        start_time = time.time()
        max_runtime_sec = getattr(self, "max_runtime_sec", None)
        while True:
            if not context.is_active():
                break
            if max_runtime_sec is not None and (time.time() - start_time) > max_runtime_sec:
                break
            now = time.time()
            if stream_mode == gnmi_pb2.SubscriptionMode.SAMPLE and now >= next_emit:
                yield make_update_notif()
                next_emit = now + (sample_interval_ns / 1e9)
            elif stream_mode == gnmi_pb2.SubscriptionMode.ON_CHANGE:
                new_version = self.cache.wait_for_update(last_version, timeout=0.2)
                if new_version != last_version:
                    yield make_update_notif()
                    last_version = new_version

            if now - last_hb > self.heartbeat_sec:
                yield gnmi_pb2.SubscribeResponse(
                    update=gnmi_pb2.Notification(timestamp=int(time.time() * 1_000_000_000))
                )
                last_hb = now

            if stream_mode != gnmi_pb2.SubscriptionMode.ON_CHANGE:
                time.sleep(0.2)


def serve(
    cache_view: CacheView,
    bind: str = "0.0.0.0",
    port: int = 9339,
    heartbeat_sec: int = 10,
    supported_models: Optional[List[gnmi_pb2.ModelData]] = None,
    max_runtime_sec: Optional[int] = None,
    list_keys: Optional[Dict[str, List[str]]] = None,
    set_handler=None,
    trigger_handler=None,
):
    import grpc
    from concurrent import futures

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    svc = GNMIServicer(
        cache_view=cache_view,
        heartbeat_sec=heartbeat_sec,
        supported_models=supported_models,
        list_keys=list_keys,
        set_handler=set_handler,
        trigger_handler=trigger_handler,
    )
    if max_runtime_sec is not None:
        setattr(svc, "max_runtime_sec", int(max_runtime_sec))
    gnmi_pb2_grpc.add_gNMIServicer_to_server(svc, server)
    bound = server.add_insecure_port(f"{bind}:{port}")
    if bound == 0:
        # grpc returns 0 instead of raising when the port can't be bound
        # (already in use, permission denied). Without this the server would
        # "start" while listening on nothing and the CLI would print "ready".
        raise OSError(f"failed to bind gNMI server to {bind}:{port} (port in use?)")
    server.start()
    return server
