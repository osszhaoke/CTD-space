"""Synthetic AirGradient-shaped source. Every observation is tagged SYNTHETIC; never LIVE."""
import json
import math


def body_at(t, *, case="observed"):
    """Deterministic payload for time t (aware datetime). Cases exercise 0/null/absent/invalid."""
    s = t.timestamp()
    doc = {
        "pm01": round(4 + 2 * math.sin(s / 900), 1),
        "pm02": round(9 + 4 * math.sin(s / 1200), 1),
        "pm10": round(12 + 5 * math.sin(s / 1500), 1),
        "pm02Compensated": round(8 + 3 * math.sin(s / 1200), 1),
        "rco2": int(620 + 180 * math.sin(s / 1800)),
        "atmp": round(25.5 + 1.5 * math.sin(s / 3600), 2),
        "rhum": round(55 + 8 * math.sin(s / 2700), 1),
        "tvocIndex": int(100 + 30 * math.sin(s / 2000)),
        "noxIndex": 1,
        "serialno": "synthetic-not-a-device",
        "wifi": -50,
    }
    if case == "zero":
        doc["pm02"] = 0
    elif case == "null":
        doc["pm02"] = None
    elif case == "absent":
        doc.pop("pm02")
    elif case == "invalid":
        doc["pm02"] = "high"
    return json.dumps(doc).encode("utf-8")


def make_fetcher(clock, case="observed"):
    def fetch(_base_url, *, timeout=5.0):
        started = clock()
        return {"outcome": "OK", "http_status": 200, "body": body_at(started, case=case),
                "request_started_at": started, "response_received_at": clock()}
    return fetch
