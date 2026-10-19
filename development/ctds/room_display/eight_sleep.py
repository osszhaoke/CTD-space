"""Eight Sleep Pod 5 read-only API client, heating level lookup table, and 6-metric normalizer.

Implements the exact 6 metrics specified for 睡眠房:
1. 心率 (pod_heart_rate): latest day -> last sleep session -> timeseries.heartRate[-1][1]
2. 心率变异性 HRV (pod_hrv): sleepQualityScore.hrv.current
3. 呼吸率 (pod_respiratory_rate): sleepQualityScore.respiratoryRate.current
4. 床温 (pod_bed_temp): GET /v1/users/{user_id}/temperature -> currentDeviceLevel -> RAW_TO_CELSIUS_MAP
5. 目标床温 (pod_target_bed_temp): GET /v1/devices/{device_id} -> left/right TargetHeatingLevel -> RAW_TO_CELSIUS_MAP
6. 室温 (pod_room_temp): latest day -> last sleep session -> timeseries.tempRoomC[-1][1]

Reference: lukas-clarke/pyEight (MIT License) constants & user/device/temperature/trends endpoints.
"""
import datetime as dt
import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import adapter

MAPPING_VERSION = "ctds.c01.eight-sleep.mapping/0.1.0"
DOC_REF = "lukas-clarke/pyEight@1.0.18 (MIT)"

AUTH_URL = "https://auth-api.8slp.net/v1/tokens"
CLIENT_API_URL = "https://client-api.8slp.net/v1"
APP_API_URL = "https://app-api.8slp.net/v1"
KNOWN_CLIENT_ID = "0894c7f33bb94800a03f1f4df13a4f38"
KNOWN_CLIENT_SECRET = "f0954a3ed5763ba3d06834c73731a32f15f168f47d4f164751275def86db0c76"
TOKEN_TIME_BUFFER_SECONDS = 120

# Eight Sleep heating level (-100..100) -> Celsius lookup table (from pyEight constants.py)
RAW_TO_CELSIUS_MAP = {
    -100: 13,
    -97: 14,
    -94: 15,
    -91: 16,
    -83: 17,
    -75: 18,
    -67: 19,
    -58: 20,
    -50: 21,
    -42: 22,
    -33: 23,
    -25: 24,
    -17: 25,
    -8: 26,
    0: 27,
    6: 28,
    11: 29,
    17: 30,
    22: 31,
    28: 32,
    33: 33,
    39: 34,
    44: 35,
    50: 36,
    56: 37,
    61: 38,
    67: 39,
    72: 40,
    78: 41,
    83: 42,
    89: 43,
    100: 44,
}

EIGHT_SLEEP_METRICS = {
    "pod_heart_rate": {
        "label_zh": "心率",
        "unit": "bpm",
        "range": (25, 220),
        "order": 1,
        "source_rule": "timeseries.heartRate[-1]",
    },
    "pod_hrv": {
        "label_zh": "心率变异性 HRV",
        "unit": "ms",
        "range": (1, 300),
        "order": 2,
        "source_rule": "sleepQualityScore.hrv.current",
    },
    "pod_respiratory_rate": {
        "label_zh": "呼吸率",
        "unit": "次/分",
        "range": (4, 60),
        "order": 3,
        "source_rule": "sleepQualityScore.respiratoryRate.current",
    },
    "pod_bed_temp": {
        "label_zh": "床温",
        "unit": "℃",
        "range": (10, 50),
        "order": 4,
        "source_rule": "currentDeviceLevel -> RAW_TO_CELSIUS_MAP",
    },
    "pod_target_bed_temp": {
        "label_zh": "目标床温",
        "unit": "℃",
        "range": (10, 50),
        "order": 5,
        "source_rule": "TargetHeatingLevel -> RAW_TO_CELSIUS_MAP",
    },
    "pod_room_temp": {
        "label_zh": "室温",
        "unit": "℃",
        "range": (-10, 60),
        "order": 6,
        "source_rule": "timeseries.tempRoomC[-1]",
    },
}

