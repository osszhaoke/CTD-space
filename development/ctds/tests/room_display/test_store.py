import sqlite3
import tempfile
import unittest
from pathlib import Path

from _helpers import T0, Clock, body, cfg

from room_display import adapter, collector, projection
from room_display.store import Store


def attempt(aid, outcome="OK", t=T0, prov="SYNTHETIC"):
    return {"attempt_id": aid, "source_id": "ag-test-01", "request_started_at": adapter.iso(t),
            "response_received_at": adapter.iso(t), "outcome": outcome, "http_status": 200,
            "payload_sha256": None, "data_provenance": prov}


def obs(aid, t=T0, prov="SYNTHETIC", **kw):
    o, _ = adapter.normalize(body(**kw), source_id="ag-test-01", room_id="demo-room-01", attempt_id=aid,
                             received_at=t, provenance=prov)
    return o


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def tearDown(self):
        self.s.close()

    def test_idempotent_rerecord(self):
        self.assertEqual(self.s.record_attempt(attempt("a1"), obs("a1")), 9)
        self.assertEqual(self.s.record_attempt(attempt("a1"), obs("a1")), 0)
        self.assertEqual(self.s.counts(), {"observation": 9, "ingestion_attempt": 1})

    def test_same_value_different_receipts_kept(self):
        self.s.record_attempt(attempt("a1"), obs("a1"))
        t1 = Clock().advance(60)
        self.s.record_attempt(attempt("a2", t=t1), obs("a2", t=t1))
        self.assertEqual(len(self.s.series("demo-room-01", "pm2_5", adapter.iso(T0))), 2)

    def test_append_only(self):
        self.s.record_attempt(attempt("a1"), obs("a1"))
        for sql in ("UPDATE observation SET value=1", "DELETE FROM observation",
                    "UPDATE ingestion_attempt SET outcome='X'", "DELETE FROM ingestion_attempt"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.s.conn.execute(sql)
        self.assertEqual(self.s.counts()["observation"], 9)

    def test_check_violation_not_silently_dropped(self):
        bad = obs("a1")
        bad[0] = dict(bad[0], value_status="OBSERVED", value=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.s.record_attempt(attempt("a1"), bad)
        self.assertEqual(self.s.counts(), {"observation": 0, "ingestion_attempt": 0})  # whole tx rolled back

    def test_display_settings_revision(self):
        self.assertEqual(self.s.config_revision("demo-room-01"), 0)
        rev = self.s.save_display_settings("demo-room-01", [
            {"metric_id": "co2", "enabled": True, "display_order": 1, "label_zh": "CO₂"}],
            action="TEST", updated_at=adapter.iso(T0))
        self.assertEqual(rev, 1)
        first = self.s.display_settings("demo-room-01")[0]
        self.assertEqual((first["metric_id"], first["label_zh"]), ("co2", "CO₂"))


class ColdRestartTest(unittest.TestCase):
    def test_reopen_reads_back_same_history_and_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c01.sqlite3"
            c = cfg()
            clock = Clock()
            s = Store(path)
            collector.register(s, c, clock)
            col = collector.Collector(s, c, clock=clock, sleeper=lambda _: None)
            for _ in range(3):
                col.round()
                clock.advance(60)
            s.save_display_settings("demo-room-01", [{"metric_id": "pm10", "enabled": True, "display_order": 1,
                                                       "label_zh": "PM10颗粒"}], action="TEST", updated_at=adapter.iso(T0))
            before = projection.build_display(s, c, clock())
            s.close()
            s2 = Store(path)
            after = projection.build_display(s2, c, clock())
            self.assertEqual(s2.counts(), {"observation": 27, "ingestion_attempt": 3})
            self.assertEqual(before, after)
            self.assertEqual(after["metrics"][0]["label_zh"], "PM10颗粒")
            s2.close()


if __name__ == "__main__":
    unittest.main()
