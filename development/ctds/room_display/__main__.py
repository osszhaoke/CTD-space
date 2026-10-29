"""CLI: python3 -m room_display (with PYTHONPATH=development/ctds) <command>.

Commands:
  init-config       write a SYNTHETIC template to var/ctds-c01/config.json if absent (never overwrites)
  auto-bind         discover AirGradient on LAN, self-update config.json, and collect if online
  bind-eight-sleep  authenticate and bind Eight Sleep Pod 6 (LIVE/READY/SYNTHETIC)
  migrate-config    normalize config formatting (Solo side alignment) and enforce 0600 permissions
  self-test         run full automated test suite and live/offline cockpit verification
  run               collector + loopback display server in one process (Ctrl-C / SIGTERM stops cleanly)
  serve             display server only (read existing DB)
  collect-once      one read round; prints outcome and status counts only (no values/address)
  status            print row counts and latest attempt outcome
"""
import argparse
import hashlib
import io
import json
import logging
import os
import signal
import sys
import threading
import unittest
import urllib.error

from . import adapter, catalog, collector, config, discover, eight_sleep, projection, server
from .store import Store


def _open(cfg, read_only=False):
    return Store(config.resolve_path(cfg["db_path"]), read_only=read_only)


def cmd_init_config(args):
    path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    if path.exists():
        print(f"exists, not overwritten: {path}")
        return 0
    config.init_config(path)
    print(f"written SYNTHETIC template: {path}")
    return 0


def cmd_auto_bind(args):
    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    bound = discover.auto_bind(cfg_path)
    cfg = bound["config"]
    store = _open(cfg)
    collector.register(store, cfg)
    collect_outcome = None
    if bound["discovered"] or (cfg["source"]["mode"] == "SYNTHETIC" and store.counts()["observation"] == 0):
        collect_outcome = collector.Collector(store, cfg).round()
    elif (cfg.get("eight_sleep") or {}).get("mode") in ("LIVE", "SYNTHETIC"):
        eight_sleep.ingest_once(store, cfg)
    counts = store.counts()
    store.close()
    print(json.dumps({
        "discovered": bound["discovered"],
        "mode": bound["mode"],
        "eight_sleep_mode": bound.get("eight_sleep_mode", "READY"),
        "room_name_zh": cfg["room_name_zh"],
        "binding_updated": bound["binding_updated"],
        "binding_revision": bound["binding_revision"],
        "metrics_count": bound["metrics_count"],
        "collect_outcome": collect_outcome,
        "counts": counts,
    }, ensure_ascii=False))
    return 0


def _describe_bind_error(exc, stage="AUTH"):
    """Human-readable cause for a failed bind attempt. Never includes credentials."""
    code = exc.code if isinstance(exc, adapter.AdapterError) else type(exc).__name__
    detail = str(exc)
    if stage == "DISCOVER":
        return f"登录成功，但查找床垫设备失败（{code}：{detail}），请确认账号下已绑定 Pod"
    if code == "NO_CONNECTION":
        return f"无法连接 Eight Sleep 服务器（{detail}），请检查网络或代理"
    if code == "AUTH_REJECTED":
        return f"服务器拒绝了账号或密码（{detail}）；若账号通过 Apple/Google 登录注册，需先在 App 中设置密码"
    if code == "HTTP_ERROR":
        return f"服务器返回错误（{detail}）：400 多为 client 凭据或请求格式被拒，429 为请求过于频繁，请稍后再试"
    return f"{code}：{detail}"