DEFAULT_ES_CONFIG = {
    "source_id": "pod-room-01",
    "mode": "READY",  # "READY" | "SYNTHETIC" | "LIVE"
    "email": None,
    "password": None,
    "bearer_token": None,
    "user_id": None,
    "device_id": None,
    "side": "auto",  # "auto" | "left" | "right" | "solo"
    "timezone": "Asia/Shanghai",
    "client_id": None,
    "client_secret": None,
    "binding_revision": 1,
}


def heating_level_to_temp(heating_level):
    """Convert Eight Sleep heating level (-100 to 100) to Celsius using RAW_TO_CELSIUS_MAP."""
    if heating_level is None or isinstance(heating_level, bool) or not isinstance(heating_level, (int, float)):
        return None
    lvl = max(-100.0, min(100.0, float(heating_level)))
    closest_key = min(RAW_TO_CELSIUS_MAP.keys(), key=lambda k: abs(k - lvl))
    return float(RAW_TO_CELSIUS_MAP[closest_key])


def _is_num(val):
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _last_timeseries_point(sessions, key):
    """Find the last session containing a non-empty timeseries[key] and return (ts_str, float_val, history_pairs)."""
    if not isinstance(sessions, list):
        return None, None, []
    for sess in reversed(sessions):
        if not isinstance(sess, dict):
            continue
        ts_map = sess.get("timeseries")
        if not isinstance(ts_map, dict):
            continue
        seq = ts_map.get(key)
        if isinstance(seq, list) and len(seq) > 0:
            valid_pts = []
            for item in seq:
                if isinstance(item, (list, tuple)) and len(item) >= 2 and _is_num(item[1]):
                    valid_pts.append([str(item[0]), round(float(item[1]), 1)])
            if valid_pts:
                last_ts, last_val = valid_pts[-1]
                return last_ts, last_val, valid_pts[-12:]
    return None, None, []


def extract_six_metrics(trends_doc, temp_doc, device_doc, *, side="auto"):
    """Extract the 6 Eight Sleep metrics and metadata from raw API documents."""
    days = []
    if isinstance(trends_doc, dict) and isinstance(trends_doc.get("days"), list):
        days = [d for d in trends_doc["days"] if isinstance(d, dict)]

    latest_day = days[-1] if days else {}
    sessions = latest_day.get("sessions") if isinstance(latest_day.get("sessions"), list) else []

    # 1. 心率: 最近一天、最后一个睡眠会话中，timeseries.heartRate 的最后一个值
    hr_ts, hr_val, hr_trend = _last_timeseries_point(sessions, "heartRate")

    # 2. 心率变异性 HRV: sleepQualityScore.hrv.current
    sq = latest_day.get("sleepQualityScore") if isinstance(latest_day.get("sleepQualityScore"), dict) else {}
    hrv_obj = sq.get("hrv") if isinstance(sq.get("hrv"), dict) else {}
    hrv_raw = hrv_obj.get("current")
    hrv_val = round(float(hrv_raw), 1) if _is_num(hrv_raw) else None

    # 3. 呼吸率: sleepQualityScore.respiratoryRate.current
    rr_obj = sq.get("respiratoryRate") if isinstance(sq.get("respiratoryRate"), dict) else {}
    rr_raw = rr_obj.get("current")
    rr_val = round(float(rr_raw), 1) if _is_num(rr_raw) else None

    # 4. 床温: 温控接口的 currentDeviceLevel，经过查表换算
    raw_device_level = None
    if isinstance(temp_doc, dict) and _is_num(temp_doc.get("currentDeviceLevel")):
        raw_device_level = int(round(float(temp_doc["currentDeviceLevel"])))
    bed_temp_c = heating_level_to_temp(raw_device_level)

    # 5. 目标床温: 设备的左右侧 TargetHeatingLevel，经过查表换算
    dev_res = device_doc.get("result") if isinstance(device_doc, dict) and isinstance(device_doc.get("result"), dict) else (device_doc if isinstance(device_doc, dict) else {})
    left_lvl = dev_res.get("leftTargetHeatingLevel")
    right_lvl = dev_res.get("rightTargetHeatingLevel")
    left_lvl = int(round(float(left_lvl))) if _is_num(left_lvl) else None
    right_lvl = int(round(float(right_lvl))) if _is_num(right_lvl) else None
    left_target_c = heating_level_to_temp(left_lvl)
    right_target_c = heating_level_to_temp(right_lvl)

    norm_side = (side or "auto").lower()
    if norm_side in ("left", "solo"):
        chosen_target_lvl = left_lvl if left_lvl is not None else right_lvl
    elif norm_side == "right":
        chosen_target_lvl = right_lvl if right_lvl is not None else left_lvl
    else:
        chosen_target_lvl = left_lvl if left_lvl is not None else right_lvl
    if chosen_target_lvl is None and isinstance(temp_doc, dict) and _is_num(temp_doc.get("currentLevel")):
        chosen_target_lvl = int(round(float(temp_doc["currentLevel"])))
    target_bed_temp_c = heating_level_to_temp(chosen_target_lvl)

    # 6. 室温: 睡眠会话中 timeseries.tempRoomC 的最后一个值
    rt_ts, rt_val, rt_trend = _last_timeseries_point(sessions, "tempRoomC")

    return {
        "metrics": {
            "pod_heart_rate": {"value": hr_val, "observed_at": hr_ts, "trend": hr_trend},
            "pod_hrv": {"value": hrv_val, "observed_at": hr_ts, "trend": []},
            "pod_respiratory_rate": {"value": rr_val, "observed_at": hr_ts, "trend": []},
            "pod_bed_temp": {
                "value": bed_temp_c,
                "observed_at": None,
                "raw_level": raw_device_level,
                "trend": [],
            },
            "pod_target_bed_temp": {
                "value": target_bed_temp_c,
                "observed_at": None,
                "raw_level": chosen_target_lvl,
                "left_level": left_lvl,
                "right_level": right_lvl,
                "left_temp_c": left_target_c,
                "right_temp_c": right_target_c,
                "trend": [],
            },
            "pod_room_temp": {"value": rt_val, "observed_at": rt_ts, "trend": rt_trend},
        },
        "meta": {
            "raw_device_level": raw_device_level,
            "target_heating_level": chosen_target_lvl,
            "left_target_level": left_lvl,
            "right_target_level": right_lvl,
            "left_target_temp_c": left_target_c,
            "right_target_temp_c": right_target_c,
        },
    }


