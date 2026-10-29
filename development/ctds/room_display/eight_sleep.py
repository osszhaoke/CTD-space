"""Eight Sleep Pod 6 read-only API client, heating level lookup table, and 17-metric normalizer.

Collects bed microenvironment metrics (per-side bed/target temperatures, heating state, all-night mean
bed temperature, room temperature, adjustable base angles) plus sleep session metrics into the local
private SQLite store (per Decision B). Personal physiological metrics (heart rate, HRV, respiratory rate)
are strictly excluded from the public cockpit UI and AI diagnostic export.

Reference: lukas-clarke/pyEight (MIT License) constants & user/device/temperature/trends endpoints.
"""
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import logging

from . import adapter

_log = logging.getLogger("ctds.c01.eight_sleep")

MAPPING_VERSION = "ctds.c01.eight-sleep.mapping/0.1.0"
DOC_REF = "lukas-clarke/pyEight@1.0.18 (MIT) / Home Assistant eight_sleep integration"
DEFAULT_CLIENT_API_URL = "https://client-api.8slp.net/v1"
DEFAULT_AUTH_URL = "https://auth-api.8slp.net/v1/tokens"
DEFAULT_APP_API_URL = "https://app-api.8slp.net/v1"   # only app-api exposes temperature.currentDeviceLevel
TRENDS_LOOKBACK_DAYS = 3                              # trends requires tz/from/to (HTTP 400 without them)
DEVICE_FIELDS = ("leftUserId", "rightUserId", "leftHeatingLevel", "rightHeatingLevel",
                 "leftTargetHeatingLevel", "rightTargetHeatingLevel", "leftNowHeating", "rightNowHeating")


def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return dt.timezone(dt.timedelta(hours=8))
DEFAULT_CLIENT_ID = "0894c7f33bb94800a03f1f4df13a4f38"
DEFAULT_CLIENT_SECRET = "f0954a3ed5763ba3d06834c73731a32f15f168f47d4f164751275def86db0c76"


def is_official_8slp_url(url_str):
    """Validate that a URL uses HTTPS and belongs to the official *.8slp.net domain."""
    if not url_str or not isinstance(url_str, str):
        return False
    parsed = urllib.parse.urlparse(url_str)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "8slp.net" or host.endswith(".8slp.net")

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
    # Per-side bed temperature, heating state and base angles (device/base APIs report both sides).
    "pod_left_bed_temp": {"label_zh": "左侧床温", "unit": "℃", "range": (10, 50), "order": 7,
                          "source_rule": "devices.leftHeatingLevel -> RAW_TO_CELSIUS_MAP"},
    "pod_left_target_temp": {"label_zh": "左侧目标床温", "unit": "℃", "range": (10, 50), "order": 8,
                             "source_rule": "devices.leftTargetHeatingLevel -> RAW_TO_CELSIUS_MAP"},
    "pod_right_bed_temp": {"label_zh": "右侧床温", "unit": "℃", "range": (10, 50), "order": 9,
                           "source_rule": "devices.rightHeatingLevel -> RAW_TO_CELSIUS_MAP"},
    "pod_right_target_temp": {"label_zh": "右侧目标床温", "unit": "℃", "range": (10, 50), "order": 10,
                              "source_rule": "devices.rightTargetHeatingLevel -> RAW_TO_CELSIUS_MAP"},
    "pod_left_heating": {"label_zh": "左侧温控运行", "unit": "", "range": (0, 1), "order": 11,
                         "source_rule": "devices.leftNowHeating (1 = running)"},
    "pod_right_heating": {"label_zh": "右侧温控运行", "unit": "", "range": (0, 1), "order": 12,
                          "source_rule": "devices.rightNowHeating (1 = running)"},
    "pod_night_bed_temp": {"label_zh": "全夜床体温度", "unit": "℃", "range": (10, 50), "order": 13,
                           "source_rule": "trends latest session mean(timeseries.tempBedC)"},
    "pod_left_torso_angle": {"label_zh": "左侧背部抬升", "unit": "°", "range": (0, 90), "order": 14,
                             "source_rule": "base.left.torso.currentAngle"},
    "pod_left_leg_angle": {"label_zh": "左侧腿部抬升", "unit": "°", "range": (0, 90), "order": 15,
                           "source_rule": "base.left.leg.currentAngle"},
    "pod_right_torso_angle": {"label_zh": "右侧背部抬升", "unit": "°", "range": (0, 90), "order": 16,
                              "source_rule": "base.right.torso.currentAngle"},
    "pod_right_leg_angle": {"label_zh": "右侧腿部抬升", "unit": "°", "range": (0, 90), "order": 17,
                            "source_rule": "base.right.leg.currentAngle"},
}
SIDE_TEMP_METRICS = ("pod_bed_temp", "pod_target_bed_temp", "pod_left_bed_temp", "pod_left_target_temp",
                     "pod_right_bed_temp", "pod_right_target_temp")

