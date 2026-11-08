"""Private runtime config loader (var/ctds-c01/config.json). Strict keys; no secrets in public output.

The config file itself is private (contains the operator-provided device address and optional
Eight Sleep credentials) and must not be committed, zipped or pasted into model context.
"""
import ipaddress
import json
import os
import re
from pathlib import Path

from . import adapter, eight_sleep, mqtt_adapter

ROOT = Path(__file__).absolute().parents[3]
DEFAULT_CONFIG = ROOT / "var/ctds-c01/config.json"
OPAQUE_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
MODES = ("LIVE", "SYNTHETIC")
PROTOCOLS = ("http", "mqtt")
MQTT_MODES = ("listen", "subscribe")
ES_MODES = ("READY", "SYNTHETIC", "LIVE")
ES_SIDES = ("solo",)

ALLOWED_SOURCE_KEYS = frozenset({
    "source_id", "mode", "base_url", "binding_revision",
    "protocol", "mqtt_mode", "mqtt_listen_host", "mqtt_listen_port",
    "mqtt_broker_url", "mqtt_expected_sensor_id", "mqtt_username", "mqtt_password",
    "mqtt_inactivity_s", "mqtt_dedup_window_s",
    "cloud_backfill_enabled", "cloud_api_base_url", "cloud_location_id",
    "cloud_api_token", "cloud_earliest_date", "cloud_gap_check_interval_s",
})

ALLOWED_ES_KEYS = frozenset({
    "source_id", "mode", "side", "timezone", "binding_revision",
    "email", "password", "bearer_token", "refresh_token", "user_id", "device_id", "token_expires_at",
    "client_id", "client_secret", "base_url", "auth_url", "stale_after_s"
})

DEFAULTS = {
    "room_id": "demo-room-01",
    "room_name_zh": "睡眠房",
    "db_path": "var/ctds-c01/ctds_c01.sqlite3",
    "listen_port": 8765,
    "poll_interval_s": 60,
    "timeout_s": 5,
    "retries": 2,
    "max_backoff_s": 300,
    "stale_after_s": 180,
    "source": {"source_id": "ag-room-01", "mode": "SYNTHETIC", "base_url": None, "binding_revision": 1},
    "eight_sleep": dict(eight_sleep.DEFAULT_ES_CONFIG),
}

TEMPLATE = dict(DEFAULTS, source=dict(DEFAULTS["source"]), eight_sleep=dict(DEFAULTS["eight_sleep"]))


class ConfigError(Exception):
    pass


def _check(cond, msg):
    if not cond:
        raise ConfigError(msg)


def resolve_path(p):
    p = Path(p)
    res = p if p.is_absolute() else ROOT / p
    if os.environ.get("CTDS_TEST_SANDBOX") == "1":
        real_db = (ROOT / "var/ctds-c01/ctds_c01.sqlite3").resolve()
        try:
            if res.resolve() == real_db:
                sandbox_dir = os.environ.get("CTDS_SANDBOX_DIR")
                if sandbox_dir:
                    return Path(sandbox_dir) / "ctds_test_sandbox.sqlite3"
                return ROOT / "var/ctds-c01/test_sandbox.sqlite3"
        except OSError:
            pass
    return res