def normalize_bundle(trends_doc, temp_doc, device_doc, *, source_id, room_id, attempt_id, received_at, provenance, side="auto"):
    """Normalize Eight Sleep API payloads into append-only Store observation rows."""
    if provenance not in ("LIVE", "REPLAY", "SYNTHETIC"):
        raise adapter.AdapterError("INVALID_PROVENANCE", provenance)

    extracted = extract_six_metrics(trends_doc, temp_doc, device_doc, side=side)
    rec_iso = adapter.iso(received_at)
    canonical_blob = json.dumps(extracted["metrics"], sort_keys=True, ensure_ascii=False).encode("utf-8")
    payload_sha = hashlib.sha256(canonical_blob).hexdigest()

    observations = []
    for mid, mdef in sorted(EIGHT_SLEEP_METRICS.items(), key=lambda kv: kv[1]["order"]):
        item = extracted["metrics"].get(mid, {})
        val = item.get("value")
        obs_at = item.get("observed_at")
        lo, hi = mdef["range"]
        if val is None:
            status, clean_val = "NOT_PROVIDED", None
        elif _is_num(val) and lo <= float(val) <= hi:
            status, clean_val = "OBSERVED", round(float(val), 2)
        else:
            status, clean_val = "INVALID", None

        ident = f"{source_id}:{attempt_id}:{mid}"
        event_id = hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32]
        observations.append({
            "event_id": event_id,
            "source_id": source_id,
            "room_id": room_id,
            "metric_id": mid,
            "value_status": status,
            "value": clean_val,
            "unit": mdef["unit"],
            "provider_observed_at": obs_at if status == "OBSERVED" else None,
            "gateway_received_at": rec_iso,
            "time_basis": "PROVIDER_OBSERVED" if (status == "OBSERVED" and obs_at) else "RECEIPT_ONLY",
            "mapping_version": MAPPING_VERSION,
            "data_provenance": provenance,
            "source_event_identity": ident,
        })
    return observations, payload_sha, extracted["meta"]


