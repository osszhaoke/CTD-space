import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import threading
import unittest
from unittest import mock
import zipfile

from _helpers import Clock, FakeDevice, cfg

from room_display import adapter, collector, config, discover, eight_sleep, projection, server
from room_display.store import Store


class FakeEightSleepServer:
    def __init__(self, compat_session_format=False):
        self.hits = []
        self.should_401_once = False
        self._401_done = False
        self.refresh_fails = False
        self.compat_session_format = compat_session_format
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len else b""
                outer.hits.append(("POST", self.path, dict(self.headers), body))

                if self.path == "/v1/tokens":
                    try:
                        req_data = json.loads(body.decode("utf-8"))
                    except Exception:
                        req_data = {}

                    is_refresh = req_data.get("grant_type") == "refresh_token" or "refreshToken" in req_data
                    if outer.refresh_fails and is_refresh:
                        self.send_response(401)
                        self.end_headers()
                        return

                    email = req_data.get("username") or req_data.get("email")
                    pwd = req_data.get("password")
                    if not is_refresh and (email == "bad@example.com" or pwd == "wrong"):
                        self.send_response(401)
                        self.end_headers()
                        return

                    token_val = "refreshed-token-999" if is_refresh else "valid-token-123"
                    if outer.compat_session_format:
                        resp = {
                            "session": {
                                "token": token_val,
                                "expirationDate": "2026-10-24T00:00:00Z",
                                "refreshToken": "new-refresh-token-888",
                                "userId": "mock-user-001",
                            }
                        }
                    else:
                        resp = {
                            "access_token": token_val,
                            "token_type": "bearer",
                            "expires_in": 7200,
                            "refresh_token": "new-refresh-token-888",
                            "userId": "mock-user-001",
                            "user_id": "mock-user-001",
                        }
                    data = json.dumps(resp).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return

                self.send_response(404)
                self.end_headers()

            def do_GET(self):
                outer.hits.append(("GET", self.path, dict(self.headers), b""))
                auth = self.headers.get("authorization", "")

                if outer.should_401_once and not outer._401_done:
                    outer._401_done = True
                    self.send_response(401)
                    self.end_headers()
                    return

                if not auth.startswith("Bearer "):
                    self.send_response(401)
                    self.end_headers()
                    return

                path, _, query = self.path.partition("?")
                params = urllib.parse.parse_qs(query)
                if path in ("/v1/users/me", "/v1/users/mock-user-001"):
                    resp = {
                        "user": {
                            "userId": "mock-user-001",
                            "currentDeviceId": "mock-device-002",
                            "devices": ["mock-device-002"],
                        }
                    }
                elif path == "/v1/devices/mock-device-002":
                    # Real shape (observed 2026-10-23): per-side fields even on a Solo pod
                    resp = {
                        "result": {
                            "leftUserId": "mock-user-001",
                            "rightUserId": "someone-else",
                            "leftHeatingLevel": -17,
                            "leftTargetHeatingLevel": -17,
                            "rightHeatingLevel": 40,
                            "rightTargetHeatingLevel": 60,
                            "leftNowHeating": True,
                            "rightNowHeating": False,
                        }
                    }
                elif path == "/v1/app/users/mock-user-001/base":
                    # Real shape (observed 2026-10-23); hardwareInfo must never be stored or shown
                    resp = {"left": {"torso": {"currentAngle": 12, "moving": False}, "leg": {"currentAngle": 4, "moving": False}},
                            "right": {"torso": {"currentAngle": 0, "moving": False}, "leg": {"currentAngle": 0, "moving": False}},
                            "hardwareInfo": {"sku": "x", "hardwareVersion": "x", "softwareVersion": "x", "macAddress": "00:11:22:33:44:55"}}
                elif path == "/v1/users/mock-user-001/temperature":
                    resp = {"currentLevel": -17, "settings": {}}   # client-api: no currentDeviceLevel
                elif path == "/v1/app/users/mock-user-001/temperature":
                    resp = {"currentDeviceLevel": -17, "currentLevel": -17}   # app-api
                elif path == "/v1/users/mock-user-001/trends" and not {"tz", "from", "to"} <= set(params):
                    resp, outer_status = {"status": 400, "code": "BadRequest", "error": "missing tz/from/to"}, 400
                    data = json.dumps(resp).encode("utf-8")
                    self.send_response(400)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                elif path == "/v1/users/mock-user-001/trends":
                    resp = {
                        "days": [
                            {
                                "day": "2026-10-23",
                                "sleepQualityScore": {
                                    "hrv": {"current": 62.5},
                                    "respiratoryRate": {"current": 15.0},
                                },
                                "sessions": [
                                    {
                                        "timeseries": {
                                            "heartRate": [["2026-10-23T02:00:00Z", 56.0]],
                                            "tempRoomC": [["2026-10-23T02:00:00Z", 23.2]],
                                            "tempBedC": [["2026-10-23T01:00:00Z", 26.0], ["2026-10-23T02:00:00Z", 27.0]],
                                        }
                                    }
                                ],
                            }
                        ]
                    }
                else:
                    self.send_response(404)
                    self.end_headers()
                    return

                data = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                outer.hits.append(("PUT", self.path, dict(self.headers), b""))
                self.send_response(405)
                self.end_headers()

            def do_PATCH(self):
                outer.hits.append(("PATCH", self.path, dict(self.headers), b""))
                self.send_response(405)
                self.end_headers()

            def do_DELETE(self):
                outer.hits.append(("DELETE", self.path, dict(self.headers), b""))
                self.send_response(405)
                self.end_headers()

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self.auth_url = f"http://127.0.0.1:{self.port}/v1/tokens"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class StaticUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(":memory:")
        cls.cfg = cfg()
        clock = Clock()
        collector.register(cls.store, cls.cfg, clock)
        collector.Collector(cls.store, cls.cfg, clock=clock, sleeper=lambda _: None).round()
        cls.srv = server.make_server(cls.store, cls.cfg, port=0)
        cls.port = cls.srv.server_address[1]
        server.serve_in_thread(cls.srv)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.store.close()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def test_root_html(self):
        st, h, body = self.get("/")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")
        text = body.decode("utf-8")
        self.assertIn("睡眠房", text)
        self.assertIn('data-theme="dark"', text)
        self.assertIn("空间监测探针", text)
        self.assertIn("智能温控床体", text)
        self.assertIn("viewport-fit=cover", text)
        self.assertIn("apple-mobile-web-app-capable", text)
        self.assertIn(">微环境气候分析<", text)
        # Zero hardware brand exposure on customer display
        for brand in ("AirGradient", "Eight Sleep", "8sleep", "Pod 5"):
            self.assertNotIn(brand, text)
        # CSP and no external links
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertFalse(re.search(r"https?://", text))  # zero external CDNs/fonts

    def test_admin_html(self):
        st, h, body = self.get("/admin")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")
        text = body.decode("utf-8")
        self.assertIn("展示字段配置", text)
        self.assertIn("Pod 6", text)
        self.assertFalse(re.search(r"https?://", text))

    def test_static_assets(self):
        for path, ctype in (("/static/preview.css", "text/css; charset=utf-8"),
                            ("/static/preview.js", "text/javascript; charset=utf-8"),
                            ("/static/admin.js", "text/javascript; charset=utf-8")):
            st, h, body = self.get(path)
            self.assertEqual(st, 200, path)
            self.assertEqual(h["Content-Type"], ctype, path)
            self.assertGreater(len(body), 100, path)

    def test_root_synthetic_rendering_never_shows_online_and_checks_status(self):
        # 1. Root page serves preview HTML
        st, _, body = self.get("/")
        self.assertEqual(st, 200)
        html = body.decode("utf-8")
        self.assertNotIn("在线", html)

        # 2. Under SYNTHETIC source_mode, display API returns SYNTHETIC
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/v1/rooms/demo-room-01/display", headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        data = json.loads(r.read())
        c.close()
        self.assertEqual(data["source_mode"], "SYNTHETIC")

        # 3. Frontend JS contains status check and never displays online for synthetic
        _, _, js_body = self.get("/static/preview.js")
        js_code = js_body.decode("utf-8")
        self.assertIn('data.source_mode !== "LIVE"', js_code)
        self.assertIn('"合成测试数据"', js_code)
        self.assertIn('m.status === "CURRENT"', js_code)
        self.assertIn('m.status === "STALE"', js_code)

    def test_discover_and_auto_bind(self):
        dev = FakeDevice()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cpath = Path(tmp) / "config.json"
                res1 = discover.auto_bind(cpath, candidates=["http://127.0.0.1:9", dev.base_url], timeout=0.5)
                self.assertTrue(res1["discovered"])
                self.assertEqual(res1["mode"], "LIVE")
                self.assertEqual(res1["config"]["room_name_zh"], "睡眠房")
                self.assertEqual(res1["config"]["source"]["base_url"], dev.base_url)
                self.assertEqual(res1["binding_revision"], 1)

                # Second run with same IP does not bump binding_revision
                res2 = discover.auto_bind(cpath, candidates=[dev.base_url], timeout=0.5)
                self.assertTrue(res2["discovered"])
                self.assertFalse(res2["binding_updated"])
                self.assertEqual(res2["binding_revision"], 1)
        finally:
            dev.close()

    def test_eight_sleep_six_metrics_and_lookup(self):
        self.assertEqual(eight_sleep.heating_level_to_temp(-100), 13)
        self.assertEqual(eight_sleep.heating_level_to_temp(-17), 25)
        self.assertEqual(eight_sleep.heating_level_to_temp(-6), 26)
        self.assertEqual(eight_sleep.heating_level_to_temp(0), 27)
        self.assertEqual(eight_sleep.heating_level_to_temp(100), 44)

        bundle = eight_sleep.make_synthetic_bundle()
        extracted = eight_sleep.extract_six_metrics(
            bundle["trends_doc"], bundle["temp_doc"], bundle["device_doc"], side="solo"
        )
        self.assertEqual(set(extracted["metrics"].keys()), set(eight_sleep.EIGHT_SLEEP_METRICS))
        m = extracted["metrics"]
        # 2026-10-23 owner request: both sides are shown (device API reports both), plus night mean and base angles
        self.assertEqual((m["pod_left_bed_temp"]["value"], m["pod_left_target_temp"]["value"]), (25.0, 25.0))
        self.assertEqual((m["pod_right_bed_temp"]["value"], m["pod_right_target_temp"]["value"]), (22.0, 23.0))
        self.assertEqual((m["pod_left_heating"]["value"], m["pod_right_heating"]["value"]), (1.0, 0.0))
        self.assertEqual(m["pod_night_bed_temp"]["value"], 26.3)
        self.assertIsNone(m["pod_left_torso_angle"]["value"])   # base_doc not passed -> no angle, never a default
        self.assertEqual(extracted["metrics"]["pod_bed_temp"]["value"], 25.0)
        self.assertEqual(extracted["metrics"]["pod_bed_temp"]["raw_level"], -17)
        self.assertEqual(extracted["metrics"]["pod_target_bed_temp"]["value"], 25.0)
        self.assertEqual(extracted["metrics"]["pod_target_bed_temp"]["raw_level"], -17)

        # TD-3: Verification that left/right keys are completely eliminated from backend payload
        target_dict = extracted["metrics"]["pod_target_bed_temp"]
        for forbidden in ("left_level", "right_level", "left_temp_c", "right_temp_c", "leftTargetHeatingLevel", "rightTargetHeatingLevel"):
            self.assertNotIn(forbidden, target_dict)
            self.assertNotIn(forbidden, extracted["meta"])

        disp = projection.build_display(self.store, self.cfg)
        self.assertIn("eight_sleep", disp)
        self.assertEqual(len(disp["eight_sleep"]["metrics"]), len(eight_sleep.EIGHT_SLEEP_METRICS))
        self.assertEqual(
            [m["metric_id"] for m in disp["eight_sleep"]["metrics"]][:6],
            [
                "pod_heart_rate",
                "pod_hrv",
                "pod_respiratory_rate",
                "pod_bed_temp",
                "pod_target_bed_temp",
                "pod_room_temp",
            ],
        )

    def test_preview_html(self):
        for path in ("/preview.html", "/preview"):
            st, h, body = self.get(path)
            self.assertEqual(st, 200, path)
            self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")
            text = body.decode("utf-8")
            self.assertIn("空间监测探针", text)
            self.assertIn("智能温控床体", text)
            self.assertIn(">微环境气候分析<", text)
            self.assertIn("导出环境与床体数据并跳转微环境气候分析", text)
            # Zero hardware brand exposure
            for brand in ("AirGradient", "Eight Sleep", "8sleep"):
                self.assertNotIn(brand, text)
            # Strict CSP without unsafe-inline
            csp = h["Content-Security-Policy"]
            self.assertIn("default-src 'self'", csp)
            self.assertNotIn("'unsafe-inline'", csp)
            # Zero external links in HTML structure
            self.assertFalse(re.search(r"https?://", text))

    def test_preview_assets(self):
        for path, ctype in (
            ("/static/preview.css", "text/css; charset=utf-8"),
            ("/static/preview.js", "text/javascript; charset=utf-8"),
        ):
            st, h, body = self.get(path)
            self.assertEqual(st, 200, path)
            self.assertEqual(h["Content-Type"], ctype, path)
            self.assertGreater(len(body), 500, path)
            text = body.decode("utf-8")
            if path.endswith(".css"):
                self.assertNotIn("@import", text)
                self.assertNotIn("url(http", text)
            if path.endswith(".js"):
                # Zero fake math simulation
                self.assertNotIn("Math.sin(", text)

    def test_container_guard_allows_bridge_peer(self):
        c_srv = server.make_server(self.store, self.cfg, port=0, host="0.0.0.0")
        c_port = c_srv.server_address[1]
        server.serve_in_thread(c_srv)
        try:
            c = http.client.HTTPConnection("127.0.0.1", c_port, timeout=5)
            c.request("GET", "/healthz", headers={"Host": f"localhost:{c_port}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            c.close()
        finally:
            c_srv.shutdown()
            c_srv.server_close()

    def test_container_guard_strict_reverse_cases(self):
        # 1. IP whitelist logic
        self.assertTrue(server._is_allowed_client_ip("127.0.0.1", is_container=False))
        self.assertTrue(server._is_allowed_client_ip("::1", is_container=False))
        self.assertFalse(server._is_allowed_client_ip("172.17.0.1", is_container=False))
        self.assertTrue(server._is_allowed_client_ip("172.17.0.1", is_container=True))
        self.assertTrue(server._is_allowed_client_ip("172.20.0.5", is_container=True))
        self.assertFalse(server._is_allowed_client_ip("192.168.1.100", is_container=True))
        self.assertFalse(server._is_allowed_client_ip("10.0.0.2", is_container=True))
        self.assertFalse(server._is_allowed_client_ip("8.8.8.8", is_container=True))

        # 2. Host and Origin enforcement on 0.0.0.0 server
        c_srv = server.make_server(self.store, self.cfg, port=0, host="0.0.0.0")
        c_port = c_srv.server_address[1]
        server.serve_in_thread(c_srv)
        try:
            # Bad Host -> 421
            c = http.client.HTTPConnection("127.0.0.1", c_port, timeout=5)
            c.request("GET", "/healthz", headers={"Host": "evil.attacker.com"})
            r = c.getresponse()
            self.assertEqual(r.status, 421)
            c.close()

            # Bad Origin on PATCH -> 403
            c = http.client.HTTPConnection("127.0.0.1", c_port, timeout=5)
            c.request(
                "PATCH",
                "/api/v1/admin/display-fields",
                body=b'{"fields":[]}',
                headers={
                    "Host": f"127.0.0.1:{c_port}",
                    "Content-Type": "application/json",
                    "Origin": "http://evil.attacker.com",
                },
            )
            r = c.getresponse()
            self.assertEqual(r.status, 403)
            c.close()
        finally:
            c_srv.shutdown()
            c_srv.server_close()

    def test_preview_html_zero_initial_readings_and_no_online(self):
        html_path = Path(__file__).resolve().parents[2] / "room_display" / "static" / "preview.html"
        self.assertTrue(html_path.exists())
        content = html_path.read_text(encoding="utf-8")

        # Zero occurrence of '在线' or '连通' (U2)
        self.assertNotIn("在线", content)
        self.assertNotIn("连通", content)

        # 2026-10-23 owner layout: 2 + 3 bed cards mirroring the 3 + 2 room cards; no personal biometrics on screen
        for key in ("es-left", "es-right", "es-state", "es-night", "es-elev"):
            self.assertIn(f'id="card-{key}"', content)
        for gone in ("card-es-hr", "card-es-hrv", "card-es-rr", ">心率<", "心率变异性", ">呼吸率<"):
            self.assertNotIn(gone, content)
        # Static rating badges must not claim a grade regardless of the value
        for claim in (">EPA 优<", ">WHO 优<", ">舒适<", ">适中<"):
            self.assertNotIn(claim, content)

        # Value / badge / tag elements must contain no digits in initial HTML
        for line in content.splitlines():
            if re.search(r'id="(val-|badge-|tag-)', line):
                matches = re.findall(r">([^<]*[0-9][^<]*)<", line)
                self.assertEqual(matches, [], f"Initial element contains digits: {line}")

    def test_delivery_single_migration_bundle_and_private_readme(self):
        root_dir = Path(__file__).resolve().parents[4]

        # 1. Option B: starmap-deploy.zip must NOT exist
        deploy_zip = root_dir / "starmap-deploy.zip"
        self.assertFalse(deploy_zip.exists(), "starmap-deploy.zip must not exist (Option B single migration bundle)")

        # 2. OneClick migration bundle
        bundle_zip = root_dir / "var" / "ctds-c01" / "StarMap-C01-Mac-OneClick.zip"
        if not bundle_zip.exists():
            self.skipTest("bundle zip not present (running inside a deployed bundle or target machine)")

        # File permissions must be 0o600
        self.assertEqual(bundle_zip.stat().st_mode & 0o777, 0o600, "Bundle zip must have 0600 permissions")

        with zipfile.ZipFile(bundle_zip, "r") as zf:
            names = zf.namelist()
            # Must contain PRIVATE_README.md
            self.assertTrue(any(name.endswith("PRIVATE_README.md") for name in names), "PRIVATE_README.md missing from bundle")

            for name in names:
                # TD-2: Exclude deprecated dual-frontend files
                for dep in ("index.html", "app.js", "style.css"):
                    self.assertFalse(name.endswith(dep), f"bundle zip contains deprecated frontend file {dep}: {name}")
                # TD-4: Exclude frozen syn modules, fixtures, test_*_syn
                self.assertFalse(name.endswith("_syn.py"), f"bundle zip contains frozen syn module: {name}")
                self.assertFalse("/fixtures/" in name, f"bundle zip contains fixtures: {name}")
                self.assertFalse("test_" in name and "_syn" in name, f"bundle zip contains syn test: {name}")
                # If sqlite or config.json exist, they MUST be under StarMap-C01-Mac/var/ctds-c01/
                if name.endswith(".sqlite3"):
                    self.assertTrue(name.startswith("StarMap-C01-Mac/var/ctds-c01/"), f"sqlite in unexpected path: {name}")
                if name.endswith("config.json"):
                    self.assertTrue(name.startswith("StarMap-C01-Mac/var/ctds-c01/"), f"config.json in unexpected path: {name}")

            self.assertTrue(any(name.endswith("config.example.json") for name in names), "config.example.json missing from bundle zip")
            self.assertTrue(any(name.endswith("preview.html") for name in names), "preview.html missing from bundle zip")
            self.assertTrue(any(name.endswith("preview.js") for name in names), "preview.js missing from bundle zip")
            self.assertTrue(any(name.endswith("preview.css") for name in names), "preview.css missing from bundle zip")
            self.assertTrue(any(name.endswith("一键部署_StarMap.command") for name in names), "一键部署 launcher missing from bundle zip")
            self.assertTrue(any(name.endswith("一键打包_StarMap.command") for name in names), "一键打包 launcher missing from bundle zip")
            self.assertTrue(any(name.endswith("package_mac.sh") for name in names), "package_mac.sh missing from bundle zip")

    def test_auto_bind_ignores_es_env_vars_and_no_credentials(self):
        import os
        dev = FakeDevice()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cpath = Path(tmp) / "config.json"
                old_env = {}
                for k in ("EIGHT_SLEEP_EMAIL", "EIGHT_SLEEP_PASSWORD", "EIGHT_SLEEP_BEARER_TOKEN"):
                    old_env[k] = os.environ.get(k)
                    os.environ[k] = "leak-test"
                try:
                    res = discover.auto_bind(cpath, candidates=[dev.base_url], timeout=0.5)
                    self.assertTrue(res["discovered"])
                    self.assertEqual(res["eight_sleep_mode"], "READY")
                    saved_cfg = json.loads(cpath.read_text(encoding="utf-8"))
                    self.assertEqual(saved_cfg["eight_sleep"]["mode"], "READY")
                    for secret_k in ("email", "password", "bearer_token", "user_id", "device_id"):
                        self.assertNotIn(secret_k, saved_cfg["eight_sleep"])
                finally:
                    for k, v in old_env.items():
                        if v is None:
                            os.environ.pop(k, None)
                        else:
                            os.environ[k] = v
        finally:
            dev.close()

    def test_legacy_config_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            cpath = Path(tmp) / "config.json"
            legacy = dict(config.TEMPLATE)
            legacy["eight_sleep"] = {
                "source_id": "es-room-01",
                "mode": "LIVE",
                "email": "user@example.com",
                "password": "super-secret-password",
                "bearer_token": "token-12345",
                "user_id": "uid-6789",
                "device_id": "dev-0000",
                "side": "left",
                "binding_revision": 1,
            }
            initial_text = json.dumps(legacy, indent=2) + "\n"
            cpath.write_text(initial_text, encoding="utf-8")

            # Option B: config.load preserves LIVE mode and credentials, strictly read-only
            loaded = config.load(cpath)
            self.assertEqual(loaded["eight_sleep"]["mode"], "LIVE")
            self.assertEqual(loaded["eight_sleep"]["email"], "user@example.com")
            self.assertEqual(loaded["eight_sleep"]["bearer_token"], "token-12345")
            self.assertEqual(loaded["eight_sleep"]["side"], "solo")

            disk_after_load = cpath.read_text(encoding="utf-8")
            self.assertEqual(disk_after_load, initial_text, "config.load must not write to disk")

            # Option B: Explicit migration normalizes doc and sets chmod 600
            migrated = config.migrate_config_file(cpath)
            self.assertTrue(migrated)

            raw_on_disk = json.loads(cpath.read_text(encoding="utf-8"))
            self.assertEqual(raw_on_disk["eight_sleep"]["mode"], "LIVE")
            self.assertEqual(raw_on_disk["eight_sleep"]["email"], "user@example.com")
            self.assertEqual(raw_on_disk["eight_sleep"]["side"], "solo")
            self.assertEqual(cpath.stat().st_mode & 0o777, 0o600, "Config file must have 0600 permissions")

            # Explicit initialization vs pure loader error
            non_existent = Path(tmp) / "non_existent.json"
            with self.assertRaises(config.ConfigError):
                config.load(non_existent)
            self.assertFalse(non_existent.exists(), "config.load must not create missing files")
            self.assertTrue(config.init_config(non_existent))
            self.assertTrue(non_existent.exists(), "init_config must explicitly create file")
            self.assertEqual(non_existent.stat().st_mode & 0o777, 0o600, "Initialized config must have 0600 permissions")

    def test_eight_sleep_ready_mode_and_live_config_validation(self):
        # READY mode returns empty trend and None values
        ready_cfg = cfg()
        ready_cfg["eight_sleep"] = {"mode": "READY"}
        view = eight_sleep.build_view(self.store, ready_cfg)
        self.assertEqual(view["mode"], "READY")
        self.assertEqual(view["status"], "READY_NOT_CONNECTED")
        self.assertEqual(view["status_zh"], "待接入")
        self.assertEqual(view["device_class"], "Pod 6")
        for m in view["metrics"]:
            self.assertIsNone(m["value"])
            self.assertEqual(m["trend"], [])
            self.assertEqual(m["status"], "NO_OBSERVATION")

        # Option B: Config validator accepts LIVE mode
        live_cfg = cfg()
        live_cfg["eight_sleep"] = {
            "mode": "LIVE",
            "email": "test@example.com",
            "password": "test-password",
            "bearer_token": "token-abc",
        }
        validated = config.validate(live_cfg)
        self.assertEqual(validated["eight_sleep"]["mode"], "LIVE")
        self.assertEqual(validated["eight_sleep"]["email"], "test@example.com")

        # Config validator rejects unknown mode
        invalid_cfg = cfg()
        invalid_cfg["eight_sleep"] = {"mode": "INVALID_MODE"}
        with self.assertRaises(config.ConfigError):
            config.validate(invalid_cfg)

    def test_eight_sleep_auth_flow_and_token_refresh(self):
        # 1. Standard OAuth2 response format (access_token)
        srv = FakeEightSleepServer(compat_session_format=False)
        try:
            client = eight_sleep.EightSleepClient(
                email="user@example.com",
                password="valid-password",
                base_url=srv.base_url,
                auth_url=srv.auth_url,
            )
            token = client.authenticate()
            self.assertEqual(token, "valid-token-123")
            self.assertEqual(client.refresh_token, "new-refresh-token-888")
            self.assertEqual(client.user_id, "mock-user-001")

            # 2. Refresh with refresh token
            token2 = client.refresh_auth()
            self.assertEqual(token2, "refreshed-token-999")

            # 3. Refresh token fails, fallback to email+password
            srv.refresh_fails = True
            token3 = client.refresh_auth()
            self.assertEqual(token3, "valid-token-123")

            # 4. Total auth failure (wrong password and refresh token failed)
            client.password = "wrong"
            with self.assertRaises(adapter.AdapterError) as ctx:
                client.refresh_auth()
            self.assertEqual(ctx.exception.code, "AUTH_REJECTED")
        finally:
            srv.close()

        # 5. Compatibility mode: legacy session.token response format
        srv_compat = FakeEightSleepServer(compat_session_format=True)
        try:
            client_compat = eight_sleep.EightSleepClient(
                email="user@example.com",
                password="valid-password",
                base_url=srv_compat.base_url,
                auth_url=srv_compat.auth_url,
            )
            token_c = client_compat.authenticate()
            self.assertEqual(token_c, "valid-token-123")
            self.assertEqual(client_compat.refresh_token, "new-refresh-token-888")
            self.assertEqual(client_compat.user_id, "mock-user-001")
        finally:
            srv_compat.close()

    def test_eight_sleep_401_retry_once(self):
        srv = FakeEightSleepServer()
        try:
            srv.should_401_once = True
            client = eight_sleep.EightSleepClient(
                email="user@example.com",
                password="valid-password",
                bearer_token="expired-token",
                user_id="mock-user-001",
                device_id="mock-device-002",
                base_url=srv.base_url,
                auth_url=srv.auth_url,
            )
            # Fetch bundle encounters 401 on first GET, triggers refresh_auth once, succeeds
            res = client.fetch_bundle()
            self.assertEqual(res["outcome"], "OK")
            self.assertIsNotNone(res.get("trends_doc"))
            self.assertIsNotNone(res.get("temp_doc"))
            self.assertIsNotNone(res.get("device_doc"))
            self.assertEqual(client.bearer_token, "valid-token-123")
        finally:
            srv.close()

    def test_eight_sleep_zero_write_and_solo_metrics(self):
        srv = FakeEightSleepServer()
        try:
            client = eight_sleep.EightSleepClient(
                email="user@example.com",
                password="valid-password",
                base_url=srv.base_url,
                auth_url=srv.auth_url,
            )
            uid, did = client.discover()
            self.assertEqual(uid, "mock-user-001")
            self.assertEqual(did, "mock-device-002")

            bundle = client.fetch_bundle()
            self.assertEqual(bundle["outcome"], "OK")

            # Strict assertion: Read-only check on all HTTP requests
            for method, path, headers, body in srv.hits:
                self.assertIn(method, ("GET", "POST"), f"Unexpected HTTP method {method} on {path}")
                if method == "POST":
                    self.assertEqual(path, "/v1/tokens", f"POST to non-auth endpoint: {path}")
                self.assertNotIn("control", path.lower(), f"Write/control endpoint touched: {path}")

            # Verify extraction (own side + both sides, night mean, base angles)
            extracted = eight_sleep.extract_six_metrics(
                bundle["trends_doc"], bundle["temp_doc"], bundle["device_doc"], side="solo",
                user_id=bundle["user_id"], base_doc=bundle["base_doc"]
            )
            m = extracted["metrics"]
            self.assertEqual(m["pod_heart_rate"]["value"], 56.0)
            self.assertEqual(m["pod_hrv"]["value"], 62.5)
            self.assertEqual(m["pod_respiratory_rate"]["value"], 15.0)
            self.assertEqual(m["pod_bed_temp"]["value"], 25.0)
            self.assertEqual(m["pod_target_bed_temp"]["value"], 25.0)
            self.assertEqual(m["pod_room_temp"]["value"], 23.2)

            self.assertEqual(m["pod_night_bed_temp"]["value"], 26.5)
            self.assertEqual((m["pod_left_torso_angle"]["value"], m["pod_left_leg_angle"]["value"]), (12.0, 4.0))
            self.assertEqual(m["pod_right_heating"]["value"], 0.0)
            # The base hardware block (incl. MAC address) is dropped before anything is stored
            self.assertNotIn("hardwareInfo", bundle["base_doc"])
            self.assertNotIn("00:11:22:33:44:55", json.dumps(bundle["base_doc"]))

            # Ingest into store and verify SQLite observations
            ingest_cfg = cfg()
            ingest_cfg["eight_sleep"] = {
                "source_id": "pod-room-01",
                "mode": "LIVE",
                "side": "solo",
                "base_url": srv.base_url,
                "auth_url": srv.auth_url,
                "bearer_token": "valid-token-123",
                "user_id": "mock-user-001",
                "device_id": "mock-device-002",
            }
            outcome = eight_sleep.ingest_once(self.store, ingest_cfg, client=client, provenance="LIVE")
            self.assertEqual(outcome, "OK")
            for mid in eight_sleep.EIGHT_SLEEP_METRICS:
                obs = self.store.latest_observation(ingest_cfg["room_id"], mid)
                self.assertIsNotNone(obs)
                self.assertEqual(obs["value_status"], "OBSERVED")
                self.assertEqual(obs["data_provenance"], "LIVE")
        finally:
            srv.close()

    def test_bind_eight_sleep_cli(self):
        srv = FakeEightSleepServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "config.json"
                db_path = Path(tmp) / "isolated.sqlite3"
                argv = [
                    "--config", str(cfg_path),
                    "--db-path", str(db_path),
                    "--email", "user@example.com",
                    "--base-url", srv.base_url,
                    "--auth-url", srv.auth_url,
                    "--side", "solo",
                    "bind-eight-sleep",
                ]
                from contextlib import redirect_stdout
                from room_display import __main__ as rd_main

                buf = io.StringIO()
                old_sandbox = os.environ.get("CTDS_TEST_SANDBOX")
                os.environ["CTDS_TEST_SANDBOX"] = "1"
                try:
                    with mock.patch("getpass.getpass", return_value="valid-password"):
                        with redirect_stdout(buf):
                            rc = rd_main.main(argv)
                finally:
                    if old_sandbox is not None:
                        os.environ["CTDS_TEST_SANDBOX"] = old_sandbox
                    else:
                        os.environ.pop("CTDS_TEST_SANDBOX", None)

                self.assertEqual(rc, 0)
                out_json = json.loads(buf.getvalue().strip())
                self.assertEqual(out_json["eight_sleep_mode"], "LIVE")
                self.assertTrue(out_json["authenticated"])
                self.assertTrue(out_json["user_id_configured"])
                self.assertTrue(out_json["device_id_configured"])

                # Verify written config permissions and content
                self.assertTrue(cfg_path.exists())
                self.assertEqual(cfg_path.stat().st_mode & 0o777, 0o600, "Config file must be chmod 0600")
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["eight_sleep"]["mode"], "LIVE")
                self.assertEqual(saved["eight_sleep"]["email"], "user@example.com")
                self.assertEqual(saved["eight_sleep"]["bearer_token"], "valid-token-123")
                self.assertEqual(saved["eight_sleep"]["user_id"], "mock-user-001")
                self.assertEqual(saved["eight_sleep"]["device_id"], "mock-device-002")

                # Verify test DB received ingested data and real DB was untouched
                self.assertTrue(db_path.exists())
                test_store = Store(db_path)
                try:
                    test_counts = test_store.counts()
                finally:
                    test_store.close()
                self.assertGreater(test_counts["observation"], 0)

                # Verify no plain secrets leaked in CLI terminal output
                stdout_text = buf.getvalue()
                self.assertNotIn("valid-password", stdout_text)
                self.assertNotIn("valid-token-123", stdout_text)
        finally:
            srv.close()

    def test_suite_never_touches_real_runtime_db(self):
        import hashlib
        root_dir = Path(__file__).resolve().parents[4]
        real_db = root_dir / "var" / "ctds-c01" / "ctds_c01.sqlite3"
        if not real_db.exists():
            self.skipTest("real runtime db does not exist")

        def get_db_info():
            b = real_db.read_bytes()
            h = hashlib.sha256(b).hexdigest()
            conn = sqlite3.connect(f"file:{real_db.resolve().as_posix()}?mode=ro&immutable=1", uri=True)
            try:
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM observation")
                obs = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM ingestion_attempt")
                att = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM observation WHERE substr(metric_id, 1, 4) = 'pod_'")
                pod_obs = cur.fetchone()[0]
                # Mock fixture signature (FakeEightSleepServer): HRV 62.5 together with respiratory rate 15.0
                cur.execute("SELECT count(*) FROM observation a JOIN observation b ON a.source_event_identity LIKE "
                            "substr(b.source_event_identity, 1, length(b.source_event_identity) - length(b.metric_id)) || '%' "
                            "WHERE a.metric_id='pod_hrv' AND a.value=62.5 AND b.metric_id='pod_respiratory_rate' AND b.value=15.0")
                mock_rows = cur.fetchone()[0]
                return h, obs, att, (pod_obs, mock_rows)
            finally:
                conn.close()

        h_before, obs_before, att_before, pod_obs_before = get_db_info()

        # Execute mock bind and synthetic actions with sandbox enabled
        srv = FakeEightSleepServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "config.json"
                test_db = Path(tmp) / "temp.sqlite3"
                from room_display import __main__ as rd_main

                old_sb = os.environ.get("CTDS_TEST_SANDBOX")
                os.environ["CTDS_TEST_SANDBOX"] = "1"
                try:
                    with mock.patch("getpass.getpass", return_value="valid-password"):
                        buf = io.StringIO()
                        from contextlib import redirect_stdout
                        with redirect_stdout(buf):
                            rd_main.main([
                                "--config", str(cfg_path),
                                "--db-path", str(test_db),
                                "--email", "user@example.com",
                                "--base-url", srv.base_url,
                                "--auth-url", srv.auth_url,
                                "bind-eight-sleep",
                            ])
                finally:
                    if old_sb is not None:
                        os.environ["CTDS_TEST_SANDBOX"] = old_sb
                    else:
                        os.environ.pop("CTDS_TEST_SANDBOX", None)
        finally:
            srv.close()

        h_after, obs_after, att_after, pod_obs_after = get_db_info()
        self.assertEqual(h_before, h_after, "Real runtime DB hash changed during test execution!")
        self.assertEqual(obs_before, obs_after, "Real runtime DB observation count changed!")
        self.assertEqual(att_before, att_after, "Real runtime DB attempt count changed!")
        # Real Eight Sleep readings may legitimately exist (owner-bound account); tests must not add any,
        # and no row may carry the mock fixture signature.
        self.assertEqual(pod_obs_after[0], pod_obs_before[0], "Tests changed the real Eight Sleep rows!")
        self.assertEqual(pod_obs_after[1], 0, "Real runtime DB contains mock fixture observations!")

    def test_bind_eight_sleep_check_mode_does_not_modify_config(self):
        srv = FakeEightSleepServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "config.json"
                # Seed with valid mock credentials
                initial_doc = dict(
                    config.TEMPLATE,
                    eight_sleep={
                        "source_id": "pod-room-01",
                        "mode": "LIVE",
                        "side": "solo",
                        "base_url": srv.base_url,
                        "auth_url": srv.auth_url,
                        "bearer_token": "valid-token-123",
                        "user_id": "mock-user-001",
                        "device_id": "mock-device-002",
                    }
                )
                cfg_path.write_text(json.dumps(initial_doc), encoding="utf-8")
                import hashlib
                sha_before = hashlib.sha256(cfg_path.read_bytes()).hexdigest()

                from contextlib import redirect_stdout
                from room_display import __main__ as rd_main

                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = rd_main.main(["--config", str(cfg_path), "bind-eight-sleep", "--check"])

                self.assertEqual(rc, 0)
                out = json.loads(buf.getvalue().strip())
                self.assertTrue(out["authenticated"])
                self.assertTrue(out["device_ok"])
                self.assertEqual(out["outcome"], "OK")

                sha_after = hashlib.sha256(cfg_path.read_bytes()).hexdigest()
                self.assertEqual(sha_before, sha_after, "--check mode must not modify configuration file!")
        finally:
            srv.close()

    def test_bind_eight_sleep_interactive_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text(json.dumps(dict(config.TEMPLATE)), encoding="utf-8")

            from contextlib import redirect_stdout
            from room_display import __main__ as rd_main

            buf = io.StringIO()
            with mock.patch("builtins.input", return_value=""):
                with redirect_stdout(buf):
                    rc = rd_main.main(["--config", str(cfg_path), "bind-eight-sleep"])

            self.assertEqual(rc, 0)
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["eight_sleep"]["mode"], "READY")

    def test_bind_eight_sleep_interactive_retry_success(self):
        srv = FakeEightSleepServer()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp) / "config.json"
                db_path = Path(tmp) / "test.sqlite3"
                cfg_path.write_text(json.dumps(dict(config.TEMPLATE)), encoding="utf-8")

                from contextlib import redirect_stdout
                from room_display import __main__ as rd_main

                buf = io.StringIO()
                # 1st try: wrong password; 2nd try: valid-password
                with mock.patch("builtins.input", return_value="user@example.com"):
                    with mock.patch("getpass.getpass", side_effect=["wrong", "valid-password"]):
                        with redirect_stdout(buf):
                            rc = rd_main.main([
                                "--config", str(cfg_path),
                                "--db-path", str(db_path),
                                "--base-url", srv.base_url,
                                "--auth-url", srv.auth_url,
                                "bind-eight-sleep"
                            ])

                self.assertEqual(rc, 0)
                saved = json.loads(cfg_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["eight_sleep"]["mode"], "LIVE")
                self.assertEqual(saved["eight_sleep"]["bearer_token"], "valid-token-123")
        finally:
            srv.close()

    def test_official_8slp_url_validation_and_rejection(self):
        # 1. URL helper validation
        self.assertTrue(eight_sleep.is_official_8slp_url("https://client-api.8slp.net"))
        self.assertTrue(eight_sleep.is_official_8slp_url("https://auth-api.8slp.net/v1/tokens"))
        self.assertTrue(eight_sleep.is_official_8slp_url("https://8slp.net"))
        self.assertFalse(eight_sleep.is_official_8slp_url("http://client-api.8slp.net"))
        self.assertFalse(eight_sleep.is_official_8slp_url("https://attacker.8slp.net.evil.com"))
        self.assertFalse(eight_sleep.is_official_8slp_url("http://127.0.0.1:8000"))

        # 2. Rejection in non-sandbox mode
        old_sb = os.environ.pop("CTDS_TEST_SANDBOX", None)
        try:
            with self.assertRaises(adapter.AdapterError) as ctx:
                eight_sleep.EightSleepClient(base_url="http://evil.com/v1")
            self.assertEqual(ctx.exception.code, "CONFIG_ERROR")

            with self.assertRaises(config.ConfigError):
                config.validate({"eight_sleep": {"base_url": "http://evil.com/v1"}})
        finally:
            if old_sb is not None:
                os.environ["CTDS_TEST_SANDBOX"] = old_sb
            else:
                os.environ.setdefault("CTDS_TEST_SANDBOX", "1")

    def test_single_bundle_unpack_and_migration_self_test(self):
        root_dir = Path(__file__).resolve().parents[4]
        bundle_zip = root_dir / "var" / "ctds-c01" / "StarMap-C01-Mac-OneClick.zip"
        if os.environ.get("CTDS_SKIP_BUNDLE_TEST") == "1":
            self.skipTest("packaging gate: the freshly built bundle is self-tested after build (package_mac.sh step 5)")
        if not bundle_zip.exists():
            self.skipTest("StarMap-C01-Mac-OneClick.zip not present")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with zipfile.ZipFile(bundle_zip, "r") as zf:
                zf.extractall(tmp_p)

            extracted_root = tmp_p / "StarMap-C01-Mac"
            self.assertTrue(extracted_root.is_dir())
            readme_p = extracted_root / "PRIVATE_README.md"
            self.assertTrue(readme_p.is_file(), "PRIVATE_README.md must exist in unpacked bundle")

            db_p = extracted_root / "var" / "ctds-c01" / "ctds_c01.sqlite3"
            self.assertTrue(db_p.is_file(), "Snapshot sqlite3 must exist in migration bundle")

            conn = sqlite3.connect(db_p)
            try:
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM observation")
                obs_count = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM ingestion_attempt")
                att_count = cur.fetchone()[0]
                self.assertGreater(obs_count, 0, "Snapshot database must contain observation rows")
                self.assertGreater(att_count, 0, "Snapshot database must contain ingestion_attempt rows")
            finally:
                conn.close()

            # Execute self-test inside unpacked bundle environment
            proc = subprocess.run(
                [sys.executable, "-B", "-m", "room_display", "self-test"],
                cwd=str(extracted_root),
                env=dict(os.environ, PYTHONPATH="development/ctds"),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, f"self-test in unpacked bundle failed:\n{proc.stdout}\n{proc.stderr}")
            res_json = json.loads(proc.stdout.strip())
            self.assertEqual(res_json["result"], "PASS")
            self.assertEqual(res_json["failures"], 0)
            self.assertEqual(res_json["errors"], 0)

    def test_preview_js_pod6_solo_and_remediation_contracts(self):
        js_path = Path(__file__).resolve().parents[2] / "room_display" / "static" / "preview.js"
        self.assertTrue(js_path.exists())
        js = js_path.read_text(encoding="utf-8")

        # Metadata alignment
        for gone in ("pod_heart_rate", "pod_hrv", "pod_respiratory_rate"):
            self.assertNotIn(gone, js)   # personal biometrics are neither displayed nor exported
        self.assertNotIn("Math.sin(", js)

        # Critical functions and logic
        self.assertIn("setOfflineState", js)
        self.assertIn("evaluateCO2", js)
        self.assertIn("evaluatePM25", js)
        self.assertIn("currentAiSnapshot", js)
        self.assertIn("navigator.clipboard.writeText", js)

    def test_root_display_dynamic_data_driven_behavior(self):
        # 1. Byte-for-byte identity between / and /preview.html
        st1, h1, body1 = self.get("/")
        st2, h2, body2 = self.get("/preview.html")
        self.assertEqual(st1, 200)
        self.assertEqual(st2, 200)
        self.assertEqual(body1, body2)
        self.assertEqual(h1["Content-Type"], h2["Content-Type"])

        # 2. Structure and initial DOM content (zero numbers in value/badge elements)
        html = body1.decode("utf-8")
        val_matches = re.findall(r'id="val-[^"]+">([^<]*)<', html)
        self.assertTrue(len(val_matches) >= 8)
        for val in val_matches:
            self.assertEqual(val.strip(), "—", f"Initial value element must be placeholder '—': {val}")

        badge_matches = re.findall(r'id="badge-[^"]+">([^<]*)<', html)
        for b in badge_matches:
            self.assertIn(b.strip(), ("连接中", "待接入", "—"), f"Initial badge must be '连接中', '待接入', or '—': {b}")

        # 3. Dynamic projection logic verification under different states
        live_cfg = cfg()
        live_cfg["source"] = {"source_id": "ag-room-01", "mode": "LIVE", "base_url": "http://192.0.2.1"}
        live_disp = projection.build_display(self.store, live_cfg)
        self.assertEqual(live_disp["source_mode"], "LIVE")
        self.assertEqual(live_disp["room_id"], "demo-room-01")

        # 4. JavaScript threshold and data-flow parsing
        js_path = Path(__file__).resolve().parents[2] / "room_display" / "static" / "preview.js"
        js = js_path.read_text(encoding="utf-8")

        # Verify TD-7 Safari popup blocker prevention
        self.assertIn('window.open("about:blank", "_blank")', js)
        self.assertIn('newWin.location.href = url', js)
        self.assertIn('newWin.close()', js)

        # Verify offline state resets every metric to placeholder
        self.assertIn('el.textContent = "—"', js)
        self.assertIn('text: "Pod 6 · 待接入"', js)          # getEsBadgeInfo (behaviour in test_frontend_contract)
        self.assertIn('esBadge.textContent = "离线 · 服务未就绪"', js)

    def test_delivery_markdown_files_contain_no_forbidden_strings(self):
        root_dir = Path(__file__).resolve().parents[4]
        forbidden_patterns = [
            re.compile(r"Pod 5\b", re.IGNORECASE),
            re.compile(r"leftTarget", re.IGNORECASE),
            re.compile(r"rightTarget", re.IGNORECASE),
            re.compile(r"EIGHT_SLEEP_PASSWORD\s*=", re.IGNORECASE),
        ]

        checked_files = []

        # 1. Source operations runbook
        runbook = root_dir / "docs" / "operations" / "DEPLOYMENT_AND_RUNBOOK_ZH.md"
        if runbook.exists():
            text = runbook.read_text(encoding="utf-8")
            for pat in forbidden_patterns:
                matches = pat.findall(text)
                self.assertEqual(matches, [], f"Forbidden string {pat.pattern} found in {runbook}: {matches}")
            checked_files.append(str(runbook))

        # 2. Markdown files inside delivery zips
        bundle_zip = root_dir / "var" / "ctds-c01" / "StarMap-C01-Mac-OneClick.zip"
        if bundle_zip.exists():
            with zipfile.ZipFile(bundle_zip, "r") as zf:
                for info in zf.infolist():
                    if info.filename.endswith(".md"):
                        content = zf.read(info).decode("utf-8")
                        for pat in forbidden_patterns:
                            matches = pat.findall(content)
                            self.assertEqual(
                                matches,
                                [],
                                f"Forbidden string {pat.pattern} found in {bundle_zip.name} -> {info.filename}: {matches}",
                            )
                        checked_files.append(f"{bundle_zip.name}:{info.filename}")

        self.assertTrue(len(checked_files) > 0, "No markdown files checked")

    def test_preview_js_behavioral_node_suite(self):
        import shutil
        import subprocess

        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js runtime not found on host machine, skipping preview.js behavioral suite")

        js_path = Path(__file__).resolve().parents[2] / "room_display" / "static" / "preview.js"
        self.assertTrue(js_path.exists())

        test_script = f"""
const assert = require('assert');
const core = require({json.dumps(str(js_path))});

// 1. AirGradient badge logic: Non-LIVE must NEVER return '在线' even if connected
for (const mode of ['SYNTHETIC', 'READY', 'OFFLINE', 'UNKNOWN', null, undefined]) {{
    for (const conn of ['CONNECTED', 'DISCONNECTED', null, undefined]) {{
        const b = core.getAgBadgeInfo(mode, conn);
        assert.strictEqual(b.isOnline, false, `mode ${{mode}} conn ${{conn}} must not be online`);
        assert.strictEqual(b.text, '合成测试数据', `mode ${{mode}} text must be 合成测试数据`);
        assert.ok(!b.text.includes('在线'), `mode ${{mode}} text must not contain 在线`);
    }}
}}
const liveConnected = core.getAgBadgeInfo('LIVE', 'CONNECTED', 60);
assert.strictEqual(liveConnected.isOnline, true);
assert.strictEqual(liveConnected.text, '每 60 秒采集 · 在线');

const liveDisconnected = core.getAgBadgeInfo('LIVE', 'DISCONNECTED');
assert.strictEqual(liveDisconnected.isOnline, false);
assert.strictEqual(liveDisconnected.text, '离线 · 局域网未就绪');

// 2. formatMetricDisplay behavior across CURRENT, STALE, NO_DATA
const currentMetric = {{ value: 23.4, status: 'CURRENT' }};
const currentRes = core.formatMetricDisplay(currentMetric, false, core.evaluateTemp);
assert.strictEqual(currentRes.valStr, '23.4');
assert.strictEqual(currentRes.status, 'CURRENT');
assert.strictEqual(currentRes.isCurrent, true);
assert.strictEqual(currentRes.tagStr, '黄金适温 · 承托体温节律');

const staleMetric = {{ value: 23.4, status: 'STALE' }};
const staleRes = core.formatMetricDisplay(staleMetric, false, core.evaluateTemp);
assert.strictEqual(staleRes.valStr, '23.4');
assert.strictEqual(staleRes.tagStr, '黄金适温 · 承托体温节律');
assert.strictEqual(staleRes.status, 'STALE');
assert.strictEqual(staleRes.isCurrent, false);

for (const noData of [null, undefined, {{ value: null, status: 'NOT_PROVIDED' }}, {{ value: 0, status: 'NO_OBSERVATION' }}]) {{
    const ndRes = core.formatMetricDisplay(noData, false, core.evaluateTemp);
    assert.strictEqual(ndRes.valStr, '—');
    assert.strictEqual(ndRes.tagStr, '—');
    assert.strictEqual(ndRes.status, 'NO_DATA');
    assert.strictEqual(ndRes.isCurrent, false);
}}

// 3. Threshold evaluations and exact boundary assertions
// CO2: <=800, <=1000, >1000
assert.strictEqual(core.evaluateCO2(700), '空气清新 · 优良状态');
assert.strictEqual(core.evaluateCO2(800), '空气清新 · 优良状态');
assert.strictEqual(core.evaluateCO2(801), '轻度积累 · 建议通风');
assert.strictEqual(core.evaluateCO2(1000), '轻度积累 · 建议通风');
assert.strictEqual(core.evaluateCO2(1001), '超标闷顿 · 需开窗新风');

// PM2.5: <=10, <=15, <=35, >35
assert.strictEqual(core.evaluatePM25(10), '洁净纯澈 · 呼吸道零负担');
assert.strictEqual(core.evaluatePM25(11), '空气洁净 · WHO 优良基准');
assert.strictEqual(core.evaluatePM25(15), '空气洁净 · WHO 优良基准');
assert.strictEqual(core.evaluatePM25(16), '轻度微粒 · 建议开启净化');
assert.strictEqual(core.evaluatePM25(35), '轻度微粒 · 建议开启净化');
assert.strictEqual(core.evaluatePM25(36), '污染超标 · 呼吸负担偏高');

// Temp: <20, <=26, >26
assert.strictEqual(core.evaluateTemp(18), '室温偏凉 · 注意保暖防寒');
assert.strictEqual(core.evaluateTemp(19.9), '室温偏凉 · 注意保暖防寒');
assert.strictEqual(core.evaluateTemp(20), '黄金适温 · 承托体温节律');
assert.strictEqual(core.evaluateTemp(24), '黄金适温 · 承托体温节律');
assert.strictEqual(core.evaluateTemp(26), '黄金适温 · 承托体温节律');
assert.strictEqual(core.evaluateTemp(26.1), '室温偏热 · 建议制冷降温');

// Humidity: <40, <=60, >60
assert.strictEqual(core.evaluateHumidity(30), '空气干燥 · 建议开启加湿');
assert.strictEqual(core.evaluateHumidity(39.9), '空气干燥 · 建议开启加湿');
assert.strictEqual(core.evaluateHumidity(40), '舒缓恒湿 · 呼吸粘膜滋润');
assert.strictEqual(core.evaluateHumidity(50), '舒缓恒湿 · 呼吸粘膜滋润');
assert.strictEqual(core.evaluateHumidity(60), '舒缓恒湿 · 呼吸粘膜滋润');
assert.strictEqual(core.evaluateHumidity(60.1), '湿度偏高 · 建议除湿清爽');

// TVOC: <=100, <=200, >200
assert.strictEqual(core.evaluateTVOC(80), '清新无味 · 材质环保安全');
assert.strictEqual(core.evaluateTVOC(100), '清新无味 · 材质环保安全');
assert.strictEqual(core.evaluateTVOC(101), '轻度活跃 · 注意空气对流');
assert.strictEqual(core.evaluateTVOC(200), '轻度活跃 · 注意空气对流');
assert.strictEqual(core.evaluateTVOC(201), '挥发物超标 · 建议排风散气');

// 4. Offline reset elements and tags completeness
assert.ok(core.OFFLINE_RESET_ELEMENTS.length >= 8);
assert.ok(core.OFFLINE_RESET_TAGS.length >= 8);
assert.ok(core.OFFLINE_RESET_ELEMENTS.includes('val-co2'));
assert.ok(core.OFFLINE_RESET_TAGS.includes('tag-co2'));

// 5. Room ID validation and fallback
assert.strictEqual(core.resolveRoomId('bedroom-01'), 'bedroom-01');
assert.strictEqual(core.resolveRoomId('<script>alert(1)</script>', 'fallback-room'), 'fallback-room');
assert.strictEqual(core.resolveRoomId('bad room!', 'fallback-room'), 'fallback-room');
assert.strictEqual(core.resolveRoomId(null, 'valid-fallback'), 'valid-fallback');

// 6. U1 Modal open/close class constants
assert.strictEqual(core.MODAL_OPEN_CLASS, 'open');
assert.strictEqual(core.MODAL_HIDDEN_CLASS, 'crosshair-hidden');

// 7. U5 Timeline functions existence
assert.strictEqual(typeof core.initTimeline, 'function');
assert.strictEqual(typeof core.selectTimelineIndex, 'function');
assert.strictEqual(typeof core.renderHistoricalDashboard, 'function');

console.log('NODE_PREVIEW_SUITE_PASS');
"""

        proc = subprocess.run(
            [node_bin, "-e", test_script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Node behavioral test failed (code {proc.returncode}):\\nstdout: {proc.stdout}\\nstderr: {proc.stderr}",
        )
        self.assertIn("NODE_PREVIEW_SUITE_PASS", proc.stdout)

    def test_css_classes_match_js_and_zero_inline_styles(self):
        # U3: Zero inline styles in preview.html and admin.html
        static_dir = Path(__file__).resolve().parents[2] / "room_display" / "static"
        for fname in ("preview.html", "admin.html"):
            fpath = static_dir / fname
            self.assertTrue(fpath.exists())
            html = fpath.read_text(encoding="utf-8")
            self.assertNotIn("style=", html.lower(), f"{fname} contains inline style attribute violating strict CSP")

        # U1: All classes added via classList.add in preview.js must exist in preview.css
        js_code = (static_dir / "preview.js").read_text(encoding="utf-8")
        css_code = (static_dir / "preview.css").read_text(encoding="utf-8")
        added_classes = set(re.findall(r'classList\.add\("([^"]+)"\)', js_code))
        const_matches = re.findall(r'const\s+MODAL_OPEN_CLASS\s*=\s*"([^"]+)"', js_code)
        if const_matches:
            added_classes.add(const_matches[0])
        const_hidden = re.findall(r'const\s+MODAL_HIDDEN_CLASS\s*=\s*"([^"]+)"', js_code)
        if const_hidden:
            added_classes.add(const_hidden[0])

        for cls in added_classes:
            self.assertTrue(
                f".{cls}" in css_code or f"[{cls}]" in css_code,
                f"Class '{cls}' added by JS has no matching rule in preview.css",
            )

    def test_bind_eight_sleep_check_mode_error_discrimination(self):
        # U6: bind-eight-sleep --check distinguishes NO_CONNECTION vs AUTH_REJECTED
        from unittest.mock import patch
        from room_display import __main__ as rd_main
        import argparse

        with tempfile.TemporaryDirectory() as tmp:
            cpath = Path(tmp) / "config.json"
            cfg_data = dict(config.TEMPLATE)
            cfg_data["eight_sleep"] = {
                "mode": "LIVE",
                "email": "test@example.com",
                "password": "secret_password",
                "base_url": "https://client-api.8slp.net",
                "auth_url": "https://auth-api.8slp.net",
            }
            cpath.write_text(json.dumps(cfg_data), encoding="utf-8")

            # 1. Connection Error -> outcome: NO_CONNECTION
            args = argparse.Namespace(
                config=str(cpath),
                check=True,
                es_mode=None,
                email=None,
                side=None,
                tz=None,
            )
            with patch("room_display.eight_sleep.EightSleepClient.authenticate", side_effect=adapter.AdapterError(adapter.NO_CONNECTION, "DNS failure")):
                with patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = rd_main.cmd_bind_eight_sleep(args)
                    self.assertEqual(ret, 1)
                    res = json.loads(out.getvalue())
                    self.assertEqual(res["outcome"], adapter.NO_CONNECTION)
                    self.assertFalse(res["authenticated"])

            # 2. Auth Rejection -> outcome: AUTH_REJECTED
            with patch("room_display.eight_sleep.EightSleepClient.authenticate", side_effect=adapter.AdapterError(adapter.AUTH_REJECTED, "Invalid credentials")):
                with patch("sys.stdout", new_callable=io.StringIO) as out:
                    ret = rd_main.cmd_bind_eight_sleep(args)
                    self.assertEqual(ret, 1)
                    res = json.loads(out.getvalue())
                    self.assertEqual(res["outcome"], adapter.AUTH_REJECTED)
                    self.assertFalse(res["authenticated"])



if __name__ == "__main__":
    unittest.main()

