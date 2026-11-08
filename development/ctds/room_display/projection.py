"""Read-only RoomDisplay projection from the store (whitelisted fields only).

Only observations whose provenance matches the configured source mode are shown,
so synthetic history can never appear on a LIVE screen.
"""
import datetime as dt
import math

from . import adapter, catalog

CONNECTIVITY = {
    "OK": "CONNECTED", "TIMEOUT": "DISCONNECTED", "NO_CONNECTION": "DISCONNECTED",
    "AUTH_REJECTED": "AUTH_PAUSED", "NOT_FOUND": "ERROR", "HTTP_ERROR": "ERROR", "RATE_LIMITED": "ERROR",
    "REDIRECT_REFUSED": "ERROR", "TOO_LARGE": "ERROR", "INVALID_PAYLOAD": "ERROR",
    "MQTT_OK": "CONNECTED", "MQTT_DUPLICATE": "CONNECTED", "MQTT_DISCONNECTED": "DISCONNECTED",
    "MQTT_AUTH_REJECTED": "AUTH_PAUSED", "MQTT_TOPIC_REJECTED": "ERROR",
    "MQTT_SENSOR_MISMATCH": "ERROR", "MQTT_TOO_LARGE": "ERROR",
}
ERROR_ZH = {
    "TIMEOUT": "设备响应超时", "NO_CONNECTION": "无法连接设备", "AUTH_REJECTED": "设备拒绝访问，已暂停读取",
    "NOT_FOUND": "设备接口不存在（固件或地址需核对）", "HTTP_ERROR": "设备返回错误", "RATE_LIMITED": "请求过于频繁",
    "REDIRECT_REFUSED": "设备返回重定向，已拒绝", "TOO_LARGE": "设备响应过大，已拒绝", "INVALID_PAYLOAD": "设备数据格式无效",
    "MQTT_DISCONNECTED": "MQTT 设备超时未上报", "MQTT_AUTH_REJECTED": "MQTT 认证被拒绝，已暂停接收",
    "MQTT_TOPIC_REJECTED": "拒绝非只读测量 Topic", "MQTT_SENSOR_MISMATCH": "MQTT 设备标识与绑定不匹配",
    "MQTT_TOO_LARGE": "MQTT 消息过大，已拒绝",
}
STATUS_ZH = {"CURRENT": "实时", "STALE": "已过期", "NO_DATA": "暂无数据", "NOT_PROVIDED": "设备未提供",
             "EXPLICIT_NULL": "设备返回空值", "INVALID": "数值无效"}
TREND_WINDOW = dt.timedelta(hours=1)   # small cards: last 1 hour, not downsampled at 60 s polling
TREND_POINTS = 120
HISTORY_MAX_HOURS = 24                  # chart modal default and timeline: last 24 hours
HISTORY_POINTS = 288                    # ~5 min per point over 24 h
SERIES_RANGES = {"24h": 24, "7d": 168, "30d": 720, "all": None}
MODAL_MAX_POINTS = 360                  # peak-preserving min/max vertices for multi-day / all-time modal charts
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


def _downsample_minmax(points, n=MODAL_MAX_POINTS):
    """Peak-preserving downsampler: keeps exact stored `[t, v]` pairs (endpoints + per-bucket min & max)."""
    if len(points) <= n or n < 4:
        return points
    middle = points[1:-1]
    n_buckets = max(1, (n - 2) // 2)
    step = len(middle) / n_buckets
    out = [points[0]]
    for b in range(n_buckets):
        lo = int(b * step)
        hi = int((b + 1) * step) if b < n_buckets - 1 else len(middle)
        if lo >= hi:
            continue
        chunk = middle[lo:hi]
        min_idx = min(range(len(chunk)), key=lambda i: (chunk[i][1], i))
        max_idx = max(range(len(chunk)), key=lambda i: (chunk[i][1], i))
        if min_idx == max_idx:
            out.append(chunk[min_idx])
        elif min_idx < max_idx:
            out.append(chunk[min_idx])
            out.append(chunk[max_idx])
        else:
            out.append(chunk[max_idx])
            out.append(chunk[min_idx])
    out.append(points[-1])
    return out


def _metric_view(store, room_id, setting, provenance, now, stale_after):
    mid = setting["metric_id"]
    m = catalog.ALL_AG_METRICS[mid]
    latest = store.latest_observation(room_id, mid, provenance=provenance)
    good = store.latest_observation(room_id, mid, observed_only=True, provenance=provenance)
    view = {"metric_id": mid, "label_zh": setting["label_zh"], "unit": m["unit"],
            "value": None, "observed_or_received_at": None, "time_basis": None, "age_s": None,
            "status": "NO_DATA", "latest_value_status": latest["value_status"] if latest else None}
    if good:
        eff_ts = good["provider_observed_at"] or good["gateway_received_at"]
        age = _age(now, eff_ts)
        view.update(value=good["value"], observed_or_received_at=eff_ts,
                    time_basis=good["time_basis"], age_s=age)
    if latest and latest["value_status"] != "OBSERVED":
        view["status"] = latest["value_status"]  # last good (if any) still shown with its own time
    elif good:
        view["status"] = "CURRENT" if view["age_s"] <= stale_after else "STALE"
    view["status_zh"] = STATUS_ZH[view["status"]]
    since = adapter.iso(now - TREND_WINDOW)
    pts = store.series(room_id, mid, since, provenance=provenance)
    if not pts and good:
        eff_ts = good["provider_observed_at"] or good["gateway_received_at"]
        since_good = adapter.iso(_parse(eff_ts) - TREND_WINDOW)
        pts = store.series(room_id, mid, since_good, provenance=provenance)
    view["trend"] = _downsample([[p["t"], p["value"]] for p in pts])
    return view


def _cloud_backfill_view(store, cfg):
    mgr = getattr(store, "_cloud_backfill_mgr", None)
    if mgr is not None:
        return mgr.status_snapshot()
    enabled = bool(cfg["source"].get("cloud_backfill_enabled"))
    done = store.get_meta("ag_cloud_full_backfill_done") == "1"
    return {
        "enabled": enabled,
        "state": ("READY" if done else "IDLE") if enabled else "DISABLED",
        "last_error_code": None,
        "full_backfill_done": done,
        "oldest_scanned_at": store.get_meta("ag_cloud_oldest_scanned_utc"),
        "last_synced_at": store.get_meta("ag_cloud_last_synced_utc"),
        "total_backfilled": int(store.get_meta("ag_cloud_backfilled_observations", "0") or 0),
    }


def build_display(store, cfg, now=None):
    now = now or adapter.utc_now()
    room_id, src_id = cfg["room_id"], cfg["source"]["source_id"]
    provenance = "LIVE" if cfg["source"]["mode"] == "LIVE" else "SYNTHETIC"
    stale_after = cfg["stale_after_s"]
    source = store.source(src_id)
    last = store.latest_attempt(src_id, provenance=provenance, exclude_cloud=True)
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
        "error_summary_zh": None if (last is None or last["outcome"] in ("OK", "MQTT_OK", "MQTT_DUPLICATE"))
        else ERROR_ZH.get(last["outcome"], "未知错误"),
        "cloud_backfill": _cloud_backfill_view(store, cfg),
        "metrics": metrics_view,
        "pod": {k: catalog.POD5_DESCRIPTOR[k] for k in ("label_zh", "status", "status_zh", "note_zh")},
        "eight_sleep": eight_sleep.build_view(store, cfg, now=now, ref_timestamps=ref_ts),
        "config_revision": store.config_revision(room_id),
    }