class EightSleepClient:
    """Pure-stdlib read-only client for Eight Sleep Cloud API (auth-api, client-api, app-api)."""

    def __init__(self, es_cfg, *, timeout=10.0, clock=adapter.utc_now, opener=None):
        self.cfg = dict(DEFAULT_ES_CONFIG)
        if isinstance(es_cfg, dict):
            self.cfg.update(es_cfg)
        self.timeout = timeout
        self.clock = clock
        self._opener = opener or urllib.request.build_opener()
        self._token = self.cfg.get("bearer_token") or os.environ.get("EIGHT_SLEEP_BEARER_TOKEN")
        self._token_exp = float("inf") if self._token else 0.0
        self._user_id = self.cfg.get("user_id") or os.environ.get("EIGHT_SLEEP_USER_ID")
        self._device_id = self.cfg.get("device_id") or os.environ.get("EIGHT_SLEEP_DEVICE_ID")
        self._side = (self.cfg.get("side") or os.environ.get("EIGHT_SLEEP_SIDE") or "auto").lower()

    def _request_json(self, method, url, *, headers=None, json_body=None):
        req_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "okhttp/4.9.3",
        }
        if headers:
            req_headers.update(headers)
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        with self._opener.open(req, timeout=self.timeout) as resp:
            raw = resp.read(262144)
            if resp.headers.get("Content-Encoding", "").lower() == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return resp.status, json.loads(raw.decode("utf-8"))

    def authenticate(self):
        email = self.cfg.get("email") or os.environ.get("EIGHT_SLEEP_EMAIL")
        password = self.cfg.get("password") or os.environ.get("EIGHT_SLEEP_PASSWORD")
        if not email or not password:
            if self._token:
                return self._token
            raise adapter.AdapterError("AUTH_REJECTED", "missing Eight Sleep credentials")

        payload = {
            "client_id": self.cfg.get("client_id") or KNOWN_CLIENT_ID,
            "client_secret": self.cfg.get("client_secret") or KNOWN_CLIENT_SECRET,
            "grant_type": "password",
            "username": email,
            "password": password,
        }
        status, doc = self._request_json("POST", AUTH_URL, json_body=payload)
        if status != 200 or not isinstance(doc, dict) or not doc.get("access_token"):
            raise adapter.AdapterError("AUTH_REJECTED", "token exchange failed")
        self._token = doc["access_token"]
        self._token_exp = time.time() + float(doc.get("expires_in", 3600))
        if doc.get("userId") and not self._user_id:
            self._user_id = str(doc["userId"])
        return self._token

    def _ensure_token(self):
        if not self._token or (time.time() + TOKEN_TIME_BUFFER_SECONDS >= self._token_exp):
            return self.authenticate()
        return self._token

    def _authed_get(self, url):
        token = self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            _, doc = self._request_json("GET", url, headers=headers)
            return doc
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and (self.cfg.get("email") or os.environ.get("EIGHT_SLEEP_EMAIL")):
                self._token = None
                token = self.authenticate()
                headers = {"Authorization": f"Bearer {token}"}
                _, doc = self._request_json("GET", url, headers=headers)
                return doc
            raise

    def fetch_bundle(self):
        """Fetch user/me, device, temperature, and recent sleep trends (strictly read-only GETs)."""
        t0 = self.clock()
        try:
            if not self._user_id or not self._device_id:
                me_doc = self._authed_get(f"{CLIENT_API_URL}/users/me")
                user_obj = me_doc.get("user", {}) if isinstance(me_doc, dict) else {}
                if not self._user_id and user_obj.get("userId"):
                    self._user_id = str(user_obj["userId"])
                devices = user_obj.get("devices")
                if not self._device_id and isinstance(devices, list) and devices:
                    self._device_id = str(devices[0])

            if self._side == "auto" and self._user_id:
                try:
                    cur_dev = self._authed_get(f"{CLIENT_API_URL}/users/{urllib.parse.quote(self._user_id)}/current-device")
                    if isinstance(cur_dev, dict) and cur_dev.get("side"):
                        self._side = str(cur_dev["side"]).lower()
                except Exception:
                    pass

            uid_q = urllib.parse.quote(str(self._user_id or ""))
            did_q = urllib.parse.quote(str(self._device_id or ""))

            temp_doc = self._authed_get(f"{APP_API_URL}/users/{uid_q}/temperature") if uid_q else {}
            device_doc = self._authed_get(f"{CLIENT_API_URL}/devices/{did_q}") if did_q else {}

            tz_name = self.cfg.get("timezone") or os.environ.get("EIGHT_SLEEP_TZ") or "Asia/Shanghai"
            start_d = (t0 - dt.timedelta(days=2)).strftime("%Y-%m-%d")
            end_d = (t0 + dt.timedelta(days=1)).strftime("%Y-%m-%d")
            q = urllib.parse.urlencode({
                "tz": tz_name,
                "from": start_d,
                "to": end_d,
                "include-main": "false",
                "include-all-sessions": "true",
                "model-version": "v2",
            })
            trends_doc = self._authed_get(f"{CLIENT_API_URL}/users/{uid_q}/trends?{q}") if uid_q else {}
            t1 = self.clock()
            return {
                "outcome": "OK",
                "http_status": 200,
                "request_started_at": t0,
                "response_received_at": t1,
                "trends_doc": trends_doc,
                "temp_doc": temp_doc,
                "device_doc": device_doc,
                "side": self._side,
            }
        except adapter.AdapterError as exc:
            return {"outcome": exc.code, "http_status": 401, "request_started_at": t0, "response_received_at": self.clock()}
        except urllib.error.HTTPError as exc:
            code = "AUTH_REJECTED" if exc.code in (401, 403) else ("RATE_LIMITED" if exc.code == 429 else "HTTP_ERROR")
            return {"outcome": code, "http_status": exc.code, "request_started_at": t0, "response_received_at": self.clock()}
        except (urllib.error.URLError, TimeoutError, OSError):
            return {"outcome": "NO_CONNECTION", "http_status": None, "request_started_at": t0, "response_received_at": None}
        except ValueError:
            return {"outcome": "INVALID_PAYLOAD", "http_status": 200, "request_started_at": t0, "response_received_at": self.clock()}


