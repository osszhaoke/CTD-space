"""CLI: python3 -m room_display (with PYTHONPATH=development/ctds) <command>.

Commands:
  init-config       write a SYNTHETIC template to var/ctds-c01/config.json if absent (never overwrites)
  auto-bind         discover AirGradient on LAN, self-update config.json, and collect if online
  bind-eight-sleep  bind Eight Sleep credentials/token into var/ctds-c01/config.json and test live fetch
  self-test         run full automated test suite and live/offline cockpit verification
  run               collector + loopback display server in one process (Ctrl-C / SIGTERM stops cleanly)
  serve             display server only (read existing DB)
  collect-once      one read round; prints outcome and status counts only (no values/address)
  status            print row counts and latest attempt outcome
"""
import argparse
import io
import json
import logging
import signal
import sys
import threading
import unittest

from . import catalog, collector, config, discover, eight_sleep, projection, server
from .store import Store


def _open(cfg):
    return Store(config.resolve_path(cfg["db_path"]))


def cmd_init_config(args):
    path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    if path.exists():
        print(f"exists, not overwritten: {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config.TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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


def cmd_bind_eight_sleep(args):
    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    if cfg_path.exists():
        cfg = config.validate(json.loads(cfg_path.read_text(encoding="utf-8")))
    else:
        cfg = dict(
            config.DEFAULTS,
            source=dict(config.DEFAULTS["source"]),
            eight_sleep=dict(config.DEFAULTS["eight_sleep"]),
        )
    es = cfg.setdefault("eight_sleep", dict(config.DEFAULTS["eight_sleep"]))
    if args.email:
        es["email"] = args.email
    if args.password:
        es["password"] = args.password
    if args.token:
        es["bearer_token"] = args.token
    if args.user_id:
        es["user_id"] = args.user_id
    if args.device_id:
        es["device_id"] = args.device_id
    if args.side:
        es["side"] = args.side.lower()
    if args.tz:
        es["timezone"] = args.tz
    es["mode"] = args.es_mode or "LIVE"
    es["binding_revision"] = int(es.get("binding_revision", 1)) + 1

    cfg = config.validate(cfg)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    store = _open(cfg)
    outcome = eight_sleep.ingest_once(store, cfg)
    metric_status = {}
    for mid in eight_sleep.EIGHT_SLEEP_METRICS:
        o = store.latest_observation(cfg["room_id"], mid)
        metric_status[mid] = o["value_status"] if o else None
    counts = store.counts()
    store.close()
    print(json.dumps({
        "eight_sleep_mode": es["mode"],
        "outcome": outcome,
        "side": es["side"],
        "binding_revision": es["binding_revision"],
        "metric_status": metric_status,
        "counts": counts,
    }, ensure_ascii=False))
    return 0 if outcome == "OK" else 2


def cmd_self_test(args):
    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    tests_dir = config.ROOT / "development/ctds/tests/room_display"
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    suite = unittest.defaultTestLoader.discover(str(tests_dir), pattern="test_*.py")
    buf = io.StringIO()
    runner = unittest.TextTestRunner(stream=buf, verbosity=1)
    result = runner.run(suite)

    cfg = config.load(cfg_path) if cfg_path.exists() else dict(config.TEMPLATE)
    store = _open(cfg)
    collector.register(store, cfg)
    disp = projection.build_display(store, cfg)
    index_html = (config.ROOT / "development/ctds/room_display/static/index.html").read_text(encoding="utf-8")
    es_view = disp.get("eight_sleep", {})
    es_mids = [m["metric_id"] for m in es_view.get("metrics", [])]
    ui_checks = (
        disp.get("room_name_zh") == "睡眠房"
        and 'data-theme="dark"' in index_html
        and 'id="pod-metrics-grid"' in index_html
        and "睡眠房" in index_html
        and "室内环境" in index_html
        and "床微环境" in index_html
        and "AirGradient" not in index_html
        and "Eight Sleep" not in index_html
        and len(disp.get("metrics", [])) >= 5
        and es_mids == [
            "pod_heart_rate",
            "pod_hrv",
            "pod_respiratory_rate",
            "pod_bed_temp",
            "pod_target_bed_temp",
            "pod_room_temp",
        ]
        and eight_sleep.heating_level_to_temp(-17) == 25.0
        and eight_sleep.heating_level_to_temp(0) == 27.0
    )
    counts = store.counts()
    store.close()

    ok = result.wasSuccessful() and ui_checks
    summary = {
        "result": "PASS" if ok else "FAIL",
        "tests_ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "room_name_zh": disp.get("room_name_zh"),
        "default_theme": "forest-green" if 'data-theme="dark"' in index_html else "other",
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
    srv = server.make_server(store, cfg)
    col = None if serve_only else collector.Collector(store, cfg, config_path=cfg_path)
    shutdown = threading.Event()

    def stop(*_):
        shutdown.set()
        if col:
            col.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    server.serve_in_thread(srv)
    print(f"display: http://127.0.0.1:{srv.server_address[1]}/  admin: /admin  mode={cfg['source']['mode']}", flush=True)
    worker = None
    if col:
        def work():
            result = col.run()
            print(f"collector ended: {result}" + ("" if result == "STOPPED" else
                  " (display keeps serving; fix binding then restart)"), flush=True)
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
    p.add_argument("--email", help="Eight Sleep account email (for bind-eight-sleep)")
    p.add_argument("--password", help="Eight Sleep account password (for bind-eight-sleep)")
    p.add_argument("--token", help="Eight Sleep bearer token (for bind-eight-sleep)")
    p.add_argument("--user-id", help="Eight Sleep userId override")
    p.add_argument("--device-id", help="Eight Sleep deviceId override")
    p.add_argument("--side", choices=["auto", "left", "right", "solo"], help="Eight Sleep mattress side")
    p.add_argument("--tz", help="Eight Sleep timezone (default Asia/Shanghai)")
    p.add_argument("--es-mode", choices=["READY", "SYNTHETIC", "LIVE"], help="Eight Sleep source mode")
    p.add_argument(
        "command",
        choices=["init-config", "auto-bind", "bind-eight-sleep", "self-test", "run", "serve", "collect-once", "status"],
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
    if args.command == "self-test":
        return cmd_self_test(args)
    cfg_path = config.resolve_path(args.config) if args.config else config.DEFAULT_CONFIG
    try:
        cfg = config.load(cfg_path)
    except config.ConfigError as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return 64
    if args.command == "run":
        return cmd_run(args, cfg, cfg_path=cfg_path)
    if args.command == "serve":
        return cmd_run(args, cfg, serve_only=True, cfg_path=cfg_path)
    if args.command == "collect-once":
        return cmd_collect_once(args, cfg, cfg_path=cfg_path)
    return cmd_status(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
