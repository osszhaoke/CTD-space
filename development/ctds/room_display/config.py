"""Private runtime config loader (var/ctds-c01/config.json). Strict keys; no secrets in public output.

The config file itself is private (contains the operator-provided device address and optional
Eight Sleep credentials) and must not be committed, zipped or pasted into model context.
"""
import json
import re
from pathlib import Path

from . import adapter, eight_sleep

ROOT = Path(__file__).absolute().parents[3]
DEFAULT_CONFIG = ROOT / "var/ctds-c01/config.json"
OPAQUE_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
MODES = ("LIVE", "SYNTHETIC")
ES_MODES = ("READY", "SYNTHETIC", "LIVE")
ES_SIDES = ("auto", "left", "right", "solo")

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
    return p if p.is_absolute() else ROOT / p


def validate(doc):
    _check(type(doc) is dict, "config must be an object")
    _check(set(doc) <= set(DEFAULTS), f"unknown keys: {sorted(set(doc) - set(DEFAULTS))}")
    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in doc.items() if k not in ("source", "eight_sleep")})
    src = dict(DEFAULTS["source"])
    if "source" in doc:
        _check(type(doc["source"]) is dict and set(doc["source"]) <= set(src), "invalid source keys")
        src.update(doc["source"])
    cfg["source"] = src

    es = dict(DEFAULTS["eight_sleep"])
    if "eight_sleep" in doc and doc["eight_sleep"] is not None:
        _check(type(doc["eight_sleep"]) is dict and set(doc["eight_sleep"]) <= set(es), "invalid eight_sleep keys")
        es.update(doc["eight_sleep"])
    cfg["eight_sleep"] = es

    _check(isinstance(cfg["room_id"], str) and OPAQUE_ID.fullmatch(cfg["room_id"]), "room_id must be opaque id")
    _check(isinstance(src["source_id"], str) and OPAQUE_ID.fullmatch(src["source_id"]), "source_id must be opaque id")
    _check(isinstance(es["source_id"], str) and OPAQUE_ID.fullmatch(es["source_id"]), "eight_sleep.source_id must be opaque id")
    _check(isinstance(cfg["room_name_zh"], str) and 1 <= len(cfg["room_name_zh"]) <= 20, "room_name_zh 1..20 chars")
    _check(src["mode"] in MODES, "source.mode must be LIVE or SYNTHETIC")
    _check(es["mode"] in ES_MODES, "eight_sleep.mode must be READY, SYNTHETIC, or LIVE")
    _check(str(es.get("side") or "auto").lower() in ES_SIDES, "eight_sleep.side must be auto, left, right, or solo")
    if src["mode"] == "LIVE":
        try:
            adapter.validate_base_url(src["base_url"])
        except adapter.AdapterError as exc:
            raise ConfigError(str(exc)) from exc
    for k, lo, hi in (("listen_port", 1024, 65535), ("poll_interval_s", 10, 3600), ("timeout_s", 1, 30),
                      ("retries", 0, 5), ("max_backoff_s", 10, 3600), ("stale_after_s", 30, 86400)):
        _check(type(cfg[k]) is int and lo <= cfg[k] <= hi, f"{k} must be int in [{lo},{hi}]")
    _check(type(src["binding_revision"]) is int and src["binding_revision"] >= 1, "binding_revision >= 1")
    _check(type(es["binding_revision"]) is int and es["binding_revision"] >= 1, "eight_sleep.binding_revision >= 1")
    return cfg


def load(path=None):
    p = Path(path) if path else DEFAULT_CONFIG
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {p}") from exc
    except ValueError as exc:
        raise ConfigError("config is not valid JSON") from exc
    return validate(doc)
