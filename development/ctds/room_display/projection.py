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
TREND_WINDOW = dt.timedelta(hours=1)   # small cards: last 1 hour, not downsampled at 60 s polling
TREND_POINTS = 120
HISTORY_MAX_HOURS = 24                  # chart modal and timeline: last 24 hours
HISTORY_POINTS = 288                    # ~5 min per point over 24 h
TIMELINE_TOLERANCE = dt.timedelta(minutes=30)
SERIES_FETCH_LIMIT = 100000


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
        "poll_interval_s": cfg.get("poll_interval_s", 60),
        "trend_window_s": int(TREND_WINDOW.total_seconds()),
        "error_summary_zh": None if (last is None or last["outcome"] == "OK") else ERROR_ZH.get(last["outcome"], "未知错误"),
        "metrics": metrics_view,
        "pod": {k: catalog.POD5_DESCRIPTOR[k] for k in ("label_zh", "status", "status_zh", "note_zh")},
        "eight_sleep": eight_sleep.build_view(store, cfg, now=now, ref_timestamps=ref_ts),
        "config_revision": store.config_revision(room_id),
    }


def _history_source(cfg, metric_id):
    """Return (source_id, provenance) whose stored observations back metric_id, or None if not readable."""
    from . import eight_sleep
    if metric_id in catalog.METRICS:
        return cfg["source"]["source_id"], ("LIVE" if cfg["source"]["mode"] == "LIVE" else "SYNTHETIC")
    if metric_id in eight_sleep.EIGHT_SLEEP_METRICS:
        es_cfg = dict(eight_sleep.DEFAULT_ES_CONFIG)
        if isinstance(cfg.get("eight_sleep"), dict):
            es_cfg.update(cfg["eight_sleep"])
        if es_cfg["mode"] == "READY":
            return None  # not connected: no history may be shown
        return es_cfg["source_id"], ("LIVE" if es_cfg["mode"] == "LIVE" else "SYNTHETIC")
    return None


def history_metric_ids(cfg):
    from . import eight_sleep
    return set(catalog.METRICS) | set(eight_sleep.EIGHT_SLEEP_METRICS)


def build_series(store, cfg, metric_id, hours, now=None):
    """Real observations of one metric over the last `hours` (1..24), downsampled to HISTORY_POINTS."""
    from . import eight_sleep
    now = now or adapter.utc_now()
    unit = (catalog.METRICS.get(metric_id) or eight_sleep.EIGHT_SLEEP_METRICS.get(metric_id) or {}).get("unit")
    src = _history_source(cfg, metric_id)
    points, provenance = [], None
    if src is not None:
        _, provenance = src
        since = adapter.iso(now - dt.timedelta(hours=hours))
        rows = store.series(cfg["room_id"], metric_id, since, limit=SERIES_FETCH_LIMIT, provenance=provenance)
        points = _downsample([[p["t"], p["value"]] for p in rows], HISTORY_POINTS)
    return {"metric_id": metric_id, "unit": unit, "window_h": hours, "provenance": provenance,
            "generated_at": adapter.iso(now), "points": points}


def _nearest(points, target, tolerance):
    """points: [(datetime, value, iso)] sorted by time. Nearest within tolerance, else None. No interpolation."""
    best, best_diff = None, None
    lo, hi = 0, len(points)
    while lo < hi:  # first index with time >= target
        mid = (lo + hi) // 2
        if points[mid][0] < target:
            lo = mid + 1
        else:
            hi = mid
    for i in (lo - 1, lo):
        if 0 <= i < len(points):
            diff = abs((points[i][0] - target).total_seconds())
            if diff <= tolerance.total_seconds() and (best_diff is None or diff < best_diff):
                best, best_diff = points[i], diff
    return {"value": best[1], "observed_at": best[2]} if best else None


def build_timeline(store, cfg, hours=24, now=None):
    """`hours` whole-hour nodes ending at the current hour, plus a final "now" node.

    Each node carries, per metric, the nearest real observation within ±30 min (else null)."""
    now = now or adapter.utc_now()
    top = now.replace(minute=0, second=0, microsecond=0)
    anchors = [top - dt.timedelta(hours=hours - 1 - i) for i in range(hours)] + [now]
    since = adapter.iso(anchors[0] - TIMELINE_TOLERANCE)
    enabled = [s["metric_id"] for s in store.display_settings(cfg["room_id"]) if s["enabled"]]
    from . import eight_sleep
    metric_ids = enabled + sorted(eight_sleep.EIGHT_SLEEP_METRICS, key=lambda k: eight_sleep.EIGHT_SLEEP_METRICS[k]["order"])
    series = {}
    for mid in metric_ids:
        src = _history_source(cfg, mid)
        if src is None:
            series[mid] = []
            continue
        rows = store.series(cfg["room_id"], mid, since, limit=SERIES_FETCH_LIMIT, provenance=src[1])
        series[mid] = [(_parse(p["t"]), p["value"], p["t"]) for p in rows]
    nodes = []
    for i, t in enumerate(anchors):
        nodes.append({"index": i, "t": adapter.iso(t), "is_now": i == len(anchors) - 1,
                      "values": {mid: _nearest(series[mid], t, TIMELINE_TOLERANCE) for mid in metric_ids}})
    return {"room_id": cfg["room_id"], "window_h": hours, "generated_at": adapter.iso(now),
            "tolerance_s": int(TIMELINE_TOLERANCE.total_seconds()), "nodes": nodes}