def make_synthetic_bundle(clock=adapter.utc_now, step_idx=0):
    """Generate a realistic synthetic Eight Sleep bundle for testing and READY preview."""
    now = clock()
    base_ts = now - dt.timedelta(minutes=10)
    hr_series = []
    rt_series = []
    for i in range(11):
        ts = adapter.iso(base_ts + dt.timedelta(minutes=i))
        hr_series.append([ts, round(58.0 + ((i + step_idx) % 4) - 1.0, 1)])
        rt_series.append([ts, round(22.4 + ((i % 3) * 0.1), 1)])
    return {
        "outcome": "OK",
        "http_status": 200,
        "request_started_at": now,
        "response_received_at": now,
        "side": "left",
        "temp_doc": {
            "currentDeviceLevel": -17,  # maps to 25°C via RAW_TO_CELSIUS_MAP
            "currentLevel": -17,
            "currentState": {"type": "smart"},
        },
        "device_doc": {
            "result": {
                "leftTargetHeatingLevel": -17,   # maps to 25°C
                "rightTargetHeatingLevel": -8,   # maps to 26°C
            }
        },
        "trends_doc": {
            "days": [
                {
                    "day": now.strftime("%Y-%m-%d"),
                    "sleepQualityScore": {
                        "hrv": {"current": 64.2},
                        "respiratoryRate": {"current": 14.6},
                    },
                    "sessions": [
                        {
                            "timeseries": {
                                "heartRate": hr_series,
                                "tempRoomC": rt_series,
                            }
                        }
                    ],
                }
            ]
        },
    }