CARD_TREND_WINDOW = dt.timedelta(hours=1)  # small cards show the last hour (same as projection.TREND_WINDOW)

DEFAULT_ES_STALE_AFTER_S = 360
DEFAULT_ES_CONFIG = {
    "source_id": "pod-room-01",
    "mode": "READY",  # "READY" | "SYNTHETIC" (LIVE disabled per Decision A)
    "side": "solo",
    "timezone": "Asia/Shanghai",
    "binding_revision": 1,
    "stale_after_s": DEFAULT_ES_STALE_AFTER_S,
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


def _resolve_side(dev_res, side, user_id):
    """Pod 6 Solo: pick the user's own side from the device payload (never exposed downstream)."""
    norm = (side or "solo").lower()
    if norm in ("left", "right"):
        return norm
    if user_id is not None:
        for cand in ("left", "right"):
            if dev_res.get(f"{cand}UserId") == user_id:
                return cand
    return "left" if "leftTargetHeatingLevel" in dev_res or "leftHeatingLevel" in dev_res else None


def _level(v):
    return int(round(float(v))) if _is_num(v) else None


def _session_mean(sessions, key):
    """Mean of timeseries[key] in the latest session that has it -> (last_ts, mean)."""
    for sess in reversed(sessions if isinstance(sessions, list) else []):
        seq = (sess.get("timeseries") or {}).get(key) if isinstance(sess, dict) else None
        pts = [p for p in (seq or []) if isinstance(p, (list, tuple)) and len(p) >= 2 and _is_num(p[1])]
        if pts:
            return str(pts[-1][0]), round(sum(float(p[1]) for p in pts) / len(pts), 1)
    return None, None


def _angle(base_doc, side, part):
    v = (((base_doc or {}).get(side) or {}).get(part) or {}).get("currentAngle") if isinstance(base_doc, dict) else None
    return float(v) if _is_num(v) else None


def extract_six_metrics(trends_doc, temp_doc, device_doc, *, side="solo", user_id=None, base_doc=None):
    """Extract the 6 Eight Sleep metrics and metadata from raw API documents (Solo single-zone alignment)."""
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

    # 4/5. 床温与目标床温：设备接口按侧返回 {left,right}HeatingLevel / TargetHeatingLevel，
    #      只取本人一侧（Solo）；app-api temperature.currentDeviceLevel 优先作为当前床温。
    dev_res = device_doc.get("result") if isinstance(device_doc, dict) and isinstance(device_doc.get("result"), dict) else (device_doc if isinstance(device_doc, dict) else {})
    own = _resolve_side(dev_res, side, user_id)
    temp = temp_doc if isinstance(temp_doc, dict) else {}

    raw_device_level = _level(temp.get("currentDeviceLevel"))
    if raw_device_level is None and own:
        raw_device_level = _level(dev_res.get(f"{own}HeatingLevel"))
    bed_temp_c = heating_level_to_temp(raw_device_level)

    target_lvl = _level(dev_res.get(f"{own}TargetHeatingLevel")) if own else None
    if target_lvl is None:
        target_lvl = _level(dev_res.get("targetHeatingLevel"))
    if target_lvl is None:
        target_lvl = _level(temp.get("currentLevel"))
    target_bed_temp_c = heating_level_to_temp(target_lvl)

    # 7+. 两侧床温 / 目标 / 运行状态（设备接口两侧都报）、全夜床体温度（会话均值）、底座抬升角度
    side_metrics = {}
    for sd in ("left", "right"):
        cur_lvl, tgt_lvl = _level(dev_res.get(f"{sd}HeatingLevel")), _level(dev_res.get(f"{sd}TargetHeatingLevel"))
        side_metrics[f"pod_{sd}_bed_temp"] = {"value": heating_level_to_temp(cur_lvl), "observed_at": None, "raw_level": cur_lvl, "trend": []}
        side_metrics[f"pod_{sd}_target_temp"] = {"value": heating_level_to_temp(tgt_lvl), "observed_at": None, "raw_level": tgt_lvl, "trend": []}
        heating = dev_res.get(f"{sd}NowHeating")
        side_metrics[f"pod_{sd}_heating"] = {"value": (1.0 if heating else 0.0) if isinstance(heating, bool) else None,
                                             "observed_at": None, "trend": []}
        for part in ("torso", "leg"):
            side_metrics[f"pod_{sd}_{part}_angle"] = {"value": _angle(base_doc, sd, part), "observed_at": None, "trend": []}
    nb_ts, nb_val = _session_mean(sessions, "tempBedC")
    side_metrics["pod_night_bed_temp"] = {"value": nb_val, "observed_at": nb_ts, "trend": []}

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
                "raw_level": target_lvl,
                "trend": [],
            },
            "pod_room_temp": {"value": rt_val, "observed_at": rt_ts, "trend": rt_trend},
            **side_metrics,
        },
        "meta": {
            "raw_device_level": raw_device_level,
            "target_heating_level": target_lvl,
        },
    }


