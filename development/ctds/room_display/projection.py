"""Read-only RoomDisplay projection from the store (whitelisted fields only).

Only observations whose provenance matches the configured source mode are shown,
so synthetic history can never appear on a LIVE screen.
"""
import datetime as dt

from . import adapter, catalog

CONNECTIVITY = {
    "OK": "CONNECTED", "TIMEOUT": "DISCONNECTED", "NO_CONNECTION": "DISCONNECTED",
    "AUTH_REJECTED": "AUTH_PAUSED", "NOT_FOUND": "ERROR", "HTTP_ERROR": "ERROR", "RATE_LIMITED": "ERROR",
    "REDIRECT_REFUSED": "ERROR", "TOO_LARGE": "ERROR", "INVALID_PAYLOAD": "ERROR",
}
ERROR_ZH = {
    "TIMEOUT": "设备响应超时", "NO_CONNECTION": "无法连接设备", "AUTH_REJECTED": "设备拒绝访问，已暂停读取",
    "NOT_FOUND": "设备接口不存在（固件或地址需核对）", "HTTP_ERROR": "设备返回错误", "RATE_LIMITED": "请求过于频繁",
    "REDIRECT_REFUSED": "设备返回重定向，已拒绝", "TOO_LARGE": "设备响应过大，已拒绝", "INVALID_PAYLOAD": "设备数据格式无效",
}
STATUS_ZH = {"CURRENT": "实时", "STALE": "已过期", "NO_DATA": "暂无数据", "NOT_PROVIDED": "设备未提供",
             "EXPLICIT_NULL": "设备返回空值", "INVALID": "数值无效"}
TREND_WINDOW = dt.timedelta(hours=6)
TREND_POINTS = 120


def _parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def _age(now, s):
    return None if s is None else max(0, int((now - _parse(s)).total_seconds()))


def _downsample(points, n=TREND_POINTS):
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n - 1)] + [points[-1]]


def _metric_view(store, room_id, setting, provenance, now, stale_after):
    mid = setting["metric_id"]
    m = catalog.METRICS[mid]
    latest = store.latest_observation(room_id, mid, provenance=provenance)
    good = store.latest_observation(room_id, mid, observed_only=True, provenance=provenance)
    view = {"metric_id": mid, "label_zh": setting["label_zh"], "unit": m["unit"],
            "value": None, "observed_or_received_at": None, "time_basis": None, "age_s": None,
            "status": "NO_DATA", "latest_value_status": latest["value_status"] if latest else None}
    if good:
        age = _age(now, good["gateway_received_at"])
        view.update(value=good["value"], observed_or_received_at=good["gateway_received_at"],
                    time_basis=good["time_basis"], age_s=age)
    if latest and latest["value_status"] != "OBSERVED":
        view["status"] = latest["value_status"]  # last good (if any) still shown with its own time
    elif good:
        view["status"] = "CURRENT" if view["age_s"] <= stale_after else "STALE"
    view["status_zh"] = STATUS_ZH[view["status"]]
    since = adapter.iso(now - TREND_WINDOW)
    pts = store.series(room_id, mid, since, provenance=provenance)
    if not pts and good:
        since_good = adapter.iso(_parse(good["gateway_received_at"]) - TREND_WINDOW)
        pts = store.series(room_id, mid, since_good, provenance=provenance)
    view["trend"] = _downsample([[p["t"], p["value"]] for p in pts])
    return view


def build_display(store, cfg, now=None):
    now = now or adapter.utc_now()
    room_id, src_id = cfg["room_id"], cfg["source"]["source_id"]
    provenance = "LIVE" if cfg["source"]["mode"] == "LIVE" else "SYNTHETIC"
    stale_after = cfg["stale_after_s"]
    source = store.source(src_id)
    last = store.latest_attempt(src_id, provenance=provenance)
    ok = store.last_success(src_id, provenance=provenance)
    if source and source["status"] == "AUTH_PAUSED":
        connectivity = "AUTH_PAUSED"
    elif last is None:
        connectivity = "NEVER_CONNECTED"
    else:
        connectivity = CONNECTIVITY.get(last["outcome"], "ERROR")
    ok_age = _age(now, ok["response_received_at"]) if ok else None
    freshness = "NO_DATA" if ok is None else ("CURRENT" if ok_age <= stale_after else "STALE")
    settings = [s for s in store.display_settings(room_id) if s["enabled"]]
    metrics_view = [_metric_view(store, room_id, s, provenance, now, stale_after) for s in settings]
    ref_trend = next((m["trend"] for m in metrics_view if m.get("trend")), [])
    ref_ts = [p[0] for p in ref_trend]
    from . import eight_sleep
    return {
        "generated_at": adapter.iso(now),
        "room_id": room_id,
        "room_name_zh": cfg["room_name_zh"],
        "source_mode": provenance,
        "actual_source_connectivity": connectivity,
        "data_freshness": freshness,
        "last_attempt_at": last["request_started_at"] if last else None,
        "last_success_at": ok["response_received_at"] if ok else None,
        "last_success_age_s": ok_age,
        "stale_after_s": stale_after,
        "error_summary_zh": None if (last is None or last["outcome"] == "OK") else ERROR_ZH.get(last["outcome"], "未知错误"),
        "metrics": metrics_view,
        "pod": {k: catalog.POD5_DESCRIPTOR[k] for k in ("label_zh", "status", "status_zh", "note_zh")},
        "eight_sleep": eight_sleep.build_view(store, cfg, now=now, ref_timestamps=ref_ts),
        "config_revision": store.config_revision(room_id),
    }