def validate(doc):
    _check(type(doc) is dict, "config must be an object")
    _check(set(doc) <= set(DEFAULTS), f"unknown keys: {sorted(set(doc) - set(DEFAULTS))}")
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in doc.items() if k not in ("source", "eight_sleep")})
    src = dict(DEFAULTS["source"])
    if "source" in doc:
        _check(type(doc["source"]) is dict and set(doc["source"]) <= ALLOWED_SOURCE_KEYS, "invalid source keys")
        src.update(doc["source"])
    cfg["source"] = src

    es = dict(DEFAULTS["eight_sleep"])
    if "eight_sleep" in doc and doc["eight_sleep"] is not None:
        _check(type(doc["eight_sleep"]) is dict and set(doc["eight_sleep"]) <= ALLOWED_ES_KEYS, "invalid eight_sleep keys")
        es.update(doc["eight_sleep"])
    cfg["eight_sleep"] = es

    _check(isinstance(cfg["room_id"], str) and OPAQUE_ID.fullmatch(cfg["room_id"]), "room_id must be opaque id")
    _check(isinstance(src["source_id"], str) and OPAQUE_ID.fullmatch(src["source_id"]), "source_id must be opaque id")
    _check(isinstance(es["source_id"], str) and OPAQUE_ID.fullmatch(es["source_id"]), "eight_sleep.source_id must be opaque id")
    _check(isinstance(cfg["room_name_zh"], str) and 1 <= len(cfg["room_name_zh"]) <= 20, "room_name_zh 1..20 chars")
    _check(src["mode"] in MODES, "source.mode must be LIVE or SYNTHETIC")
    _check(es["mode"] in ES_MODES, "eight_sleep.mode must be READY, SYNTHETIC, or LIVE")
    side = str(es.get("side") or "solo").lower()
    if side in ("auto", "left", "right"):
        side = "solo"
    es["side"] = side
    _check(es["side"] in ES_SIDES, "eight_sleep.side must be solo")
    proto = src.get("protocol", "http")
    _check(proto in PROTOCOLS, "source.protocol must be http or mqtt")
    if proto == "mqtt":
        mqtt_mode = src.get("mqtt_mode") or "listen"
        _check(mqtt_mode in MQTT_MODES, "source.mqtt_mode must be listen or subscribe")
        src["mqtt_mode"] = mqtt_mode
        inact = src.get("mqtt_inactivity_s", 240)
        _check(type(inact) is int and 5 <= inact <= 86400, "mqtt_inactivity_s must be int in [5,86400]")
        dedup = src.get("mqtt_dedup_window_s", 15)
        _check(type(dedup) in (int, float) and 0 <= dedup <= 300, "mqtt_dedup_window_s must be in [0,300]")
        exp_id = src.get("mqtt_expected_sensor_id")
        _check(exp_id is None or (isinstance(exp_id, str) and bool(mqtt_adapter.SENSOR_ID_RE.fullmatch(exp_id))),
               "mqtt_expected_sensor_id must be 6..16 hex chars")
        _check(src.get("mqtt_username") is None or isinstance(src["mqtt_username"], str),
               "mqtt_username must be string or null")
        _check(src.get("mqtt_password") is None or isinstance(src["mqtt_password"], str),
               "mqtt_password must be string or null")
        if src.get("base_url") is not None:
            try:
                adapter.validate_base_url(src["base_url"])
            except adapter.AdapterError as exc:
                raise ConfigError(str(exc)) from exc
        if src["mode"] == "LIVE":
            if mqtt_mode == "listen":
                lhost = src.get("mqtt_listen_host") or "0.0.0.0"
                _check(isinstance(lhost, str), "mqtt_listen_host must be string")
                if lhost not in ("0.0.0.0", "::"):
                    try:
                        lip = ipaddress.ip_address(lhost)
                    except ValueError as exc:
                        raise ConfigError("mqtt_listen_host must be 0.0.0.0, ::, loopback, or private IP") from exc
                    _check(lip.is_loopback or lip.is_private, "mqtt_listen_host must be loopback or private IP")
                min_port = 0 if os.environ.get("CTDS_TEST_SANDBOX") == "1" else 1024
                lport = src.get("mqtt_listen_port", 1883)
                _check(type(lport) is int and min_port <= lport <= 65535,
                       f"mqtt_listen_port must be int in [{min_port},65535]")
                src["mqtt_listen_host"] = lhost
                src["mqtt_listen_port"] = lport
            else:
                try:
                    mqtt_adapter.validate_mqtt_broker_url(src.get("mqtt_broker_url"))
                except adapter.AdapterError as exc:
                    raise ConfigError(str(exc)) from exc
    elif src["mode"] == "LIVE":
        try:
            adapter.validate_base_url(src["base_url"])
        except adapter.AdapterError as exc:
            raise ConfigError(str(exc)) from exc
    if os.environ.get("CTDS_TEST_SANDBOX") != "1":
        if es.get("base_url") and not eight_sleep.is_official_8slp_url(es["base_url"]):
            raise ConfigError(f"CONFIG_ERROR: eight_sleep.base_url must be https://*.8slp.net: {es['base_url']}")
        if es.get("auth_url") and not eight_sleep.is_official_8slp_url(es["auth_url"]):
            raise ConfigError(f"CONFIG_ERROR: eight_sleep.auth_url must be https://*.8slp.net: {es['auth_url']}")
    if "cloud_backfill_enabled" in src:
        _check(type(src["cloud_backfill_enabled"]) is bool, "cloud_backfill_enabled must be bool")
    if src.get("cloud_api_base_url") is not None:
        from . import cloud_backfill
        _check(isinstance(src["cloud_api_base_url"], str) and cloud_backfill.is_allowed_cloud_base_url(src["cloud_api_base_url"]),
               "cloud_api_base_url must be https://api.airgradient.com (or loopback in sandbox)")
    if src.get("cloud_location_id") is not None:
        loc_str = str(src["cloud_location_id"]).strip()
        _check(loc_str.isdigit() and 1 <= int(loc_str) <= 999999999, "cloud_location_id must be positive numeric id")
    if src.get("cloud_api_token") is not None:
        tok = src["cloud_api_token"]
        _check(isinstance(tok, str) and 8 <= len(tok) <= 128 and not any(c.isspace() for c in tok),
               "cloud_api_token must be 8..128 non-whitespace chars")
    if src.get("cloud_earliest_date") is not None:
        from . import cloud_backfill
        try:
            cloud_backfill.parse_cloud_timestamp(src["cloud_earliest_date"])
        except adapter.AdapterError as exc:
            raise ConfigError(f"invalid cloud_earliest_date: {exc}") from exc
    if "cloud_gap_check_interval_s" in src:
        _check(type(src["cloud_gap_check_interval_s"]) is int and 60 <= src["cloud_gap_check_interval_s"] <= 86400,
               "cloud_gap_check_interval_s must be int in [60,86400]")
    if src.get("cloud_backfill_enabled"):
        _check(src.get("cloud_location_id") is not None and src.get("cloud_api_token"),
               "cloud_backfill_enabled requires cloud_location_id and cloud_api_token")
    for k, lo, hi in (("listen_port", 1024, 65535), ("poll_interval_s", 10, 3600), ("timeout_s", 1, 30),
                      ("retries", 0, 5), ("max_backoff_s", 10, 3600), ("stale_after_s", 30, 86400)):
        _check(type(cfg[k]) is int and lo <= cfg[k] <= hi, f"{k} must be int in [{lo},{hi}]")
    if "stale_after_s" in es:
        _check(type(es["stale_after_s"]) is int and 30 <= es["stale_after_s"] <= 86400, "eight_sleep.stale_after_s must be int in [30,86400]")
    _check(type(src["binding_revision"]) is int and src["binding_revision"] >= 1, "binding_revision >= 1")
    _check(type(es["binding_revision"]) is int and es["binding_revision"] >= 1, "eight_sleep.binding_revision >= 1")
    return cfg