def normalize_bundle(trends_doc, temp_doc, device_doc, *, source_id, room_id, attempt_id, received_at, provenance, side="solo", user_id=None, base_doc=None):
    """Normalize Eight Sleep API payloads into append-only Store observation rows."""
    if provenance not in ("LIVE", "REPLAY", "SYNTHETIC"):
        raise adapter.AdapterError("INVALID_PROVENANCE", provenance)

    extracted = extract_six_metrics(trends_doc, temp_doc, device_doc, side=side, user_id=user_id, base_doc=base_doc)
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
    """Eight Sleep read-only API client with token auto-refresh and password re-auth."""

    def __init__(
        self,
        *,
        email=None,
        password=None,
        bearer_token=None,
        refresh_token=None,
        user_id=None,
        device_id=None,
        token_expires_at=None,
        side="solo",
        client_id=None,
        client_secret=None,
        timeout_s=10.0,
        clock=adapter.utc_now,
        base_url=DEFAULT_CLIENT_API_URL,
        auth_url=DEFAULT_AUTH_URL,
        app_url=None,
        timezone="Asia/Shanghai",
        on_token_refreshed=None,
    ):
        self.email = email
        self.password = password
        self.bearer_token = bearer_token
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.device_id = device_id
        self.token_expires_at = token_expires_at
        self.side = side or "solo"
        self.client_id = client_id or DEFAULT_CLIENT_ID
        self.client_secret = client_secret or DEFAULT_CLIENT_SECRET
        self.timeout_s = timeout_s
        self.clock = clock
        self.base_url = (base_url or DEFAULT_CLIENT_API_URL).rstrip("/")
        self.auth_url = (auth_url or DEFAULT_AUTH_URL).rstrip("/")
        if app_url:
            self.app_url = app_url.rstrip("/")
        elif self.base_url == DEFAULT_CLIENT_API_URL:
            self.app_url = DEFAULT_APP_API_URL
        else:  # test / proxy base: app endpoints served under <base>/app
            self.app_url = f"{self.base_url}/app"
        self.timezone = timezone or "Asia/Shanghai"
        if os.environ.get("CTDS_TEST_SANDBOX") != "1":
            if not all(is_official_8slp_url(u) for u in (self.base_url, self.auth_url, self.app_url)):
                raise adapter.AdapterError(
                    "CONFIG_ERROR",
                    f"Eight Sleep URLs must be https://*.8slp.net (base_url={self.base_url}, auth_url={self.auth_url})",
                )
        self.on_token_refreshed = on_token_refreshed
        self.last_request_time = 0.0
        self.ssl_context = adapter.get_ssl_context()

    @classmethod
    def from_config(cls, es_cfg, clock=adapter.utc_now, config_path=None):
        if not isinstance(es_cfg, dict):
            es_cfg = {}

        def _persist_callback(auth_state):
            if config_path:
                try:
                    p = Path(config_path)
                    if p.exists():
                        doc = json.loads(p.read_text(encoding="utf-8"))
                        if "eight_sleep" in doc and isinstance(doc["eight_sleep"], dict):
                            doc["eight_sleep"].update(auth_state)
                            from . import config as _cfg
                            _cfg.save_config(doc, p)
                except Exception as exc:
                    _log.warning(
                        "Failed to persist Eight Sleep auth state: %s", type(exc).__name__
                    )

        return cls(
            email=es_cfg.get("email"),
            password=es_cfg.get("password"),
            bearer_token=es_cfg.get("bearer_token"),
            refresh_token=es_cfg.get("refresh_token"),
            user_id=es_cfg.get("user_id"),
            device_id=es_cfg.get("device_id"),
            token_expires_at=es_cfg.get("token_expires_at"),
            side=es_cfg.get("side", "solo"),
            client_id=es_cfg.get("client_id"),
            client_secret=es_cfg.get("client_secret"),
            base_url=es_cfg.get("base_url") or DEFAULT_CLIENT_API_URL,
            auth_url=es_cfg.get("auth_url") or DEFAULT_AUTH_URL,
            timezone=es_cfg.get("timezone") or "Asia/Shanghai",
            clock=clock,
            on_token_refreshed=_persist_callback,
        )

    def get_auth_state(self):
        state = {}
        if self.bearer_token:
            state["bearer_token"] = self.bearer_token
        if self.refresh_token:
            state["refresh_token"] = self.refresh_token
        if self.token_expires_at:
            state["token_expires_at"] = self.token_expires_at
        if self.user_id:
            state["user_id"] = self.user_id
        if self.device_id:
            state["device_id"] = self.device_id
        if self.side:
            state["side"] = self.side
        return state

    def authenticate(self, email=None, password=None):
        """Exchange email & password for Bearer token via Eight Sleep auth API (OAuth2 password grant)."""
        e = email or self.email
        p = password or self.password
        if not e or not p:
            raise adapter.AdapterError("AUTH_REJECTED", "Missing Eight Sleep email or password")

        payload_dict = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "password",
            "username": e,
            "password": p,
        }
        payload = json.dumps(payload_dict).encode("utf-8")
        req = urllib.request.Request(
            self.auth_url,
            data=payload,
            headers={
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": "Home Assistant 1.0.18",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s, context=self.ssl_context) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (401, 403):
                raise adapter.AdapterError("AUTH_REJECTED", f"Authentication failed: HTTP {code}") from exc
            raise adapter.AdapterError("HTTP_ERROR", f"Auth HTTP error: {code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise adapter.AdapterError("NO_CONNECTION", f"Auth network error: {exc}") from exc

        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        self.bearer_token = data.get("access_token") or data.get("token") or session.get("token")
        self.refresh_token = data.get("refresh_token") or session.get("refreshToken") or self.refresh_token

        if "expires_in" in data and isinstance(data["expires_in"], (int, float)):
            expires_dt = self.clock() + dt.timedelta(seconds=int(data["expires_in"]))
            self.token_expires_at = adapter.iso(expires_dt)
        else:
            self.token_expires_at = data.get("expirationDate") or session.get("expirationDate")

        uid = data.get("userId") or session.get("userId") or data.get("user_id")
        if uid:
            self.user_id = str(uid)

        if not self.bearer_token:
            raise adapter.AdapterError("AUTH_REJECTED", "Auth response did not contain access token")

        if self.on_token_refreshed:
            self.on_token_refreshed(self.get_auth_state())
        return self.bearer_token

    def refresh_auth(self):
        """Refresh order: 1. Try refresh_token (grant_type=refresh_token), 2. Try email+password re-auth, 3. Raise AUTH_REJECTED."""
        if self.refresh_token:
            try:
                payload_dict = {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                }
                payload = json.dumps(payload_dict).encode("utf-8")
                req = urllib.request.Request(
                    self.auth_url,
                    data=payload,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json",
                        "user-agent": "Home Assistant 1.0.18",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout_s, context=self.ssl_context) as resp:
                    data = json.loads(resp.read().decode("utf-8"))

                session = data.get("session") if isinstance(data.get("session"), dict) else {}
                new_token = data.get("access_token") or data.get("token") or session.get("token")
                if new_token:
                    self.bearer_token = new_token
                    self.refresh_token = data.get("refresh_token") or session.get("refreshToken") or self.refresh_token
                    if "expires_in" in data and isinstance(data["expires_in"], (int, float)):
                        expires_dt = self.clock() + dt.timedelta(seconds=int(data["expires_in"]))
                        self.token_expires_at = adapter.iso(expires_dt)
                    else:
                        self.token_expires_at = data.get("expirationDate") or session.get("expirationDate") or self.token_expires_at

                    uid = data.get("userId") or session.get("userId") or data.get("user_id")
                    if uid:
                        self.user_id = str(uid)

                    if self.on_token_refreshed:
                        self.on_token_refreshed(self.get_auth_state())
                    return self.bearer_token
            except urllib.error.HTTPError as exc:
                exc.close()
                _log.warning("Eight Sleep token refresh failed: HTTPError")
            except Exception as exc:
                _log.warning(
                    "Eight Sleep token refresh failed: %s", type(exc).__name__
                )

        if self.email and self.password:
            try:
                return self.authenticate()
            except adapter.AdapterError:
                raise
            except Exception as exc:
                raise adapter.AdapterError("AUTH_REJECTED", f"Re-auth failed: {exc}") from exc

        raise adapter.AdapterError("AUTH_REJECTED", "Eight Sleep credentials missing or expired")

    def discover(self):
        """Auto-discover user_id and device_id."""
        if not self.bearer_token:
            self.refresh_auth()

        if not self.user_id:
            me_doc = self._get(f"{self.base_url}/users/me")
            u = me_doc.get("user") or me_doc
            self.user_id = u.get("userId") or u.get("id")

        if not self.device_id and self.user_id:
            try:
                u_doc = self._get(f"{self.base_url}/users/{self.user_id}")
                user_obj = u_doc.get("user") or u_doc
                devs = user_obj.get("devices") or []
                self.device_id = user_obj.get("currentDeviceId") or (devs[0] if devs else None)
            except Exception:
                pass

        if not self.device_id:
            try:
                me_doc = self._get(f"{self.base_url}/users/me")
                user_obj = me_doc.get("user") or me_doc
                devs = user_obj.get("devices") or []
                self.device_id = user_obj.get("currentDeviceId") or (devs[0] if devs else None)
            except Exception:
                pass

        if self.on_token_refreshed:
            self.on_token_refreshed(self.get_auth_state())
        return self.user_id, self.device_id

    def _get(self, url, retry_401=True, label="request"):
        """Execute read-only GET request with automatic 401 token refresh.

        Failures carry the real HTTP status (exc.http_status) and the endpoint label (exc.endpoint)."""
        if not self.bearer_token:
            self.refresh_auth()

        now_ts = time.time()
        if now_ts - self.last_request_time < 0.2:
            time.sleep(0.2)
        self.last_request_time = time.time()

        req = urllib.request.Request(
            url,
            headers={
                "authorization": f"Bearer {self.bearer_token}",
                "accept": "application/json",
                "user-agent": "Home Assistant 1.0.18",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s, context=self.ssl_context) as resp:
                data = resp.read()
                if data[:2] == b"\x1f\x8b":
                    try:
                        data = gzip.decompress(data)
                    except Exception as exc:
                        err = adapter.AdapterError("INVALID_PAYLOAD", f"{label}: gzip decompress failed: {type(exc).__name__}")
                        err.endpoint = label
                        raise err from exc
                try:
                    return json.loads(data.decode("utf-8"))
                except Exception as exc:
                    err = adapter.AdapterError("INVALID_PAYLOAD", f"{label}: JSON decode failed: {type(exc).__name__}")
                    err.endpoint = label
                    raise err from exc
        except adapter.AdapterError:
            raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 401 and retry_401:
                self.refresh_auth()
                return self._get(url, retry_401=False, label=label)
            if code in (401, 403):
                err = adapter.AdapterError("AUTH_REJECTED", f"{label}: API request unauthorized: HTTP {code}")
            elif code == 429:
                err = adapter.AdapterError("HTTP_ERROR", f"{label}: Eight Sleep rate limited (429)")
            else:
                err = adapter.AdapterError("HTTP_ERROR", f"{label}: Eight Sleep HTTP {code}")
            err.http_status, err.endpoint = code, label
            _log.warning("Eight Sleep %s failed: HTTP %s", label, code)
            raise err from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            err = adapter.AdapterError("NO_CONNECTION", f"{label}: Eight Sleep connection error: {type(exc).__name__}")
            err.endpoint = label
            raise err from exc

    def fetch_bundle(self):
        """Fetch trends, temperature and device docs (strictly GET read-only)."""
        start_t = self.clock()
        try:
            if not self.bearer_token:
                self.refresh_auth()
            if not self.user_id or not self.device_id:
                self.discover()

            today = self.clock().astimezone(_tz(self.timezone)).date()
            query = urllib.parse.urlencode({
                "tz": self.timezone, "from": (today - dt.timedelta(days=TRENDS_LOOKBACK_DAYS)).isoformat(),
                "to": today.isoformat(), "include-main": "false", "include-all-sessions": "true", "model-version": "v2",
            })
            trends_doc = self._get(f"{self.base_url}/users/{self.user_id}/trends?{query}", label="trends")
            temp_doc = self._get(f"{self.app_url}/users/{self.user_id}/temperature", label="temperature")
            device_doc = self._get(f"{self.base_url}/devices/{self.device_id}?filter=" + ",".join(DEVICE_FIELDS), label="device")
            base_doc = None
            try:  # optional: pods without an adjustable base return 404
                raw_base = self._get(f"{self.app_url}/users/{self.user_id}/base", label="base")
                base_doc = {sd: {part: {"currentAngle": ((raw_base.get(sd) or {}).get(part) or {}).get("currentAngle")}
                                 for part in ("torso", "leg")} for sd in ("left", "right")} if isinstance(raw_base, dict) else None
            except adapter.AdapterError as exc:
                if exc.code == "AUTH_REJECTED":
                    raise

            return {
                "outcome": "OK",
                "http_status": 200,
                "request_started_at": start_t,
                "response_received_at": self.clock(),
                "side": self.side,
                "user_id": self.user_id,
                "trends_doc": trends_doc,
                "base_doc": base_doc,
                "temp_doc": temp_doc,
                "device_doc": device_doc,
            }
        except adapter.AdapterError as exc:
            return {
                "outcome": exc.code,
                "http_status": getattr(exc, "http_status", None) or (401 if exc.code == "AUTH_REJECTED" else None),
                "request_started_at": start_t,
                "response_received_at": self.clock(),
                "side": self.side,
            }


def make_synthetic_bundle(clock=adapter.utc_now, step_idx=0):
    """Generate a realistic synthetic Eight Sleep bundle for testing and READY preview."""
    now = clock()
    base_ts = now - dt.timedelta(minutes=10)
    hr_series = []
    rt_series = []
    bed_series = []
    for i in range(11):
        ts = adapter.iso(base_ts + dt.timedelta(minutes=i))
        hr_series.append([ts, round(58.0 + ((i + step_idx) % 4) - 1.0, 1)])
        rt_series.append([ts, round(22.4 + ((i % 3) * 0.1), 1)])
        bed_series.append([ts, round(26.0 + ((i % 4) * 0.2), 1)])
    return {
        "outcome": "OK",
        "http_status": 200,
        "request_started_at": now,
        "response_received_at": now,
        "side": "solo",
        "temp_doc": {
            "currentDeviceLevel": -17,  # maps to 25°C via RAW_TO_CELSIUS_MAP
            "currentLevel": -17,
            "currentState": {"type": "smart"},
        },
        "user_id": "synthetic-user",
        "device_doc": {  # same shape as client-api /devices/{id}?filter=… ; only the user's own side is read
            "result": {
                "leftUserId": "synthetic-user",
                "leftHeatingLevel": -17,          # maps to 25°C
                "leftTargetHeatingLevel": -17,
                "leftNowHeating": True,
                "rightHeatingLevel": -40,
                "rightTargetHeatingLevel": -30,
                "rightNowHeating": False,
            }
        },
        "base_doc": {"left": {"torso": {"currentAngle": 10}, "leg": {"currentAngle": 5}},
                     "right": {"torso": {"currentAngle": 0}, "leg": {"currentAngle": 0}}},
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
                                "tempBedC": bed_series,
                            }
                        }
                    ],
                }
            ]
        },
    }