def ingest_once(store, cfg, *, client=None, provenance=None, clock=adapter.utc_now):
    """Run one Eight Sleep collection attempt and persist observations into Store."""
    es_cfg = dict(DEFAULT_ES_CONFIG)
    if isinstance(cfg.get("eight_sleep"), dict):
        es_cfg.update(cfg["eight_sleep"])
    src_id = es_cfg["source_id"]
    mode = es_cfg["mode"]
    prov = provenance or ("LIVE" if mode == "LIVE" else "SYNTHETIC")

    store.register_source(
        source_id=src_id,
        provider="eight_sleep",
        device_class="Pod 5",
        room_id=cfg["room_id"],
        binding_revision=int(es_cfg.get("binding_revision", 1)),
        configuration_version=MAPPING_VERSION,
        status="ACTIVE",
        capability_version=DOC_REF,
        updated_at=adapter.iso(clock()),
    )

    attempt_id = uuid.uuid4().hex
    if client is not None:
        res = client.fetch_bundle()
    elif mode == "LIVE":
        res = EightSleepClient(es_cfg, timeout=cfg.get("timeout_s", 5), clock=clock).fetch_bundle()
    else:
        res = make_synthetic_bundle(clock=clock)

    outcome = res["outcome"]
    observations = []
    payload_sha = None
    if outcome == "OK":
        try:
            observations, payload_sha, _ = normalize_bundle(
                res.get("trends_doc"),
                res.get("temp_doc"),
                res.get("device_doc"),
                source_id=src_id,
                room_id=cfg["room_id"],
                attempt_id=attempt_id,
                received_at=res["response_received_at"],
                provenance=prov,
                side=res.get("side") or es_cfg.get("side", "auto"),
            )
        except adapter.AdapterError as exc:
            outcome = exc.code

    store.record_attempt(
        {
            "attempt_id": attempt_id,
            "source_id": src_id,
            "request_started_at": adapter.iso(res["request_started_at"]),
            "response_received_at": adapter.iso(res["response_received_at"]) if res.get("response_received_at") else None,
            "outcome": outcome,
            "http_status": res.get("http_status"),
            "payload_sha256": payload_sha,
            "data_provenance": prov,
        },
        observations,
    )
    if outcome == "AUTH_REJECTED":
        store.set_source_status(src_id, "AUTH_PAUSED", adapter.iso(clock()))
    return outcome


# Baseline preview series aligned to timeline points when mode == "READY" (prior to account handoff)
_PREVIEW_SERIES = {
    "pod_heart_rate":       [60.0, 59.0, 59.0, 58.0, 58.0, 57.0, 58.0, 57.0, 58.0, 58.0, 58.0],
    "pod_hrv":              [61.5, 62.0, 62.4, 63.0, 63.2, 63.8, 64.0, 64.1, 63.9, 64.0, 64.2],
    "pod_respiratory_rate": [14.8, 14.8, 14.7, 14.7, 14.6, 14.5, 14.6, 14.6, 14.5, 14.6, 14.6],
    "pod_bed_temp":         [26.0, 26.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0],
    "pod_target_bed_temp":  [25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0],
    "pod_room_temp":        [22.6, 22.6, 22.5, 22.5, 22.5, 22.4, 22.4, 22.4, 22.4, 22.4, 22.4],
}


