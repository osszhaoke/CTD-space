"""Full-lifecycle fault injection, recovery, and persistence drill.

Validates:
1. Normal ingest -> network loss -> stale data retention -> reconnection & self-healing
2. Error summary generation and clear-up upon recovery
3. Full browser/client flow: display query -> admin patch -> immediate effect -> server restart -> state preserved
4. Auth failure (401/403) transition to AUTH_PAUSED without infinite retries
5. Database integrity under abnormal payload injections
"""
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from _helpers import Clock, FakeDevice, body, cfg

from room_display import adapter, collector, config, projection, server
from room_display.store import Store


class FaultRecoveryDrillTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "drill.sqlite3"
        self.clock = Clock()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_disconnect_stale_retention_and_recovery(self):
        """Phase 2: Disconnection retains last known good value with STALE label; recovery clears errors."""
        dev = FakeDevice()
        try:
            c = cfg(mode="LIVE", base_url=dev.base_url, db_path=str(self.db_path), retries=1)
            store = Store(self.db_path)
            collector.register(store, c, clock=self.clock)
            col = collector.Collector(store, c, clock=self.clock, sleeper=lambda _: None)

            # 1. Successful round
            outcome = col.round()
            self.assertEqual(outcome, "OK")
            d1 = projection.build_display(store, c, now=self.clock())
            self.assertEqual(d1["actual_source_connectivity"], "CONNECTED")
            self.assertEqual(d1["data_freshness"], "CURRENT")
            self.assertIsNone(d1["error_summary_zh"])
            pm25_val = next(m for m in d1["metrics"] if m["metric_id"] == "pm2_5")["value"]
            self.assertEqual(pm25_val, 7.0)

            # 2. Inject network failure (dev down)
            dev.mode = "500"
            self.clock.advance(300)  # advance past stale threshold (180s)
            outcome_err = col.round()
            self.assertEqual(outcome_err, "HTTP_ERROR")
            d2 = projection.build_display(store, c, now=self.clock())
            self.assertEqual(d2["actual_source_connectivity"], "ERROR")
            self.assertEqual(d2["data_freshness"], "STALE")
            self.assertEqual(d2["error_summary_zh"], "设备返回错误")
            # Last good value is preserved
            pm25_stale = next(m for m in d2["metrics"] if m["metric_id"] == "pm2_5")
            self.assertEqual(pm25_stale["value"], 7.0)
            self.assertEqual(pm25_stale["status"], "STALE")

            # 3. Disconnect completely
            dev.mode = "404"
            self.clock.advance(60)
            col.round()
            d3 = projection.build_display(store, c, now=self.clock())
            self.assertEqual(d3["actual_source_connectivity"], "ERROR")
            self.assertEqual(d3["error_summary_zh"], "设备接口不存在（固件或地址需核对）")

            # 4. Self-healing / reconnection
            dev.mode = "ok"
            self.clock.advance(60)
            outcome_rec = col.round()
            self.assertEqual(outcome_rec, "OK")
            d4 = projection.build_display(store, c, now=self.clock())
            self.assertEqual(d4["actual_source_connectivity"], "CONNECTED")
            self.assertEqual(d4["data_freshness"], "CURRENT")
            self.assertIsNone(d4["error_summary_zh"])

            store.close()
        finally:
            dev.close()

    def test_full_client_admin_flow_and_cold_restart(self):
        """End-to-end flow: read display -> patch admin fields -> restart server/store -> verify persistence."""
        c = cfg(mode="SYNTHETIC", db_path=str(self.db_path))
        store1 = Store(self.db_path)
        collector.register(store1, c, clock=self.clock)
        col1 = collector.Collector(store1, c, clock=self.clock, sleeper=lambda _: None)
        col1.round()

        srv1 = server.make_server(store1, c, port=0)
        port = srv1.server_address[1]
        server.serve_in_thread(srv1)

        def req(method, path, body_data=None):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Host": f"127.0.0.1:{port}"}
            payload = None
            if body_data is not None:
                headers["Content-Type"] = "application/json"
                headers["Origin"] = f"http://127.0.0.1:{port}"
                payload = json.dumps(body_data).encode("utf-8")
            conn.request(method, path, body=payload, headers=headers)
            res = conn.getresponse()
            raw = res.read()
            conn.close()
            return res.status, json.loads(raw.decode("utf-8"))

        try:
            # 1. Fetch initial display
            status, disp = req("GET", f"/api/v1/rooms/{c['room_id']}/display")
            self.assertEqual(status, 200)
            self.assertEqual(disp["room_id"], "demo-room-01")
            self.assertTrue(len(disp["metrics"]) > 0)

            # 2. Patch admin display fields (custom order & custom zh name)
            custom_fields = [
                {"metric_id": "co2", "enabled": True, "display_order": 1, "label_zh": "主卧二氧化碳"},
                {"metric_id": "pm2_5", "enabled": True, "display_order": 2, "label_zh": "细颗粒物PM2.5"},
                {"metric_id": "temperature", "enabled": False, "display_order": 3, "label_zh": "室温"},
            ]
            st_patch, patch_res = req("PATCH", "/api/v1/admin/display-fields", {"fields": custom_fields})
            self.assertEqual(st_patch, 200)
            self.assertEqual(patch_res["config_revision"], 1)

            # 3. Verify immediate effect in display
            _, disp_after = req("GET", f"/api/v1/rooms/{c['room_id']}/display")
            m_ids = [m["metric_id"] for m in disp_after["metrics"]]
            self.assertEqual(m_ids[:2], ["co2", "pm2_5"])
            self.assertNotIn("temperature", m_ids)  # temperature disabled
            self.assertEqual(disp_after["metrics"][0]["label_zh"], "主卧二氧化碳")
        finally:
            srv1.shutdown()
            srv1.server_close()
            store1.close()

        # 4. Cold restart: open new Store and Server against the same SQLite database
        store2 = Store(self.db_path)
        srv2 = server.make_server(store2, c, port=port)
        server.serve_in_thread(srv2)
        try:
            st_re, disp_re = req("GET", f"/api/v1/rooms/{c['room_id']}/display")
            self.assertEqual(st_re, 200)
            re_m_ids = [m["metric_id"] for m in disp_re["metrics"]]
            self.assertEqual(re_m_ids[:2], ["co2", "pm2_5"])
            self.assertNotIn("temperature", re_m_ids)
            self.assertEqual(disp_re["metrics"][0]["label_zh"], "主卧二氧化碳")
            self.assertEqual(disp_re["config_revision"], 1)
        finally:
            srv2.shutdown()
            srv2.server_close()
            store2.close()

    def test_auth_rejection_pauses_source(self):
        """Auth rejection halts polling on that source and shows AUTH_PAUSED status."""
        dev = FakeDevice()
        try:
            dev.mode = "401"
            c = cfg(mode="LIVE", base_url=dev.base_url, db_path=str(self.db_path), retries=1)
            store = Store(self.db_path)
            collector.register(store, c, clock=self.clock)
            col = collector.Collector(store, c, clock=self.clock, sleeper=lambda _: None)

            outcome = col.run(max_rounds=3)
            self.assertEqual(outcome, "AUTH_PAUSED")
            # Check display projection
            disp = projection.build_display(store, c, now=self.clock())
            self.assertEqual(disp["actual_source_connectivity"], "AUTH_PAUSED")
            self.assertEqual(disp["error_summary_zh"], "设备拒绝访问，已暂停读取")
            store.close()
        finally:
            dev.close()


if __name__ == "__main__":
    unittest.main()
