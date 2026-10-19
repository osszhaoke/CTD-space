#!/usr/bin/env python3
"""Build a portable Mac one-click deployment bundle (var/ctds-c01/StarMap-C01-Mac-OneClick.zip).

Includes:
- 一键部署_StarMap.command (double-clickable macOS launcher)
- delivery/scripts/deploy_mac.sh
- development/ctds/room_display/ and development/ctds/tests/room_display/
- docs/operations/DEPLOYMENT_AND_RUNBOOK_ZH.md
- Sanitized offline baseline DB (var/ctds-c01/seed_frozen.sqlite3, no IP/MAC/serialno)
- Sanitized seed config (var/ctds-c01/seed_config.json with RFC5737 placeholder IP, auto-updated by LAN discovery)
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT_ZIP = ROOT / "var/ctds-c01/StarMap-C01-Mac-OneClick.zip"
BUNDLE_ROOT = "StarMap-C01-Mac"

COMMAND_LAUNCHER = """#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
exec bash delivery/scripts/deploy_mac.sh "$@"
"""

SEED_CONFIG = {
    "room_id": "demo-room-01",
    "room_name_zh": "睡眠房",
    "db_path": "var/ctds-c01/ctds_c01.sqlite3",
    "listen_port": 8765,
    "poll_interval_s": 60,
    "timeout_s": 5,
    "retries": 2,
    "max_backoff_s": 300,
    "stale_after_s": 180,
    "source": {
        "source_id": "ag-room-01",
        "mode": "LIVE",
        "base_url": "http://192.0.2.10",
        "binding_revision": 1,
    },
    "eight_sleep": {
        "source_id": "es-pod-01",
        "mode": "READY",
        "email": "",
        "password": "",
        "bearer_token": "",
        "user_id": "",
        "device_id": "",
        "side": "auto",
        "timezone": "Asia/Shanghai",
        "binding_revision": 1,
    },
}


def _export_clean_sqlite_seed(src_db: Path, dst_db: Path) -> None:
    src = sqlite3.connect(str(src_db))
    try:
        dst = sqlite3.connect(str(dst_db))
        try:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def _write_exec_info(arcname: str, mode: int = 0o755) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(arcname)
    zi.external_attr = (stat.S_IFREG | mode) << 16
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def build_bundle() -> dict:
    OUT_ZIP.parent.mkdir(parents=True, exist_ok=True)
    include_paths = []
    for sub in ("development/ctds/room_display", "development/ctds/tests/room_display"):
        base = ROOT / sub
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith("."):
                include_paths.append(p.relative_to(ROOT))
    for extra in (
        Path("delivery/scripts/deploy_mac.sh"),
        Path("docs/operations/DEPLOYMENT_AND_RUNBOOK_ZH.md"),
        Path("docs/operations/com.ctds.room-display.plist"),
    ):
        if (ROOT / extra).is_file():
            include_paths.append(extra)

    src_db = ROOT / "var/ctds-c01/ctds_c01.sqlite3"
    with zipfile.ZipFile(OUT_ZIP, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(_write_exec_info(f"{BUNDLE_ROOT}/一键部署_StarMap.command", 0o755), COMMAND_LAUNCHER.encode("utf-8"))
        for rel in include_paths:
            full = ROOT / rel
            arc = f"{BUNDLE_ROOT}/{rel.as_posix()}"
            if rel.suffix == ".sh":
                zf.writestr(_write_exec_info(arc, 0o755), full.read_bytes())
            else:
                zf.write(full, arc)

        zf.writestr(
            f"{BUNDLE_ROOT}/var/ctds-c01/seed_config.json",
            (json.dumps(SEED_CONFIG, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        if src_db.is_file():
            with tempfile.TemporaryDirectory() as tmp:
                clean_db = Path(tmp) / "seed_frozen.sqlite3"
                _export_clean_sqlite_seed(src_db, clean_db)
                zf.write(clean_db, f"{BUNDLE_ROOT}/var/ctds-c01/seed_frozen.sqlite3")

    raw = OUT_ZIP.read_bytes()
    return {
        "bundle_path": str(OUT_ZIP.relative_to(ROOT)),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "files_included": len(include_paths) + 3,
    }


if __name__ == "__main__":
    print(json.dumps(build_bundle(), ensure_ascii=False, indent=2))