def build_view(store, cfg, now=None, ref_timestamps=()):
    """Build the Eight Sleep Pod 5 display projection."""
    now = now or adapter.utc_now()
    room_id = cfg["room_id"]
    es_cfg = dict(DEFAULT_ES_CONFIG)
    if isinstance(cfg.get("eight_sleep"), dict):
        es_cfg.update(cfg["eight_sleep"])
    src_id = es_cfg["source_id"]
    mode = es_cfg["mode"]
    stale_after = cfg.get("stale_after_s", 180)

    # Check if we have stored observations for the configured provenance (or LIVE fallback)
    prov = "LIVE" if mode == "LIVE" else ("SYNTHETIC" if mode == "SYNTHETIC" else "LIVE")
    last_attempt = store.latest_attempt(src_id, provenance=prov)

    if mode == "READY" and last_attempt is None:
        # Provide API-ready preview aligned with the room's timeline timestamps so all 6 cards
        # and timeline scrubbing work out-of-the-box before the live account is handed off.
        ts_list = list(ref_timestamps)[-11:] if ref_timestamps else [adapter.iso(now)]
        n_pts = len(ts_list)
        metrics_out = []
        for mid, mdef in sorted(EIGHT_SLEEP_METRICS.items(), key=lambda kv: kv[1]["order"]):
            seq = _PREVIEW_SERIES[mid]
            vals = [seq[min(i, len(seq) - 1)] for i in range(n_pts)]
            trend = [[ts_list[i], vals[i]] for i in range(n_pts)]
            extra = {}
            if mid == "pod_bed_temp":
                extra = {"raw_level": -17, "lookup_note": "Level -17 → 25℃"}
            elif mid == "pod_target_bed_temp":
                extra = {"raw_level": -17, "left_level": -17, "right_level": -8, "lookup_note": "L -17(25℃) · R -8(26℃)"}
            metrics_out.append({
                "metric_id": mid,
                "label_zh": mdef["label_zh"],
                "unit": mdef["unit"],
                "value": vals[-1],
                "observed_or_received_at": ts_list[-1],
                "status": "READY_PREVIEW",
                "status_zh": "待接入预览",
                "source_rule": mdef["source_rule"],
                "trend": trend,
                **extra,
            })
        return {
            "provider": "eight_sleep",
            "device_class": "Pod 5",
            "label_zh": "床微环境",
            "mode": "READY",
            "api_ready": True,
            "status": "READY_NOT_CONNECTED",
            "status_zh": "待接入",
            "metrics": metrics_out,
        }

    # Live or Synthetic stored observations path
    source_row = store.source(src_id)
    if source_row and source_row["status"] == "AUTH_PAUSED":
        conn_status, status_zh = "AUTH_PAUSED", "认证暂停"
    elif last_attempt is None:
        conn_status, status_zh = "NEVER_CONNECTED", "待接入"
    elif last_attempt["outcome"] == "OK":
        conn_status, status_zh = "CONNECTED", "在线"
    else:
        conn_status, status_zh = "DISCONNECTED", "离线"

    since_iso = adapter.iso(now - dt.timedelta(hours=6))
    metrics_out = []
    for mid, mdef in sorted(EIGHT_SLEEP_METRICS.items(), key=lambda kv: kv[1]["order"]):
        good = store.latest_observation(room_id, mid, observed_only=True, provenance=prov)
        pts = store.series(room_id, mid, since_iso, provenance=prov)
        if not pts and good:
            g_dt = dt.datetime.fromisoformat(good["gateway_received_at"].replace("Z", "+00:00"))
            pts = store.series(room_id, mid, adapter.iso(g_dt - dt.timedelta(hours=6)), provenance=prov)
        age = None
        if good:
            g_dt = dt.datetime.fromisoformat(good["gateway_received_at"].replace("Z", "+00:00"))
            age = max(0, int((now - g_dt).total_seconds()))
        m_status = "NO_DATA" if not good else ("CURRENT" if age <= stale_after else "STALE")
        extra = {}
        if mid in ("pod_bed_temp", "pod_target_bed_temp") and good and good["value"] is not None:
            # Reverse lookup approximate level for display badge
            lvl = min(RAW_TO_CELSIUS_MAP.items(), key=lambda kv: abs(kv[1] - good["value"]))[0]
            extra = {"raw_level": lvl, "lookup_note": f"Level {lvl:+d} → {int(round(good['value']))}℃"}
        metrics_out.append({
            "metric_id": mid,
            "label_zh": mdef["label_zh"],
            "unit": mdef["unit"],
            "value": good["value"] if good else None,
            "observed_or_received_at": (good["provider_observed_at"] or good["gateway_received_at"]) if good else None,
            "status": m_status,
            "status_zh": "实时" if m_status == "CURRENT" else ("已过期" if m_status == "STALE" else "暂无数据"),
            "source_rule": mdef["source_rule"],
            "trend": [[p["t"], p["value"]] for p in pts[-120:]],
            **extra,
        })

    return {
        "provider": "eight_sleep",
        "device_class": "Pod 5",
        "label_zh": "Eight Sleep Pod 5",
        "mode": mode,
        "api_ready": True,
        "status": conn_status,
        "status_zh": status_zh,
        "metrics": metrics_out,
    }
