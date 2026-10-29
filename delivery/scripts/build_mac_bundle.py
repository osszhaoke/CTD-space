#!/usr/bin/env python3
"""Build the single self-contained Mac deployment and migration bundle.

Outputs: var/ctds-c01/StarMap-C01-Mac-OneClick.zip (chmod 600)
Authorized by Decision B: Contains real credentials and sqlite snapshot when configured locally.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT_ZIP = ROOT / "var/ctds-c01/StarMap-C01-Mac-OneClick.zip"
BUNDLE_ROOT = "StarMap-C01-Mac"

EXCLUDED_NAMES = frozenset({"index.html", "app.js", "style.css"})

ROOT_LAUNCHERS = (
    "一键部署_StarMap.command",
    "一键部署_StarMap_局域网共享.command",
    "一键打包_StarMap.command",
    "检测_EightSleep连通性.command",
    "检测_局域网共享连通性.command",
)

def bundle_config_bytes(cfg_path: Path) -> bytes:
    """Packaged copy of config.json for the target Mac (the local file is never modified).

    AirGradient is shipped in LIVE mode: if the first scan on the target network finds nothing, the screen
    shows "离线" (never synthetic data) and the collector keeps re-scanning until the device appears.
    """
    raw = cfg_path.read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw
    src = doc.get("source")
    if isinstance(src, dict) and src.get("mode") != "LIVE":
        src["mode"] = "LIVE"
        if not src.get("base_url"):
            src["base_url"] = "http://192.0.2.10"   # RFC 5737 placeholder; replaced by auto-discovery
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def make_private_readme(cfg_path: Path) -> str:
    has_bound = False
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            es = data.get("eight_sleep", {})
            has_bound = bool(es.get("bearer_token") and str(es.get("bearer_token")).strip() and es.get("mode") == "LIVE")
        except Exception:
            has_bound = False

    if has_bound:
        return """# 私密现场交付与迁移包说明

本包包含已鉴权的 Eight Sleep 账号凭据与本地历史数据库，专供目标 Mac 现场一键部署与接续运行。
**含已鉴权凭据与生理数据 · 勿外发**

- 规范基线：CTDS-C01 方案 B 授权落档
- 适用范围：目标 Mac 本地环回部署，自动发现并接入 AirGradient 与 Eight Sleep，解压即能持续采集并展示生理时序。
- 权限说明：var/ctds-c01/config.json 包含凭据，已设为 600 权限，严禁公开发布。

## 目标 Mac 一键部署步骤
1. 用 AirDrop 或 U 盘把本 zip 拷到目标 Mac，双击解压（目标机需 Python 3.9+，已装 3.10+ 即可）。
2. 首次打开：在解压出的文件夹里 **右键「一键部署_StarMap.command」→ 打开 → 再点「打开」**（macOS 会拦截从其他设备传来的脚本，只需这一次）。
3. 脚本自动完成：扫描当前网段并接入 AirGradient（找不到会提示手动输入设备 IP，可留空跳过）→ 用包内已保存的凭据静默接入 Eight Sleep（凭据失效时会提示重新输入）→ 运行自检 → 启动服务并打开 http://127.0.0.1:8765/ 。
4. 屏幕上顶部徽章显示"每 60 秒采集 · 在线"与"Pod 6 · 实时在线"即为接入成功。

## 故障排查与连通性诊断
若目标 Mac 遇到 Eight Sleep 离线或网络故障：
- 直接双击 **「检测_EightSleep连通性.command」**；
- 脚本会自动检测 DNS 解析、443 端口 TCP 连通、TLS 握手证书链以及各业务 API 返回状态；
- 并在桌面自动生成已脱敏的诊断报告文件，并在访达中自动高亮定位；
- 右键诊断报告 -> 共享 -> 隔空投送 (AirDrop) 发回即可迅速定位原因。

## 注意
- AirGradient 与目标 Mac 需在同一局域网（同网段、路由器未开启 AP/客户端隔离），且设备已开启本地 API。
- 迁移完成后请删除中转用的 zip 副本；不要上传网盘、聊天工具或提交 Git。
"""
    else:
        return """# 私密现场交付与迁移包说明

本包为现场交付与迁移包，专供目标 Mac 现场一键部署。
**尚未绑定 Eight Sleep，部署时将提示绑定**

- 规范基线：CTDS-C01 方案 B 授权落档
- 适用范围：目标 Mac 本地环回部署，自动发现 AirGradient，尚未配置 Eight Sleep 真实账号凭据。
- 首次运行：一键部署脚本将引导交互式输入 Eight Sleep 凭据完成绑定。

## 目标 Mac 一键部署步骤
1. 用 AirDrop 或 U 盘把本 zip 拷到目标 Mac，双击解压（目标机需 Python 3.9+，已装 3.10+ 即可）。
2. 首次打开：在解压出的文件夹里 **右键「一键部署_StarMap.command」→ 打开 → 再点「打开」**（macOS 会拦截从其他设备传来的脚本，只需这一次）。
3. 脚本自动完成：扫描当前网段并接入 AirGradient（找不到会提示手动输入设备 IP，可留空跳过）→ 用包内已保存的凭据静默接入 Eight Sleep（凭据失效时会提示重新输入）→ 运行自检 → 启动服务并打开 http://127.0.0.1:8765/ 。
4. 屏幕上顶部徽章显示"每 60 秒采集 · 在线"与"Pod 6 · 实时在线"即为接入成功。

