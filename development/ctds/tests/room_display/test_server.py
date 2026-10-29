import http.client
import json
import unittest

from _helpers import Clock, cfg

from room_display import collector, server
from room_display.store import Store


class ServerTest(unittest.TestCase):
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

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return r.status, dict(r.getheaders()), payload

    def test_bound_to_loopback(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_health_ready(self):
        self.assertEqual(self.req("GET", "/healthz")[0], 200)
        st, _, p = self.req("GET", "/readyz")
        self.assertEqual((st, p["ready"]), (200, True))

    def test_display_and_headers(self):
        st, h, p = self.req("GET", "/api/v1/rooms/demo-room-01/display")
        self.assertEqual(st, 200)
        self.assertEqual(p["source_mode"], "SYNTHETIC")
        self.assertIn("default-src 'self'", h["Content-Security-Policy"])
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Cache-Control"], "no-store")

    def test_other_room_404(self):
        self.assertEqual(self.req("GET", "/api/v1/rooms/other-room/display")[0], 404)

    def test_host_header_enforced(self):
        self.assertEqual(self.req("GET", "/healthz", headers={"Host": "evil.example:80"})[0], 421)

    def test_write_methods_denied(self):
        for m in ("POST", "PUT", "DELETE"):
            self.assertEqual(self.req(m, "/api/v1/rooms/demo-room-01/display", body={})[0], 405)
        self.assertEqual(self.req("PATCH", "/api/v1/rooms/demo-room-01/display", body={})[0], 405)

    def test_capabilities(self):
        st, _, p = self.req("GET", "/api/v1/sources/capabilities")
        self.assertEqual(st, 200)
        self.assertEqual(p["pod5"]["status"], "READY_NOT_CONNECTED")
        self.assertEqual(p["pod5"]["metrics"], [])
        self.assertTrue(all(c["availability"] == "DOC_ONLY" for c in p["airgradient"]))  # synthetic never VERIFIED

    def test_admin_patch_roundtrip(self):
        fields = [{"metric_id": "co2", "enabled": True, "display_order": 1, "label_zh": "二氧化碳浓度"},
                  {"metric_id": "pm2_5", "enabled": True, "display_order": 2, "label_zh": "PM2.5"}]
        st, _, p = self.req("PATCH", "/api/v1/admin/display-fields", body={"fields": fields},
                            headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 200)
        self.assertGreaterEqual(p["config_revision"], 1)
        _, _, d = self.req("GET", "/api/v1/rooms/demo-room-01/display")
        self.assertEqual(d["metrics"][0]["label_zh"], "二氧化碳浓度")

    def test_admin_patch_rejections(self):
        good = {"metric_id": "co2", "enabled": True, "display_order": 1, "label_zh": "CO2"}
        cases = [
            ({"fields": [dict(good, metric_id="heart_rate")]}, {}, 400),
            ({"fields": [dict(good, metric_id="serialno")]}, {}, 400),
            ({"fields": [dict(good, label_zh="<script>")]}, {}, 400),
            ({"fields": [dict(good, label_zh="")]}, {}, 400),
            ({"fields": [dict(good, enabled=1)]}, {}, 400),
            ({"fields": [dict(good, display_order=0)]}, {}, 400),
            ({"fields": [dict(good, extra=1)]}, {}, 400),
            ({"fields": [good, good]}, {}, 400),
            ({"fields": [good], "url": "http://x"}, {}, 400),
            ({"fields": [good]}, {"Content-Type": "text/plain"}, 415),
            ({"fields": [good]}, {"Origin": "http://evil.example"}, 403),
            ({"fields": [good]}, {"Origin": f"http://0.0.0.0:{self.port}"}, 403),
        ]
        rev = self.store.config_revision("demo-room-01")
        for body, headers, code in cases:
            self.assertEqual(self.req("PATCH", "/api/v1/admin/display-fields", body=body, headers=headers)[0], code,
                             (body, headers))
        self.assertEqual(self.store.config_revision("demo-room-01"), rev)  # nothing saved

    def test_unknown_path_and_traversal(self):
        for p in ("/static/../server.py", "/static/x.js", "/etc/passwd", "/api/v1/admin"):
            self.assertEqual(self.req("GET", p)[0], 404, p)

    def test_container_and_network_ip_filtering(self):
        # 1. Loopback is always allowed
        self.assertTrue(server._is_allowed_client_ip("127.0.0.1", is_container=False))
        self.assertTrue(server._is_allowed_client_ip("127.0.0.1", is_container=True))

        # 2. Outside container, Docker bridge 172.16.0.0/12 is REJECTED (TD-6)
        self.assertFalse(server._is_allowed_client_ip("172.17.0.1", is_container=False))
        self.assertFalse(server._is_allowed_client_ip("172.16.0.2", is_container=False))

        # 3. Inside container, Docker bridge 172.16.0.0/12 is ALLOWED
        self.assertTrue(server._is_allowed_client_ip("172.17.0.1", is_container=True))
        self.assertTrue(server._is_allowed_client_ip("172.16.0.2", is_container=True))

        # 4. External LAN IP is rejected in container unless explicitly allowlisted
        self.assertFalse(server._is_allowed_client_ip("192.168.1.50", is_container=True))

        # 5. Explicit CTDS_ALLOWED_NETWORKS works
        import os
        old = os.environ.get("CTDS_ALLOWED_NETWORKS")
        try:
            os.environ["CTDS_ALLOWED_NETWORKS"] = "192.168.1.0/24"
            self.assertTrue(server._is_allowed_client_ip("192.168.1.50", is_container=False))
            self.assertFalse(server._is_allowed_client_ip("10.0.0.1", is_container=False))
        finally:
            if old is None:
                os.environ.pop("CTDS_ALLOWED_NETWORKS", None)
            else:
                os.environ["CTDS_ALLOWED_NETWORKS"] = old


if __name__ == "__main__":
    unittest.main()
