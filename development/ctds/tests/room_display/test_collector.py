import unittest

from _helpers import T0, Clock, FakeDevice, body, cfg

from room_display import adapter, collector, projection
from room_display.store import Store


def scripted(outcomes, clock):
    seq = list(outcomes)

    def fetch(_url, *, timeout):
        o = seq.pop(0) if seq else "OK"
        t = clock()
        return {"outcome": o, "http_status": {"OK": 200, "AUTH_REJECTED": 401}.get(o),
                "body": body() if o == "OK" else (b"{bad" if o == "BADBODY" else None),
                "request_started_at": t, "response_received_at": t if o != "TIMEOUT" else None}
    return fetch


def badbody_fetch(clock):
    def fetch(_url, *, timeout):
        t = clock()
        return {"outcome": "OK", "http_status": 200, "body": b'{"pm02":1,"pm02":2}',
                "request_started_at": t, "response_received_at": t}
    return fetch


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.clock, self.store, self.cfg = Clock(), Store(":memory:"), cfg()
        collector.register(self.store, self.cfg, self.clock)
        self.sleeps = []

    def tearDown(self):
        self.store.close()

    def make(self, fetcher):
        return collector.Collector(self.store, self.cfg, fetcher=fetcher, clock=self.clock,
                                   sleeper=self.sleeps.append)

    def test_bounded_retry(self):
        col = self.make(scripted(["TIMEOUT"] * 10, self.clock))
        self.assertEqual(col.round(), "TIMEOUT")
        self.assertEqual(self.sleeps, [2, 4])
        self.assertEqual(self.store.counts()["ingestion_attempt"], 3)
        self.assertEqual(self.store.counts()["observation"], 0)

    def test_retry_recovers(self):
        col = self.make(scripted(["NO_CONNECTION", "OK"], self.clock))
        self.assertEqual(col.round(), "OK")
        self.assertEqual(col.consecutive_failures, 0)
        self.assertEqual(self.store.counts(), {"observation": 9, "ingestion_attempt": 2})

    def test_auth_pauses_without_retry(self):
        col = self.make(scripted(["AUTH_REJECTED"], self.clock))
        self.assertEqual(col.run(max_rounds=5), "AUTH_PAUSED")
        self.assertEqual(self.store.counts()["ingestion_attempt"], 1)
        self.assertEqual(self.store.source("ag-test-01")["status"], "AUTH_PAUSED")
        self.assertEqual(projection.build_display(self.store, self.cfg, self.clock())["actual_source_connectivity"],
                         "AUTH_PAUSED")

    def test_backoff_capped_and_not_permanent_stop(self):
        col = self.make(scripted(["NOT_FOUND"] * 6 + ["OK"], self.clock))
        delays = []
        for _ in range(6):
            col.round()
            delays.append(col.next_delay())
        self.assertEqual(delays, [60, 120, 240, 300, 300, 300])
        self.assertEqual(col.round(), "OK")  # service continues after failures
        self.assertEqual(col.next_delay(), 60)

    def test_invalid_payload_recorded_no_observations(self):
        col = self.make(badbody_fetch(self.clock))
        self.assertEqual(col.round(), "INVALID_PAYLOAD")
        a = self.store.latest_attempt("ag-test-01")
        self.assertEqual(a["outcome"], "INVALID_PAYLOAD")
        self.assertEqual(len(a["payload_sha256"]), 64)
        self.assertEqual(self.store.counts()["observation"], 0)

    def test_synthetic_mode_tags_synthetic(self):
        col = collector.Collector(self.store, self.cfg, clock=self.clock, sleeper=lambda _: None)
        self.assertEqual(col.round(), "OK")
        o = self.store.latest_observation("demo-room-01", "pm2_5")
        self.assertEqual(o["data_provenance"], "SYNTHETIC")
        self.assertEqual(self.store.verified_metrics("ag-test-01"), set())

    def test_stop_interrupts_run(self):
        col = collector.Collector(self.store, self.cfg, clock=self.clock)
        col.stop()
        self.assertEqual(col.run(), "STOPPED")


class LoopbackPipelineTest(unittest.TestCase):
    """Real HTTP path against a loopback FAKE device. Tagged REPLAY: shape-level, not LIVE proof."""

    def test_fetch_normalize_store_project(self):
        dev = FakeDevice()
        try:
            c = cfg(mode="LIVE", base_url=dev.base_url)
            s = Store(":memory:")
            collector.register(s, c)
            outcome = collector.ingest_once(s, c, adapter.fetch, provenance="REPLAY")
            self.assertEqual(outcome, "OK")
            self.assertEqual(s.counts(), {"observation": 9, "ingestion_attempt": 1})
            # LIVE screen must not show REPLAY data
            d = projection.build_display(s, c)
            self.assertEqual(d["actual_source_connectivity"], "NEVER_CONNECTED")
            self.assertTrue(all(m["value"] is None for m in d["metrics"]))
            s.close()
        finally:
            dev.close()


if __name__ == "__main__":
    unittest.main()