## 故障排查与连通性诊断
若目标 Mac 遇到 Eight Sleep 离线或网络故障：
- 直接双击 **「检测_EightSleep连通性.command」**；
- 脚本会自动检测 DNS 解析、443 端口 TCP 连通、TLS 握手证书链以及各业务 API 返回状态；
- 并在桌面自动生成已脱敏的诊断报告文件，并在访达中自动高亮定位；
- 右键诊断报告 -> 共享 -> 隔空投送 (AirDrop) 发回即可迅速定位原因。

## 注意
- AirGradient 与目标 Mac 需在同一局域网（同网段、路由器未开启 AP/客户端隔离），且设备已开启本地 API。
- 迁移完成后请删除中转用的 zip 副本；不要上传网盘、聊天工具或提交 Git。
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
        "source_id": "pod-room-01",
        "mode": "READY",
        "side": "solo",
        "timezone": "Asia/Shanghai",
        "binding_revision": 1,
    },
}


def _write_exec_info(arcname: str, mode: int = 0o755) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(arcname)
    zi.external_attr = (stat.S_IFREG | mode) << 16
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def _export_sqlite_consistent_bytes(db_path: Path) -> bytes:
    """Export a clean consistent SQLite snapshot using the backup API."""
    if not db_path.is_file():
        return b""
    src = sqlite3.connect(db_path)
    try:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as tf:
            tmp_path = Path(tf.name)
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
        data = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)
        return data
    finally:
        src.close()


def build_bundle(out_zip: Path = None) -> dict:
    """Build the single delivery bundle. CTDS_BUNDLE_OUT (or out_zip) builds to a staging path instead."""
    out_zip = Path(out_zip or os.environ.get("CTDS_BUNDLE_OUT") or OUT_ZIP)
    if not out_zip.is_absolute():
        out_zip = ROOT / out_zip
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "starmap-deploy.zip").unlink(missing_ok=True)
    include_paths = []
    for sub in ("development/ctds/room_display", "development/ctds/tests/room_display"):
        base = ROOT / sub
        for p in sorted(base.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".") and p.name not in EXCLUDED_NAMES:
                include_paths.append(p.relative_to(ROOT))

    for extra in (
        Path("delivery/scripts/deploy_mac.sh"),
        Path("delivery/scripts/package_mac.sh"),
        Path("delivery/scripts/diag_eight_sleep.py"),
        Path("delivery/scripts/diag_lan_connectivity.py"),
        Path("delivery/scripts/verify_manifest.py"),
        Path("delivery/manifest.lock.json"),
        Path("Dockerfile"),
        Path("docker-compose.yml"),
        Path("docs/operations/DEPLOYMENT_AND_RUNBOOK_ZH.md"),
        Path("docs/operations/com.ctds.room-display.plist"),
    ):
        if (ROOT / extra).is_file():
            include_paths.append(extra)

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Launchers (single source of truth from repository root) & Private README
        for launcher_name in ROOT_LAUNCHERS:
            launcher_path = ROOT / launcher_name
            if not launcher_path.is_file():
                raise FileNotFoundError(f"missing root launcher: {launcher_name}")
            zf.writestr(_write_exec_info(f"{BUNDLE_ROOT}/{launcher_name}", 0o755), launcher_path.read_bytes())
        readme_content = make_private_readme(ROOT / "var/ctds-c01/config.json")
        zf.writestr(_write_exec_info(f"{BUNDLE_ROOT}/PRIVATE_README.md", 0o644), readme_content.encode("utf-8"))

        # Source code, tests, docs, scripts
        for rel in include_paths:
            full = ROOT / rel
            arc = f"{BUNDLE_ROOT}/{rel.as_posix()}"
            mode = 0o755 if rel.suffix in (".sh", ".command") else 0o644
            zf.writestr(_write_exec_info(arc, mode), full.read_bytes())

        # Config template
        zf.writestr(
            _write_exec_info(f"{BUNDLE_ROOT}/var/ctds-c01/config.example.json", 0o600),
            (json.dumps(SEED_CONFIG, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

        # Real configured config.json if present
        cfg_real = ROOT / "var/ctds-c01/config.json"
        if cfg_real.is_file():
            zf.writestr(_write_exec_info(f"{BUNDLE_ROOT}/var/ctds-c01/config.json", 0o600), bundle_config_bytes(cfg_real))

        # Real database snapshot if present
        db_real = ROOT / "var/ctds-c01/ctds_c01.sqlite3"
        if db_real.is_file():
            db_bytes = _export_sqlite_consistent_bytes(db_real)
            if db_bytes:
                zf.writestr(_write_exec_info(f"{BUNDLE_ROOT}/var/ctds-c01/ctds_c01.sqlite3", 0o600), db_bytes)

        # Recent collector log if <= 5MB
        log_real = ROOT / "var/ctds-c01/collector.log"
        if log_real.is_file() and log_real.stat().st_size <= 5 * 1024 * 1024:
            zf.writestr(_write_exec_info(f"{BUNDLE_ROOT}/var/ctds-c01/collector.log", 0o600), log_real.read_bytes())

    # Set package permission to 0600 per Decision B
    try:
        os.chmod(out_zip, 0o600)
    except OSError:
        pass

    raw = out_zip.read_bytes()
    return {
        "bundle_path": str(out_zip.relative_to(ROOT)) if out_zip.is_relative_to(ROOT) else str(out_zip),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "files_included": len(include_paths) + 3,
    }


if __name__ == "__main__":
    res = {
        "one_click": build_bundle(),
    }
    print(json.dumps(res, ensure_ascii=False, indent=2))