def _history_source(cfg, metric_id):
    """Return (source_id, provenance) whose stored observations back metric_id, or None if not readable."""
    from . import eight_sleep
    if metric_id in catalog.ALL_AG_METRICS:
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
    return set(catalog.ALL_AG_METRICS) | set(eight_sleep.EIGHT_SLEEP_METRICS)


def build_series(store, cfg, metric_id, hours=24, now=None, *, range_key=None):
    """Real observations of one metric over `hours` (1..24) or `range_key` ('24h'|'7d'|'30d'|'all')."""
    from . import eight_sleep
    now = now or adapter.utc_now()
    unit = (catalog.ALL_AG_METRICS.get(metric_id) or eight_sleep.EIGHT_SLEEP_METRICS.get(metric_id) or {}).get("unit")
    src = _history_source(cfg, metric_id)
    points, provenance = [], None
    total_count = 0
    min_val = max_val = avg_val = None

    if range_key is not None:
        if range_key not in SERIES_RANGES:
            raise ValueError(f"unsupported range_key: {range_key}")
        cfg_hours = SERIES_RANGES[range_key]
        if cfg_hours is None:
            window_h = 24
            since = adapter.iso(now - dt.timedelta(hours=24))
            if src is not None:
                _, provenance = src
                bounds = store.series_bounds(cfg["room_id"], metric_id, provenance=provenance)
                if bounds["count"] > 0 and bounds["min_t"]:
                    min_dt = _parse(bounds["min_t"])
                    span_s = max(3600.0, (now - min_dt).total_seconds())
                    window_h = max(1, int(math.ceil(span_s / 3600.0)))
                    since = bounds["min_t"]
        else:
            window_h = cfg_hours
            since = adapter.iso(now - dt.timedelta(hours=window_h))

        if src is not None:
            _, provenance = src
            rows = store.series(cfg["room_id"], metric_id, since, limit=None, provenance=provenance)
            raw_pts = [[p["t"], p["value"]] for p in rows]
            total_count = len(raw_pts)
            if total_count > 0:
                vals = [pt[1] for pt in raw_pts]
                min_val = min(vals)
                max_val = max(vals)
                avg_val = sum(vals) / len(vals)
            points = _downsample_minmax(raw_pts, MODAL_MAX_POINTS)
        return {
            "metric_id": metric_id,
            "unit": unit,
            "range": range_key,
            "window_h": window_h,
            "start_at": since,
            "end_at": adapter.iso(now),
            "provenance": provenance,
            "generated_at": adapter.iso(now),
            "total_count": total_count,
            "min_value": min_val,
            "max_value": max_val,
            "avg_value": avg_val,
            "points": points,
        }

    since = adapter.iso(now - dt.timedelta(hours=hours))
    if src is not None:
        _, provenance = src
        rows = store.series(cfg["room_id"], metric_id, since, limit=SERIES_FETCH_LIMIT, provenance=provenance)
        raw_pts = [[p["t"], p["value"]] for p in rows]
        total_count = len(raw_pts)
        if total_count > 0:
            vals = [pt[1] for pt in raw_pts]
            min_val = min(vals)
            max_val = max(vals)
            avg_val = sum(vals) / len(vals)
        points = _downsample(raw_pts, HISTORY_POINTS)
    return {
        "metric_id": metric_id,
        "unit": unit,
        "window_h": hours,
        "start_at": since,
        "end_at": adapter.iso(now),
        "provenance": provenance,
        "generated_at": adapter.iso(now),
        "total_count": total_count,
        "min_value": min_val,
        "max_value": max_val,
        "avg_value": avg_val,
        "points": points,
    }


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
