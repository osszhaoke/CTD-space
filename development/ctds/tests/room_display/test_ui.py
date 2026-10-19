import http.client
from pathlib import Path
import re
import tempfile
import unittest

from _helpers import Clock, FakeDevice, cfg

from room_display import collector, discover, eight_sleep, projection, server
from room_display.store import Store


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

    def test_index_html(self):
        st, h, body = self.get("/")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "text/html; charset=utf-8")
        text = body.decode("utf-8")
        self.assertIn("房间空气质量大屏", text)
        self.assertIn("睡眠房", text)
        self.assertIn('data-theme="dark"', text)
        self.assertIn("室内环境", text)
        self.assertIn("床微环境", text)
        self.assertIn("viewport-fit=cover", text)
        self.assertIn("apple-mobile-web-app-capable", text)
        self.assertIn('id="pod-metrics-grid"', text)
        self.assertIn("待接入", text)
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
        self.assertIn("Pod 5", text)
        self.assertFalse(re.search(r"https?://", text))

    def test_static_assets(self):
        for path, ctype in (("/static/style.css", "text/css; charset=utf-8"),
                            ("/static/app.js", "text/javascript; charset=utf-8"),
                            ("/static/admin.js", "text/javascript; charset=utf-8")):
            st, h, body = self.get(path)
            self.assertEqual(st, 200, path)
            self.assertEqual(h["Content-Type"], ctype, path)
            self.assertGreater(len(body), 100, path)

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
            bundle["trends_doc"], bundle["temp_doc"], bundle["device_doc"], side="left"
        )
        self.assertEqual(
            set(extracted["metrics"].keys()),
            {
                "pod_heart_rate",
                "pod_hrv",
                "pod_respiratory_rate",
                "pod_bed_temp",
                "pod_target_bed_temp",
                "pod_room_temp",
            },
        )
        self.assertEqual(extracted["metrics"]["pod_bed_temp"]["value"], 25.0)
        self.assertEqual(extracted["metrics"]["pod_bed_temp"]["raw_level"], -17)
        self.assertEqual(extracted["metrics"]["pod_target_bed_temp"]["value"], 25.0)
        self.assertEqual(extracted["metrics"]["pod_target_bed_temp"]["raw_level"], -17)

        disp = projection.build_display(self.store, self.cfg)
        self.assertIn("eight_sleep", disp)
        self.assertEqual(len(disp["eight_sleep"]["metrics"]), 6)
        self.assertEqual(
            [m["metric_id"] for m in disp["eight_sleep"]["metrics"]],
            [
                "pod_heart_rate",
                "pod_hrv",
                "pod_respiratory_rate",
                "pod_bed_temp",
                "pod_target_bed_temp",
                "pod_room_temp",
            ],
        )


if __name__ == "__main__":
    unittest.main()

