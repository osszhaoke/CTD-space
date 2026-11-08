#!/usr/bin/env python3
"""StarMap (CTDS) · Background Service & Quick Start Controller.

Provides fast, silent, detached background process management similar to PTDS.
Commands:
  start    Ensure background service is running and open browser (default)
  stop     Gracefully stop background service
  restart  Restart background service
  status   Inspect background service and collection health
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
VAR_DIR = ROOT / "var" / "ctds-c01"
PID_FILE = VAR_DIR / "daemon.pid"
LOG_FILE = VAR_DIR / "daemon.log"
CONFIG_FILE = VAR_DIR / "config.json"
DEFAULT_PORT = 8765


def get_configured_port() -> int:
    if CONFIG_FILE.is_file():
        try:
            doc = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return int(doc.get("listen_port", DEFAULT_PORT))
        except Exception:
            pass
    return DEFAULT_PORT


def get_configured_room_id() -> str:
    if CONFIG_FILE.is_file():
        try:
            doc = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return str(doc.get("room_id", "demo-room-01"))
        except Exception:
            pass
    return "demo-room-01"


def is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False

    # Verify process command line contains room_display
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0 and "room_display" in res.stdout:
            return True
    except Exception:
        pass
    return False


def get_running_pid() -> int | None:
    if not PID_FILE.is_file():
        return None
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
        if text.isdigit():
            pid = int(text)
            if is_pid_alive(pid):
                return pid
    except Exception:
        pass
    return None


def check_readyz(port: int, timeout: float = 1.0) -> dict | None:
    url = f"http://127.0.0.1:{port}/readyz"
    req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{port}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def fetch_display_summary(port: int, room_id: str) -> dict | None:
    url = f"http://127.0.0.1:{port}/api/v1/rooms/{room_id}/display"
    req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{port}"})
    try:
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        pass
    return None


def cleanup_stale_listeners(port: int) -> None:
    """Safely terminate orphan room_display processes on the target port."""
    try:
        res = subprocess.run(
            ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.strip().splitlines():
                if line.strip().isdigit():
                    l_pid = int(line.strip())
                    if is_pid_alive(l_pid):
                        try:
                            os.kill(l_pid, signal.SIGTERM)
                            time.sleep(0.3)
                            if is_pid_alive(l_pid):
                                os.kill(l_pid, signal.SIGKILL)
                        except OSError:
                            pass
    except Exception:
        pass


def cmd_start(args) -> int:
    port = get_configured_port()
    room_id = get_configured_room_id()
    display_url = f"http://127.0.0.1:{port}/"

    running_pid = get_running_pid()
    ready_info = check_readyz(port)

    # 1. Already running and healthy -> quick exit
    if running_pid and ready_info:
        print("============================================================")
        print("  StarMap (CTDS) 空间与微环境采集底座")
        print("============================================================")
        print(f"  服务状态:   已在后台静默运行中 (PID: {running_pid})")
        print(f"  大屏地址:   {display_url}")
        print(f"  就绪状态:   ready={ready_info.get('ready')} (source={ready_info.get('source_status')})")
        print("------------------------------------------------------------")
        print("  提示：关闭浏览器或终端窗口不会停止后台数据采集。")
        print("  若需查看实时状态: bash 启动CTDS.command status")
        print("  若需停止后台服务: bash 启动CTDS.command stop")
        print("============================================================")

        if not args.no_browser and not os.environ.get("CTDS_NO_BROWSER"):
            subprocess.run(["open", display_url], check=False)
        return 0

    # 2. Not running or unhealthy -> ensure configuration exists and start
    VAR_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.is_file():
        # Auto-initialize config if completely missing
        init_cmd = [sys.executable, "-B", "-m", "room_display", "--config", str(CONFIG_FILE), "init-config"]
        subprocess.run(init_cmd, cwd=str(ROOT), env=dict(os.environ, PYTHONPATH="development/ctds"), check=False)

    # Clean up stale pid file and orphan listeners
    if PID_FILE.is_file():
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    cleanup_stale_listeners(port)

    log_fd = open(LOG_FILE, "a", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH="development/ctds")
    cmd = [sys.executable, "-B", "-m", "room_display", "--config", str(CONFIG_FILE), "run"]
    if getattr(args, "lan", False):
        cmd.append("--lan")

    # True detached background execution (POSIX start_new_session)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")

    # Poll readyz
    ready = False
    for _ in range(25):
        time.sleep(0.2)
        ready_info = check_readyz(port, timeout=0.5)
        if ready_info and ready_info.get("ready"):
            ready = True
            break

    if not ready:
        print(f"[ERROR] 服务启动超时 (PID: {proc.pid})，请检查日志: {LOG_FILE}", file=sys.stderr)
        return 1

    print("============================================================")
    print("  StarMap (CTDS) 空间与微环境采集底座")
    print("============================================================")
    print(f"  服务启动成功，已转入后台静默运行 (PID: {proc.pid})")
    print(f"  大屏地址:   {display_url}")
    print(f"  后台日志:   {LOG_FILE.relative_to(ROOT)}")
    print(f"  数据存储:   var/ctds-c01/ctds_c01.sqlite3")
    print("------------------------------------------------------------")
    print("  提示：后台采集线程已启动，持续搜集 AirGradient 与微环境数据。")
    print("  关闭网页或关闭此终端窗口，后台采集仍将持续静默执行。")
    print("  若需查看实时状态: bash 启动CTDS.command status")
    print("  若需停止后台服务: bash 启动CTDS.command stop")
    print("============================================================")

    if not args.no_browser and not os.environ.get("CTDS_NO_BROWSER"):
        subprocess.run(["open", display_url], check=False)
    return 0


def cmd_stop(args) -> int:
    port = get_configured_port()
    running_pid = get_running_pid()

    if not running_pid:
        cleanup_stale_listeners(port)
        if PID_FILE.is_file():
            try:
                PID_FILE.unlink()
            except OSError:
                pass
        print("未检测到运行中的 StarMap (CTDS) 后台服务。")
        return 0

    print(f"正在停止 StarMap (CTDS) 后台服务 (PID: {running_pid})...")
    try:
        os.kill(running_pid, signal.SIGTERM)
        for _ in range(25):
            time.sleep(0.2)
            if not is_pid_alive(running_pid):
                break
        if is_pid_alive(running_pid):
            os.kill(running_pid, signal.SIGKILL)
            time.sleep(0.2)
    except OSError:
        pass

    cleanup_stale_listeners(port)
    if PID_FILE.is_file():
        try:
            PID_FILE.unlink()
        except OSError:
            pass

    print("服务已成功停止。")
    return 0


def cmd_restart(args) -> int:
    cmd_stop(args)
    time.sleep(0.5)
    return cmd_start(args)


def cmd_status(args) -> int:
    port = get_configured_port()
    room_id = get_configured_room_id()
    running_pid = get_running_pid()
    ready_info = check_readyz(port)

    print("============================================================")
    print("  StarMap (CTDS) 后台服务运行状态")
    print("============================================================")
    if running_pid and ready_info:
        print(f"  状态:       运行中 (RUNNING)")
        print(f"  PID:        {running_pid}")
        print(f"  大屏端口:   http://127.0.0.1:{port}/")
        print(f"  就绪指标:   ready={ready_info.get('ready')} (source={ready_info.get('source_status')})")

        disp = fetch_display_summary(port, room_id)
        if disp:
            print("------------------------------------------------------------")
            print("  采集源概览:")
            ag_mode = disp.get("source_mode", "UNKNOWN")
            ag_conn = disp.get("actual_source_connectivity", "UNKNOWN")
            print(f"    - AirGradient 空间探针:  {ag_mode} · {ag_conn}")

            es = disp.get("eight_sleep", {})
            es_mode = es.get("mode", "UNKNOWN")
            es_status = es.get("status_zh", es.get("status", "UNKNOWN"))
            print(f"    - Eight Sleep 温控床体:  {es_mode} · {es_status}")

            cloud_bf = disp.get("cloud_backfill", {})
            if cloud_bf:
                bf_status = cloud_bf.get("status", "OFF")
                bf_done = cloud_bf.get("full_backfill_done", False)
                print(f"    - 云端全量历史同步:      {bf_status} (全量完成: {bf_done})")
    else:
        print("  状态:       已停止 (STOPPED)")
        print(f"  目标端口:   {port}")

    # SQLite file size
    db_file = VAR_DIR / "ctds_c01.sqlite3"
    if db_file.is_file():
        size_mb = db_file.stat().st_size / (1024 * 1024)
        print(f"  数据库大小: {size_mb:.2f} MB ({db_file.relative_to(ROOT)})")
    print("============================================================")
    return 0 if (running_pid and ready_info) else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="StarMap (CTDS) Background Service Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="action", help="Service control action")

    p_start = sub.add_parser("start", help="Start background service and open browser (default)")
    p_start.add_argument("--no-browser", action="store_true", help="do not open browser automatically")
    p_start.add_argument("--lan", action="store_true", help="enable LAN sharing mode")

    sub.add_parser("stop", help="Stop background service")
    p_restart = sub.add_parser("restart", help="Restart background service")
    p_restart.add_argument("--no-browser", action="store_true", help="do not open browser automatically")
    p_restart.add_argument("--lan", action="store_true", help="enable LAN sharing mode")

    sub.add_parser("status", help="Check background service status and collection summary")

    args, unknown = parser.parse_known_args()

    # Default action to start if none specified (for direct double-click)
    if not args.action:
        args.action = "start"
        args.no_browser = False
        args.lan = False

    if args.action == "start":
        return cmd_start(args)
    elif args.action == "stop":
        return cmd_stop(args)
    elif args.action == "restart":
        return cmd_restart(args)
    elif args.action == "status":
        return cmd_status(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