def cmd_bind_eight_sleep(args):
    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    if cfg_path.exists():
        cfg = config.load(cfg_path)
    else:
        cfg = dict(
            config.DEFAULTS,
            source=dict(config.DEFAULTS["source"]),
            eight_sleep=dict(config.DEFAULTS["eight_sleep"]),
        )
    es = cfg.setdefault("eight_sleep", dict(config.DEFAULTS["eight_sleep"]))
    if getattr(args, "side", None):
        es["side"] = args.side.lower()
    if getattr(args, "tz", None):
        es["timezone"] = args.tz

    # I2: --check sub-mode: test existing saved credentials without modifying config
    if getattr(args, "check", False):
        if not cfg_path.exists():
            print(json.dumps({"authenticated": False, "device_ok": False, "outcome": "NO_CONFIG"}, ensure_ascii=False))
            return 1
        has_cred = bool(es.get("bearer_token") or es.get("refresh_token") or (es.get("email") and es.get("password")))
        if not has_cred:
            print(json.dumps({"authenticated": False, "device_ok": False, "outcome": "NO_CREDENTIALS"}, ensure_ascii=False))
            return 1
        try:
            client = eight_sleep.EightSleepClient.from_config(es, config_path=None)
            user_id = None
            device_id = None
            if client.bearer_token:
                try:
                    user_id, device_id = client.discover()
                except Exception:
                    client.refresh_auth()
                    user_id, device_id = client.discover()
            else:
                client.authenticate()
                user_id, device_id = client.discover()
            device_ok = bool(user_id and device_id)
            print(json.dumps({"authenticated": True, "device_ok": device_ok, "outcome": "OK"}, ensure_ascii=False))
            return 0
        except Exception as exc:
            outcome = "AUTH_REJECTED"
            if isinstance(exc, adapter.AdapterError):
                outcome = exc.code
            elif isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
                outcome = adapter.NO_CONNECTION
            print(json.dumps({"authenticated": False, "device_ok": False, "outcome": outcome, "detail": str(exc)}, ensure_ascii=False))
            return 1

    mode = args.es_mode or "LIVE"
    client = None
    if mode == "LIVE":
        import getpass
        max_attempts = 3
        attempt = 0
        cli_email = getattr(args, "email", None)
        while attempt < max_attempts:
            attempt += 1
            if cli_email:
                email = cli_email
            else:
                try:
                    email = input("请输入 Eight Sleep 注册邮箱（留空跳过）: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n已取消输入。")
                    return 1

            if not email:
                print("已跳过 Eight Sleep 绑定，保持 READY 待接入模式。")
                es["mode"] = "READY"
                cfg["eight_sleep"] = es
                if getattr(args, "db_path", None):
                    cfg["db_path"] = args.db_path
                cfg = config.validate(cfg)
                config.save_config(cfg, cfg_path)
                print(json.dumps({
                    "eight_sleep_mode": "READY",
                    "authenticated": False,
                    "user_id_configured": False,
                    "device_id_configured": False,
                    "outcome": "SKIPPED",
                }, ensure_ascii=False))
                return 0

            try:
                password = getpass.getpass("请输入 Eight Sleep 登录密码: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已取消输入。")
                return 1

            base_url = getattr(args, "base_url", None) or es.get("base_url") or eight_sleep.DEFAULT_CLIENT_API_URL
            auth_url = getattr(args, "auth_url", None) or es.get("auth_url") or eight_sleep.DEFAULT_AUTH_URL

            # In production, base_url and auth_url must be official https://*.8slp.net
            if os.environ.get("CTDS_TEST_SANDBOX") != "1":
                if not eight_sleep.is_official_8slp_url(base_url) or not eight_sleep.is_official_8slp_url(auth_url):
                    print(json.dumps({"outcome": "CONFIG_ERROR", "detail": "base_url and auth_url must be https://*.8slp.net"}, ensure_ascii=False))
                    return 1

            stage = "AUTH"
            try:
                client = eight_sleep.EightSleepClient(
                    email=email,
                    password=password,
                    side=es.get("side", "solo"),
                    base_url=base_url,
                    auth_url=auth_url,
                )
                token = client.authenticate()
                stage = "DISCOVER"
                user_id, device_id = client.discover()
                break
            except Exception as exc:
                reason = _describe_bind_error(exc, stage)
                if attempt < max_attempts and not cli_email:
                    print(f"Eight Sleep 绑定失败：{reason}（剩余尝试次数：{max_attempts - attempt}）")
                else:
                    code = exc.code if isinstance(exc, adapter.AdapterError) else type(exc).__name__
                    print(json.dumps({"outcome": code, "stage": stage, "reason_zh": reason, "detail": str(exc)},
                                     ensure_ascii=False))
                    return 1

        es["mode"] = "LIVE"
        es["email"] = email
        es["password"] = password
        es["bearer_token"] = token
        es["refresh_token"] = client.refresh_token
        es["user_id"] = user_id
        es["device_id"] = device_id
        es["token_expires_at"] = client.token_expires_at
        if getattr(args, "base_url", None):
            es["base_url"] = args.base_url
        if getattr(args, "auth_url", None):
            es["auth_url"] = args.auth_url
    else:
        es["mode"] = mode

    es["binding_revision"] = int(es.get("binding_revision", 1)) + 1
    cfg["eight_sleep"] = es
    if getattr(args, "db_path", None):
        cfg["db_path"] = args.db_path
    cfg = config.validate(cfg)
    config.save_config(cfg, cfg_path)

    store = _open(cfg)
    # Defense: only official 8slp endpoint client can write provenance="LIVE"
    is_real_8slp = client and eight_sleep.is_official_8slp_url(client.auth_url) and eight_sleep.is_official_8slp_url(client.base_url)
    effective_provenance = "LIVE" if (es["mode"] == "LIVE" and is_real_8slp) else "SYNTHETIC"
    outcome = eight_sleep.ingest_once(store, cfg, client=client, provenance=effective_provenance)
    metric_status = {}
    for mid in sorted(eight_sleep.EIGHT_SLEEP_METRICS.keys(), key=lambda k: eight_sleep.EIGHT_SLEEP_METRICS[k]["order"]):
        o = store.latest_observation(cfg["room_id"], mid)
        metric_status[mid] = o["value_status"] if o else "NOT_PROVIDED"
    counts = store.counts()
    store.close()
    print(json.dumps({
        "eight_sleep_mode": es["mode"],
        "authenticated": bool(es.get("bearer_token")),
        "user_id_configured": bool(es.get("user_id")),
        "device_id_configured": bool(es.get("device_id")),
        "outcome": outcome,
        "side": es["side"],
        "binding_revision": es["binding_revision"],
        "metric_status": metric_status,
        "counts": counts,
    }, ensure_ascii=False, indent=2))
    return 0 if outcome == "OK" else 2


