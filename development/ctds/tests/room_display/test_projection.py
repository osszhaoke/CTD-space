import json
import unittest

from _helpers import Clock, body, cfg

from room_display import adapter, collector, projection
from room_display.store import Store


def fetch_from(seq, clock):
    """seq items: dict(pm02=...) overrides for OK, or an outcome string."""
    items = list(seq)

    def fetch(_url, *, timeout):
        it = items.pop(0)
        t = clock()
        if isinstance(it, str):
            return {"outcome": it, "http_status": None, "body": None, "request_started_at": t,
                    "response_received_at": None}
        return {"outcome": "OK", "http_status": 200, "body": body(**it), "request_started_at": t,
                "response_received_at": t}
    return fetch


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.clock, self.store, self.cfg = Clock(), Store(":memory:"), cfg(retries=0)
        collector.register(self.store, self.cfg, self.clock)

    def tearDown(self):
        self.store.close()

    def run_seq(self, seq, step=60):
        col = collector.Collector(self.store, self.cfg, fetcher=fetch_from(seq, self.clock), clock=self.clock,
                                  sleeper=lambda _: None)
        for _ in seq:
            col.round()
            self.clock.advance(step)

    def metric(self, d, mid):
        return next(m for m in d["metrics"] if m["metric_id"] == mid)

    def test_no_data(self):
        d = projection.build_display(self.store, self.cfg, self.clock())
        self.assertEqual((d["actual_source_connectivity"], d["data_freshness"]), ("NEVER_CONNECTED", "NO_DATA"))
        self.assertEqual(self.metric(d, "pm2_5")["status"], "NO_DATA")

    def test_current_then_stale_keeps_original_time(self):
        self.run_seq([{}])
        d = projection.build_display(self.store, self.cfg, self.clock())
        m = self.metric(d, "pm2_5")
        self.assertEqual((m["status"], m["value"], d["data_freshness"]), ("CURRENT", 7.0, "CURRENT"))
        t_obs = m["observed_or_received_at"]
        self.clock.advance(600)
        d = projection.build_display(self.store, self.cfg, self.clock())
        m = self.metric(d, "pm2_5")
        self.assertEqual((m["status"], m["status_zh"], d["data_freshness"]), ("STALE", "已过期", "STALE"))
        self.assertEqual(m["observed_or_received_at"], t_obs)  # old value not refreshed to now
        self.assertGreaterEqual(m["age_s"], 600)

    def test_disconnect_shows_last_good_and_error(self):
        self.run_seq([{}, "NO_CONNECTION", "TIMEOUT", "NO_CONNECTION", "NO_CONNECTION"])
        d = projection.build_display(self.store, self.cfg, self.clock())
        m = self.metric(d, "pm2_5")
        self.assertEqual(d["actual_source_connectivity"], "DISCONNECTED")
        self.assertEqual(d["error_summary_zh"], "无法连接设备")
        self.assertEqual((m["value"], m["status"]), (7.0, "STALE"))

    def test_null_after_good_shows_status_and_last_good(self):
        self.run_seq([{}, {"pm02": None}])
        m = self.metric(projection.build_display(self.store, self.cfg, self.clock()), "pm2_5")
        self.assertEqual((m["status"], m["value"], m["latest_value_status"]), ("EXPLICIT_NULL", 7.0, "EXPLICIT_NULL"))

    def test_zero_is_current_not_missing(self):
        self.run_seq([{"pm02": 0}])
        m = self.metric(projection.build_display(self.store, self.cfg, self.clock()), "pm2_5")
        self.assertEqual((m["status"], m["value"]), ("CURRENT", 0.0))

    def test_trend_window_and_order(self):
        self.run_seq([{"pm02": v} for v in (1, 2, 3, 4)])
        m = self.metric(projection.build_display(self.store, self.cfg, self.clock()), "pm2_5")
        self.assertEqual([p[1] for p in m["trend"]], [1.0, 2.0, 3.0, 4.0])

    def test_downsample_bound(self):
        pts = [[str(i), float(i)] for i in range(1000)]
        out = projection._downsample(pts)
        self.assertEqual(len(out), projection.TREND_POINTS)
        self.assertEqual(out[-1], pts[-1])

    def test_live_screen_excludes_synthetic(self):
        self.run_seq([{}])
        live = cfg(mode="LIVE", base_url="http://192.0.2.10", retries=0)
        d = projection.build_display(self.store, live, self.clock())
        self.assertEqual(d["source_mode"], "LIVE")
        self.assertEqual(d["actual_source_connectivity"], "NEVER_CONNECTED")
        self.assertTrue(all(m["value"] is None for m in d["metrics"]))

    def test_whitelist_no_identity_or_address(self):
        self.run_seq([{}])
        live = cfg(mode="LIVE", base_url="http://192.0.2.10", retries=0)
        for c in (self.cfg, live):
            blob = json.dumps(projection.build_display(self.store, c, self.clock()), ensure_ascii=False)
            for forbidden in ("fake000000", "serialno", "192.0.2.10", "base_url", "wifi", "firmware"):
                self.assertNotIn(forbidden, blob)

    def test_pod_ready_placeholder_only(self):
        d = projection.build_display(self.store, self.cfg, self.clock())
        self.assertEqual(d["pod"]["status"], "READY_NOT_CONNECTED")
        self.assertEqual(set(d["pod"]), {"label_zh", "status", "status_zh", "note_zh"})

    def test_hidden_metric_not_in_display(self):
        self.store.save_display_settings("demo-room-01", [{"metric_id": "co2", "enabled": False, "display_order": 1,
                                                            "label_zh": "二氧化碳"}], action="T",
                                         updated_at=adapter.iso(self.clock()))
        ids = [m["metric_id"] for m in projection.build_display(self.store, self.cfg, self.clock())["metrics"]]
        self.assertNotIn("co2", ids)


if __name__ == "__main__":
    unittest.main()