def migrate_doc(doc):
    """Normalize config formatting (e.g. side to solo). Retains credentials and LIVE mode per Decision B."""
    if not isinstance(doc, dict):
        return False
    changed = False
    es = doc.get("eight_sleep")
    if isinstance(es, dict):
        side = str(es.get("side") or "solo").lower()
        if side in ("auto", "left", "right"):
            es["side"] = "solo"
            changed = True
    return changed


def init_config(path=None, force=False):
    """Explicitly create config from example template if absent (does not overwrite unless force=True)."""
    p = Path(path) if path else DEFAULT_CONFIG
    if p.exists() and not force:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    example_p = p.parent / "config.example.json"
    if example_p.exists():
        p.write_text(example_p.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        p.write_text(json.dumps(TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return True


def save_config(doc, path=None):
    """Atomically write and chmod 600 config file."""
    if isinstance(doc, (str, Path)) and isinstance(path, dict):
        doc, path = path, doc
    p = Path(path) if path else DEFAULT_CONFIG
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return True


def migrate_config_file(path=None):
    """Normalize config formatting and write to disk atomically with chmod 600."""
    p = Path(path) if path else DEFAULT_CONFIG
    if not p.exists():
        return False
    doc = json.loads(p.read_text(encoding="utf-8"))
    if migrate_doc(doc):
        save_config(doc, p)
        return True
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return False


def load(path=None):
    """Strictly read-only config loader. Parses, validates, and normalizes without writing to disk."""
    p = Path(path) if path else DEFAULT_CONFIG
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {p}") from exc
    except ValueError as exc:
        raise ConfigError("config is not valid JSON") from exc

    migrate_doc(doc)
    return validate(doc)
