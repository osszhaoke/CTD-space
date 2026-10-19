"""Metric capability catalog (AirGradient candidates) and Pod 5 READY descriptor.

Availability is DOC_ONLY until a real device response proves a field (then the
store records VERIFIED per source from observed fields). Nothing here unlocks
personal data; Pod 5 has no metrics that can be displayed.
"""
MAPPING_VERSION = "ctds.c01.airgradient-local.mapping/0.1.0"
DOC_REF = "airgradienthq/arduino docs/local-server.md@0b7c8c0cb7f7b4282fb83ef82c30b4da0c8e8360"

# metric_id -> provider field, unit, Chinese label, plausible range (inclusive), default order
METRICS = {
    "pm2_5":       {"field": "pm02",            "unit": "µg/m³", "label_zh": "PM2.5",      "range": (0, 5000),   "order": 1, "default": True},
    "co2":         {"field": "rco2",            "unit": "ppm",   "label_zh": "二氧化碳",    "range": (0, 40000),  "order": 2, "default": True},
    "temperature": {"field": "atmp",            "unit": "℃",     "label_zh": "温度",       "range": (-40, 85),   "order": 3, "default": True},
    "humidity":    {"field": "rhum",            "unit": "%RH",   "label_zh": "相对湿度",    "range": (0, 100),    "order": 4, "default": True},
    "tvoc_index":  {"field": "tvocIndex",       "unit": "指数",   "label_zh": "TVOC指数",   "range": (0, 500),    "order": 5, "default": True},
    "pm1":         {"field": "pm01",            "unit": "µg/m³", "label_zh": "PM1.0",      "range": (0, 5000),   "order": 6, "default": True},
    "pm10":        {"field": "pm10",            "unit": "µg/m³", "label_zh": "PM10",       "range": (0, 5000),   "order": 7, "default": True},
    "nox_index":   {"field": "noxIndex",        "unit": "指数",   "label_zh": "NOx指数",    "range": (0, 500),    "order": 8, "default": True},
    # Compensated PM2.5 is a separate metric; never used to fill raw pm2_5.
    "pm2_5_compensated": {"field": "pm02Compensated", "unit": "µg/m³", "label_zh": "PM2.5（校正）", "range": (0, 5000), "order": 9, "default": False},
}

# Provider fields that must never be stored or displayed (identity/network/device internals).
FORBIDDEN_FIELDS = frozenset({"serialno", "wifi", "model", "firmware", "boot", "bootCount", "ledMode"})

POD5_DESCRIPTOR = {
    "provider": "eight_sleep",
    "device_class": "Pod 5",
    "status": "READY_NOT_CONNECTED",
    "label_zh": "Eight Sleep Pod 5",
    "status_zh": "待接入",
    "note_zh": "本期仅登记，不连接账号、不读取个人数据、不控制设备",
    "metrics": [],  # intentionally empty; no toggle can add personal metrics
}


def capabilities(verified_metric_ids=()):
    verified = set(verified_metric_ids)
    out = []
    for mid, m in sorted(METRICS.items(), key=lambda kv: kv[1]["order"]):
        out.append({
            "metric_id": mid,
            "canonical_unit": m["unit"],
            "description_zh": m["label_zh"],
            "availability": "VERIFIED" if mid in verified else "DOC_ONLY",
            "visibility_allowed": True,
            "sensitivity": "ENVIRONMENT",
        })
    return out
