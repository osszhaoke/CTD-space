"""Private runtime config loader (var/ctds-c01/config.json). Strict keys; no secrets in public output.

The config file itself is private (contains the operator-provided device address and optional
Eight Sleep credentials) and must not be committed, zipped or pasted into model context.
"""
import json
import os
import re
from pathlib import Path

from . import adapter, eight_sleep

ROOT = Path(__file__).absolute().parents[3]
DEFAULT_CONFIG = ROOT / "var/ctds-c01/config.json"
OPAQUE_ID = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")
MODES = ("LIVE", "SYNTHETIC")
ES_MODES = ("READY", "SYNTHETIC", "LIVE")
ES_SIDES = ("solo",)

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
        _check(type(doc["source"]) is dict and set(doc["source"]) <= set(src), "invalid source keys")
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
    if src["mode"] == "LIVE":
        try:
            adapter.validate_base_url(src["base_url"])
        except adapter.AdapterError as exc:
            raise ConfigError(str(exc)) from exc
    if os.environ.get("CTDS_TEST_SANDBOX") != "1":
        if es.get("base_url") and not eight_sleep.is_official_8slp_url(es["base_url"]):
            raise ConfigError(f"CONFIG_ERROR: eight_sleep.base_url must be https://*.8slp.net: {es['base_url']}")
        if es.get("auth_url") and not eight_sleep.is_official_8slp_url(es["auth_url"]):
            raise ConfigError(f"CONFIG_ERROR: eight_sleep.auth_url must be https://*.8slp.net: {es['auth_url']}")
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