def ingest_once(store, cfg, *, client=None, provenance=None, clock=adapter.utc_now, config_path=None):
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
        device_class="Pod 6",
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
        client_inst = EightSleepClient.from_config(es_cfg, clock=clock, config_path=config_path)
        res = client_inst.fetch_bundle()
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
                side=res.get("side") or es_cfg.get("side", "solo"),
                user_id=res.get("user_id") or es_cfg.get("user_id"),
                base_doc=res.get("base_doc"),
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


def build_view(store, cfg, now=None, ref_timestamps=()):
    """Build the Eight Sleep Pod 6 display projection (READY, SYNTHETIC, or LIVE)."""
    now = now or adapter.utc_now()
    room_id = cfg["room_id"]
    es_cfg = dict(DEFAULT_ES_CONFIG)
    if isinstance(cfg.get("eight_sleep"), dict):
        es_cfg.update(cfg["eight_sleep"])
    src_id = es_cfg["source_id"]
    mode = es_cfg["mode"]
    stale_after = int(es_cfg.get("stale_after_s", DEFAULT_ES_STALE_AFTER_S))

    # In READY mode, values and trends are empty per P0-4 (zero fake data)
    if mode == "READY":
        metrics_out = []
        for mid, mdef in sorted(EIGHT_SLEEP_METRICS.items(), key=lambda kv: kv[1]["order"]):
            metrics_out.append({
                "metric_id": mid,
                "label_zh": mdef["label_zh"],
                "unit": mdef["unit"],
                "value": None,
                "observed_or_received_at": None,
                "status": "NO_OBSERVATION",
                "status_zh": "待接入",
                "source_rule": mdef["source_rule"],
                "trend": [],
            })
        return {
            "provider": "eight_sleep",
            "device_class": "Pod 6",
            "label_zh": "床微环境",
            "mode": "READY",
            "api_ready": True,
            "status": "READY_NOT_CONNECTED",
            "status_zh": "待接入",
            "metrics": metrics_out,
        }

    # Synthetic or Live stored observations path
    prov = "LIVE" if mode == "LIVE" else "SYNTHETIC"
    last_attempt = store.latest_attempt(src_id, provenance=prov)
    source_row = store.source(src_id)
    if source_row and source_row["status"] == "AUTH_PAUSED":
        conn_status, status_zh = "AUTH_PAUSED", "认证失效"
    elif last_attempt is None:
        conn_status, status_zh = "NEVER_CONNECTED", "待接入"
    elif last_attempt["outcome"] == "OK":
        conn_status, status_zh = "CONNECTED", "在线"
    else:
        conn_status, status_zh = "DISCONNECTED", "离线"

    since_iso = adapter.iso(now - CARD_TREND_WINDOW)
    metrics_out = []
    for mid, mdef in sorted(EIGHT_SLEEP_METRICS.items(), key=lambda kv: kv[1]["order"]):
        good = store.latest_observation(room_id, mid, observed_only=True, provenance=prov)
        pts = store.series(room_id, mid, since_iso, provenance=prov)
        if not pts and good:
            g_dt = dt.datetime.fromisoformat(good["gateway_received_at"].replace("Z", "+00:00"))
            pts = store.series(room_id, mid, adapter.iso(g_dt - CARD_TREND_WINDOW), provenance=prov)
        age = None
        if good:
            g_dt = dt.datetime.fromisoformat(good["gateway_received_at"].replace("Z", "+00:00"))
            age = max(0, int((now - g_dt).total_seconds()))
        m_status = "NO_DATA" if not good else ("CURRENT" if age <= stale_after else "STALE")
        extra = {}
        if mid in SIDE_TEMP_METRICS and good and good["value"] is not None:
            lvl = min(RAW_TO_CELSIUS_MAP.items(), key=lambda kv: abs(kv[1] - good["value"]))[0]
            extra = {"raw_level": lvl, "lookup_note": f"Level {lvl:+d} → {int(round(good['value']))}℃"}
        metrics_out.append({
            "metric_id": mid,
            "label_zh": mdef["label_zh"],
            "unit": mdef["unit"],
            "value": good["value"] if good else None,
            "observed_or_received_at": (good["provider_observed_at"] or good["gateway_received_at"]) if good else None,
            "age_s": age,
            "status": m_status,
            "status_zh": "实时" if m_status == "CURRENT" else ("已过期" if m_status == "STALE" else "暂无数据"),
            "source_rule": mdef["source_rule"],
            "trend": [[p["t"], p["value"]] for p in pts[-120:]],
            **extra,
        })

    return {
        "provider": "eight_sleep",
        "device_class": "Pod 6",
        "label_zh": "Eight Sleep Pod 6",
        "mode": mode,
        "api_ready": True,
        "status": conn_status,
        "status_zh": status_zh,
        "metrics": metrics_out,
    }