def cmd_self_test(args):
    real_db_path = config.ROOT / "var/ctds-c01/ctds_c01.sqlite3"
    db_sha_before = hashlib.sha256(real_db_path.read_bytes()).hexdigest() if real_db_path.is_file() else None

    tests_dir = config.ROOT / "development/ctds/tests/room_display"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))

    # Run test suite strictly in sandbox mode to isolate from real runtime DB
    old_sandbox = os.environ.get("CTDS_TEST_SANDBOX")
    os.environ["CTDS_TEST_SANDBOX"] = "1"
    try:
        suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test_*.py")
        buf = io.StringIO()
        runner = unittest.TextTestRunner(stream=buf, verbosity=1)
        result = runner.run(suite)
    finally:
        if old_sandbox is None:
            os.environ.pop("CTDS_TEST_SANDBOX", None)
        else:
            os.environ["CTDS_TEST_SANDBOX"] = old_sandbox

    # Verify real runtime DB was strictly unmutated
    db_mutated = False
    if real_db_path.is_file():
        db_sha_after = hashlib.sha256(real_db_path.read_bytes()).hexdigest()
        if db_sha_before and db_sha_before != db_sha_after:
            db_mutated = True

    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    cfg = config.load(cfg_path) if cfg_path.exists() else dict(config.TEMPLATE)
    store = _open(cfg, read_only=True)
    disp = projection.build_display(store, cfg)
    main_html_path = config.ROOT / "development/ctds/room_display/static/preview.html"
    main_html = main_html_path.read_text(encoding="utf-8")
    es_view = disp.get("eight_sleep", {})
    es_mids = [m["metric_id"] for m in es_view.get("metrics", [])]
    ui_checks = (
        disp.get("room_name_zh") == "睡眠房"
        and 'data-theme="dark"' in main_html
        and "AirGradient" not in main_html
        and "Eight Sleep" not in main_html
        and len(disp.get("metrics", [])) >= 5
        and es_mids == sorted(eight_sleep.EIGHT_SLEEP_METRICS, key=lambda k: eight_sleep.EIGHT_SLEEP_METRICS[k]["order"])
        and eight_sleep.heating_level_to_temp(-17) == 25.0
        and eight_sleep.heating_level_to_temp(0) == 27.0
    )
    counts = store.counts()
    store.close()

    ok = result.wasSuccessful() and ui_checks and not db_mutated
    summary = {
        "result": "PASS" if ok else "FAIL",
        "tests_ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "db_unmutated": not db_mutated,
        "room_name_zh": disp.get("room_name_zh"),
        "default_theme": "forest-green" if 'data-theme="dark"' in main_html else "other",
        "source_mode": disp.get("source_mode"),
        "connectivity": disp.get("actual_source_connectivity"),
        "eight_sleep_mode": es_view.get("mode"),
        "eight_sleep_metrics": len(es_mids),
        "counts": counts,
    }
    print(json.dumps(summary, ensure_ascii=False))
    if not ok:
        print(buf.getvalue(), file=sys.stderr)
    return 0 if ok else 1


def cmd_run(args, cfg, serve_only=False, cfg_path=None):
    store = _open(cfg)
    collector.register(store, cfg)
    allow_lan = bool(getattr(args, "lan", False) or os.environ.get("CTDS_ALLOW_LAN") == "1")
    srv = server.make_server(store, cfg, host=getattr(args, "host", None), allow_lan=allow_lan)
    col = None if serve_only else collector.Collector(store, cfg, config_path=cfg_path)
    shutdown = threading.Event()

    def stop(*_):
        shutdown.set()
        if col:
            col.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    server.serve_in_thread(srv)
    host_display = srv.server_address[0] if srv.server_address[0] not in ("0.0.0.0", "::") else "127.0.0.1"
    print(f"display: http://{host_display}:{srv.server_address[1]}/ (四海极奢看板)  admin: /admin  mode={cfg['source']['mode']}", flush=True)
    if allow_lan or srv.server_address[0] in ("0.0.0.0", "::"):
        print(f"lan: 局域网设备可通过本机 IP 访问 (例如 http://<本机IP>:{srv.server_address[1]}/)", flush=True)
    worker = None
    if col:
        def work():
            # An unexpected error restarts collection with backoff; the display keeps serving meanwhile.
            backoff = 5
            while not col.stop_event.is_set():
                try:
                    result = col.run()
                    print(f"collector ended: {result}" + ("" if result == "STOPPED" else
                          " (display keeps serving; fix binding then restart)"), flush=True)
                    return
                except Exception as exc:
                    print(f"collector error: {type(exc).__name__} (restarting in {backoff}s; display keeps serving)",
                          flush=True)
                    if col.stop_event.wait(backoff):
                        return
                    backoff = min(backoff * 2, 300)
        worker = threading.Thread(target=work, name="ctds-c01-collector", daemon=True)
        worker.start()
    while not shutdown.wait(1.0):
        pass
    srv.shutdown()
    srv.server_close()
    if worker:
        worker.join(timeout=cfg["timeout_s"] + 2)
    store.close()
    print("stopped", flush=True)
    return 0


def cmd_collect_once(args, cfg, cfg_path=None):
    store = _open(cfg)
    collector.register(store, cfg)
    outcome = collector.Collector(store, cfg, config_path=cfg_path).round()
    last = store.latest_attempt(cfg["source"]["source_id"])
    metric_status = {}
    if outcome == "OK":
        for mid in catalog.METRICS:
            o = store.latest_observation(cfg["room_id"], mid)
            metric_status[mid] = o["value_status"] if o else None
    print(json.dumps({"mode": cfg["source"]["mode"], "outcome": outcome, "http_status": last["http_status"],
                      "metric_status": metric_status, "counts": store.counts()}, ensure_ascii=False))
    store.close()
    return 0 if outcome == "OK" else 2


def cmd_migrate_config(args):
    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    if not cfg_path.exists():
        print(json.dumps({"migrated": False, "reason": "FILE_NOT_FOUND"}, ensure_ascii=False))
        return 1
    migrated = config.migrate_config_file(cfg_path)
    try:
        doc = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        doc = {}
    print(json.dumps({"migrated": migrated, "mode": doc.get("eight_sleep", {}).get("mode", "READY")}, ensure_ascii=False))
    return 0


def cmd_status(args, cfg):
    store = _open(cfg)
    last = store.latest_attempt(cfg["source"]["source_id"])
    print(json.dumps({"counts": store.counts(), "latest_outcome": last["outcome"] if last else None,
                      "latest_at": last["request_started_at"] if last else None}, ensure_ascii=False))
    store.close()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="room_display")
    p.add_argument("--config", help="path to private config (default var/ctds-c01/config.json)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--side", choices=["solo"], default="solo", help="Eight Sleep mattress side (Solo alignment)")
    p.add_argument("--tz", help="Eight Sleep timezone (default Asia/Shanghai)")
    p.add_argument("--es-mode", choices=["READY", "SYNTHETIC", "LIVE"], help="Eight Sleep source mode")
    p.add_argument("--email", help="Eight Sleep account email")
    p.add_argument("--db-path", help="path to SQLite database (overrides config)")
    p.add_argument("--base-url", help="Eight Sleep client API base URL (for testing or proxy)")
    p.add_argument("--auth-url", help="Eight Sleep auth API URL (for testing or proxy)")
    p.add_argument("--check", action="store_true", help="test existing saved credentials without modifying config")
    p.add_argument("--host", help="listen host (default 127.0.0.1, or 0.0.0.0 for container/LAN)")
    p.add_argument("--lan", "--share-lan", action="store_true", help="allow access from other devices on the same LAN (Wi-Fi)")
    p.add_argument(
        "command",
        choices=["init-config", "auto-bind", "bind-eight-sleep", "migrate-config", "self-test", "run", "serve", "collect-once", "status"],
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "init-config":
        return cmd_init_config(args)
    if args.command == "auto-bind":
        return cmd_auto_bind(args)
    if args.command == "bind-eight-sleep":
        return cmd_bind_eight_sleep(args)
    if args.command == "migrate-config":
        return cmd_migrate_config(args)
    if args.command == "self-test":
        return cmd_self_test(args)
    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    if not cfg_path.exists() and args.command in ("run", "serve"):
        config.init_config(cfg_path)
    try:
        cfg = config.load(cfg_path)
    except config.ConfigError as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return 64
    if getattr(args, "db_path", None):
        cfg["db_path"] = args.db_path
    if args.command == "run":
        return cmd_run(args, cfg, cfg_path=cfg_path)
    if args.command == "serve":
        return cmd_run(args, cfg, serve_only=True, cfg_path=cfg_path)
    if args.command == "collect-once":
        return cmd_collect_once(args, cfg, cfg_path=cfg_path)
    return cmd_status(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
