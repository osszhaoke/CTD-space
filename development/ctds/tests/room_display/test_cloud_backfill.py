"""Automated tests for AirGradient Cloud API v1 auto-backfill, gap-healing, and multi-range series."""
import datetime as dt
import http.client
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from _helpers import Clock, cfg

from room_display import __main__ as cli_main
from room_display import adapter, cloud_backfill, collector, config, mqtt_adapter, projection, server
from room_display.store import Store


def _parse_basic_utc(s):
    return dt.datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)


class FakeAirGradientCloudServer:
    """Loopback server emulating official AirGradient Public API v1 (`/raw`, `/past`, `/current`)."""

    def __init__(self, *, valid_token="tok-valid-secret-999", location_id="888777"):
        self.valid_token = valid_token
        self.location_id = str(location_id)
        self.records = []  # list of dicts with aware UTC `_dt` and payload dict
        self.requests = []
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                parent.requests.append((parsed.path, qs))
                tok = (qs.get("token") or [""])[0]
                if tok != parent.valid_token:
                    self._json(401, {"message": "Unauthorized"})
                    return
                prefix = f"/public/api/v1/locations/{parent.location_id}/measures/"
                if not parsed.path.startswith(prefix):
                    self._json(404, {"message": "Not found"})
                    return
                kind = parsed.path[len(prefix):]
                if kind == "current":
                    latest = parent.records[-1]["payload"] if parent.records else {"pm02": 12.0, "rco2": 520}
                    self._json(200, latest)
                    return
                if kind not in ("raw", "past"):
                    self._json(404, {"message": "Unknown endpoint"})
                    return
                try:
                    t_from = _parse_basic_utc((qs.get("from") or [""])[0])
                    t_to = _parse_basic_utc((qs.get("to") or [""])[0])
                except ValueError:
                    self._json(422, {"message": "Invalid basic ISO timestamp"})
                    return
                max_span = dt.timedelta(days=2) if kind == "raw" else dt.timedelta(days=10)
                if (t_to - t_from) > max_span or t_to < t_from:
                    self._json(422, {"message": "Interval exceeds API limit"})
                    return
                matched = [r["payload"] for r in parent.records if t_from <= r["dt"] <= t_to]
                if kind == "raw":
                    matched = matched[-200:]
                self._json(200, matched)

            def _json(self, status, obj):
                raw = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def add_record(self, obs_dt, *, pm02=18.5, pm02_corrected=14.2, rco2=610, atmp=23.4, rhum=51.0, tvoc_idx=95):
        iso_z = obs_dt.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.records.append({
            "dt": obs_dt.astimezone(dt.timezone.utc),
            "payload": {
                "locationId": int(self.location_id),
                "locationName": "Private Master Suite",
                "serialno": "a8032af60000",
                "firmwareVersion": "8.7.6",
                "latitude": 31.2304,
                "longitude": 121.4737,
                "pm01": 8.0,
                "pm02": pm02,
                "pm10": 22.0,
                "pm02_corrected": pm02_corrected,
                "atmp": atmp,
                "atmp_corrected": atmp - 0.5,
                "rhum": rhum,
                "rco2": rco2,
                "tvocIndex": tvoc_idx,
                "noxIndex": 1,
                "wifi": -44,
                "timestamp": iso_z,
            },
        })
        self.records.sort(key=lambda r: r["dt"])

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


class CloudBackfillAndSeriesRangeTest(unittest.TestCase):
    def setUp(self):
        self._old_sbx = os.environ.get("CTDS_TEST_SANDBOX")
        os.environ["CTDS_TEST_SANDBOX"] = "1"
        self.cloud_srv = FakeAirGradientCloudServer()

    def tearDown(self):
        self.cloud_srv.close()
        if self._old_sbx is None:
            os.environ.pop("CTDS_TEST_SANDBOX", None)
        else:
            os.environ["CTDS_TEST_SANDBOX"] = self._old_sbx

    def _make_live_mqtt_cloud_cfg(self, *, earliest="2026-10-15T00:00:00Z", token=None):
        base = dict(config.DEFAULTS, source=dict(config.DEFAULTS["source"]), eight_sleep=dict(config.DEFAULTS["eight_sleep"]))
        base["room_id"] = "demo-room-01"
        base["room_name_zh"] = "睡眠房"
        base["db_path"] = ":memory:"
        base["source"].update({
            "source_id": "ag-test-01",
            "mode": "LIVE",
            "protocol": "mqtt",
            "mqtt_mode": "listen",
            "mqtt_listen_host": "127.0.0.1",
            "mqtt_listen_port": 18830,
            "cloud_backfill_enabled": True,
            "cloud_api_base_url": self.cloud_srv.base_url,
            "cloud_location_id": self.cloud_srv.location_id,
            "cloud_api_token": token or self.cloud_srv.valid_token,
            "cloud_earliest_date": earliest,
            "cloud_gap_check_interval_s": 120,
        })
        return config.validate(base)

    def test_full_startup_backfill_resumable_dedup_and_privacy_clean(self):
        now_dt = dt.datetime(2026, 11, 8, 0, 0, 0, tzinfo=dt.timezone.utc)
        clock = Clock(now_dt)
        store = Store(":memory:")
        c = self._make_live_mqtt_cloud_cfg(earliest="2026-10-15T00:00:00Z")
        collector.register(store, c, clock)

        # Seed archive points (20 days ago, 12 days ago) and recent 48h raw points (5h ago, 10m ago)
        t_archive_1 = now_dt - dt.timedelta(days=20)
        t_archive_2 = now_dt - dt.timedelta(days=12)
        t_recent_5h = now_dt - dt.timedelta(hours=5)
        t_twin_local = now_dt - dt.timedelta(minutes=10)
        self.cloud_srv.add_record(t_archive_1, pm02=42.0, pm02_corrected=31.5, rco2=780)
        self.cloud_srv.add_record(t_archive_2, pm02=28.0, pm02_corrected=21.0, rco2=640)
        self.cloud_srv.add_record(t_recent_5h, pm02=19.0, pm02_corrected=14.8, rco2=560)
        self.cloud_srv.add_record(t_twin_local, pm02=99.0, pm02_corrected=88.0, rco2=999)

        # Record an authoritative local MQTT observation 12s after `t_twin_local` (within ±45s twin window)
        mqtt_rx_dt = t_twin_local + dt.timedelta(seconds=12)
        mqtt_payload = json.dumps({
            "pm01": 6, "pm02": 15.0, "pm10": 18, "pm02Compensated": 12.5,
            "rco2": 510, "atmp": 22.5, "rhum": 50.0, "tvocIndex": 90, "noxIndex": 1, "wifi": -45,
        }).encode("utf-8")
        mqtt_adapter.ingest_mqtt_message(
            store, c, topic="airgradient/readings/a8032af60000", payload=mqtt_payload,
            deduplicator=mqtt_adapter.MqttDeduplicator(window_s=15), provenance="LIVE",
            clock=lambda: mqtt_rx_dt,
        )

        # Step 1: Run partial backfill (`max_past_windows=1`), verify checkpointing before completion
        r1 = cloud_backfill.sync_once(store, c, clock=clock, inter_window_pause_s=0, max_past_windows=1)
        self.assertFalse(r1["full_backfill_done"])
        self.assertEqual(r1["state"], "SYNCING")
        self.assertIsNotNone(r1["oldest_scanned_at"])

        # Step 2: Resume until full archive down to `earliest` is done
        r2 = cloud_backfill.sync_once(store, c, clock=clock, inter_window_pause_s=0)
        self.assertTrue(r2["full_backfill_done"])
        self.assertEqual(r2["state"], "READY")

        # Local MQTT twin at `t_twin_local` must NOT be overwritten or duplicated by cloud's 99.0!
        all_pm25 = store.series(c["room_id"], "pm2_5", "2026-10-01T00:00:00Z", limit=None, provenance="LIVE")
        self.assertEqual([p["value"] for p in all_pm25], [42.0, 28.0, 19.0, 15.0])

        # Compensated PM2.5 (`pm02_corrected`) stays strictly separated from raw `pm2_5`
        all_comp = store.series(c["room_id"], "pm2_5_compensated", "2026-10-01T00:00:00Z", limit=None, provenance="LIVE")
        self.assertEqual([p["value"] for p in all_comp], [31.5, 21.0, 14.8, 12.5])

        # Verify `PROVIDER_OBSERVED` vs `RECEIPT_ONLY` time basis in SQLite
        rows = store._q("SELECT provider_observed_at, gateway_received_at, time_basis FROM observation WHERE metric_id='pm2_5' ORDER BY rowid ASC")
        time_bases = {r["time_basis"] for r in rows}
        self.assertEqual(time_bases, {"RECEIPT_ONLY", "PROVIDER_OBSERVED"})
        cloud_rows = [r for r in rows if r["time_basis"] == "PROVIDER_OBSERVED"]
        self.assertEqual(len(cloud_rows), 3)
        for cr in cloud_rows:
            self.assertIsNotNone(cr["provider_observed_at"])
            self.assertNotEqual(cr["provider_observed_at"], cr["gateway_received_at"])

        # Strict privacy audit: zero token, locationId, locationName, GPS, or serialno in SQLite or display JSON
        db_dump = "\n".join(
            json.dumps([dict(r) for r in store._q(f"SELECT * FROM {tbl}")], ensure_ascii=False)
            for tbl in ("observation", "ingestion_attempt", "meta")
        )
        disp_dump = json.dumps(projection.build_display(store, c, now=now_dt), ensure_ascii=False)
        for secret in (
            self.cloud_srv.valid_token,
            self.cloud_srv.location_id,
            "Private Master Suite",
            "a8032af60000",
            "31.2304",
            "121.4737",
        ):
            self.assertNotIn(secret, db_dump)
            self.assertNotIn(secret, disp_dump)
        store.close()

    def test_gap_auto_heal_after_mac_sleep_and_mqtt_recovery_trigger(self):
        t0 = dt.datetime(2026, 11, 8, 0, 0, 0, tzinfo=dt.timezone.utc)
        clock = Clock(t0)
        store = Store(":memory:")
        c = self._make_live_mqtt_cloud_cfg(earliest="2026-11-07T00:00:00Z")
        collector.register(store, c, clock)

        self.cloud_srv.add_record(t0 - dt.timedelta(hours=2), pm02=16.0, rco2=540)
        res0 = cloud_backfill.sync_once(store, c, clock=clock, inter_window_pause_s=0)
        self.assertTrue(res0["full_backfill_done"])

        # Simulate Mac sleeping for 6 hours while 3 hourly cloud measurements occurred
        for h in (5, 3, 1):
            self.cloud_srv.add_record(t0 + dt.timedelta(hours=6 - h), pm02=20.0 + h, rco2=600 + h * 10)
        clock.advance(6 * 3600)

        # Verify Collector._on_mqtt_outcome wakes CloudBackfillManager when gap >= 270s
        col = collector.Collector(store, c, clock=clock, sleeper=lambda _: None)
        col._last_mqtt_ok_ts = t0.timestamp()
        with mock.patch.object(col.cloud_backfill, "trigger_gap_check") as trig_mock:
            col._on_mqtt_outcome(mqtt_adapter.MQTT_OK, clock())
            trig_mock.assert_called_once()

        # Run incremental gap heal and confirm only the missed window is scanned and all 3 points arrive
        before_reqs = len(self.cloud_srv.requests)
        res_heal = cloud_backfill.sync_once(store, c, clock=clock, inter_window_pause_s=0)
        self.assertGreater(res_heal["observations_written"], 0)
        self.assertLessEqual(len(self.cloud_srv.requests) - before_reqs, 3)

        pts = store.series(c["room_id"], "pm2_5", "2026-11-07T00:00:00Z", limit=None, provenance="LIVE")
        self.assertEqual([p["value"] for p in pts], [16.0, 25.0, 23.0, 21.0])
        store.close()

    def test_cloud_auth_401_never_degrades_local_mqtt_connectivity(self):
        t0 = dt.datetime(2026, 11, 8, 1, 0, 0, tzinfo=dt.timezone.utc)
        clock = Clock(t0)
        store = Store(":memory:")
        c = self._make_live_mqtt_cloud_cfg(token="disabled-or-wrong-token")
        collector.register(store, c, clock)

        # Local MQTT is actively reporting live readings
        mqtt_payload = json.dumps({"pm02": 21.3, "rco2": 530, "atmp": 21.5, "rhum": 65.0, "tvocIndex": 88}).encode("utf-8")
        mqtt_adapter.ingest_mqtt_message(
            store, c, topic="airgradient/readings/a8032af60000", payload=mqtt_payload,
            deduplicator=mqtt_adapter.MqttDeduplicator(window_s=15), provenance="LIVE", clock=clock,
        )

        mgr = cloud_backfill.CloudBackfillManager(store, c, clock=clock, inter_window_pause_s=0)
        store._cloud_backfill_mgr = mgr
        mgr.start()
        try:
            deadline = dt.datetime.now() + dt.timedelta(seconds=3)
            while mgr.status_snapshot()["state"] != "AUTH_REQUIRED" and dt.datetime.now() < deadline:
                threading.Event().wait(0.03)
            snap = mgr.status_snapshot()
            self.assertEqual(snap["state"], "AUTH_REQUIRED")
            self.assertEqual(snap["last_error_code"], "CLOUD_AUTH_REJECTED")

            # Local sensor connectivity on big screen MUST remain CONNECTED with zero error banner!
            disp = projection.build_display(store, c, now=t0)
            self.assertEqual(disp["actual_source_connectivity"], "CONNECTED")
            self.assertIsNone(disp["error_summary_zh"])
            self.assertEqual(disp["cloud_backfill"]["state"], "AUTH_REQUIRED")

            # Operator enables API Access in AirGradient dashboard -> automatic recovery on next wake
            self.cloud_srv.add_record(t0 - dt.timedelta(hours=4), pm02=33.0, rco2=710)
            c["source"]["cloud_api_token"] = self.cloud_srv.valid_token
            mgr.trigger_gap_check()
            deadline = dt.datetime.now() + dt.timedelta(seconds=3)
            while mgr.status_snapshot()["state"] != "READY" and dt.datetime.now() < deadline:
                threading.Event().wait(0.03)
            self.assertEqual(mgr.status_snapshot()["state"], "READY")
            self.assertGreater(mgr.status_snapshot()["total_backfilled"], 0)
        finally:
            mgr.stop()
            store.close()

    def test_multi_range_series_endpoint_minmax_downsampling_and_js_helpers(self):
        now_dt = dt.datetime(2026, 11, 8, 2, 0, 0, tzinfo=dt.timezone.utc)
        store = Store(":memory:")
        c = self._make_live_mqtt_cloud_cfg()
        collector.register(store, c, lambda: now_dt)

        # Insert 900 hourly observations (~37.5 days) plus an extreme spike & trough to verify peak preservation
        obs_rows = []
        for i in range(900):
            ts = now_dt - dt.timedelta(hours=899 - i)
            val = 20.0 + (i % 12)
            if i == 120:
                val = 2.5   # global minimum (32.5 days ago -> only in `all`)
            elif i == 500:
                val = 188.4 # global maximum (16.6 days ago -> in `30d` and `all`)
            rows, _ = adapter.normalize(
                {"pm02": val},
                source_id=c["source"]["source_id"],
                room_id=c["room_id"],
                attempt_id=f"att-{i}",
                received_at=now_dt,
                provenance="LIVE",
                mapping_version=cloud_backfill.catalog.CLOUD_MAPPING_VERSION,
                event_identity=f"cloud:test:{adapter.iso(ts)}",
                provider_observed_at=ts,
            )
            obs_rows.extend(rows)
        store.record_attempt({
            "attempt_id": "bulk-seed",
            "source_id": c["source"]["source_id"],
            "request_started_at": adapter.iso(now_dt),
            "response_received_at": adapter.iso(now_dt),
            "outcome": "CLOUD_BACKFILL_OK",
            "http_status": 200,
            "payload_sha256": "00" * 32,
            "data_provenance": "LIVE",
        }, obs_rows)

        # Direct projection check with fixed `now=now_dt`
        s_all = projection.build_series(store, c, "pm2_5", now=now_dt, range_key="all")
        self.assertEqual(s_all["total_count"], 900)
        self.assertLessEqual(len(s_all["points"]), projection.MODAL_MAX_POINTS)
        self.assertEqual(s_all["min_value"], 2.5)
        self.assertEqual(s_all["max_value"], 188.4)
        downsampled_vals = [pt[1] for pt in s_all["points"]]
        self.assertIn(2.5, downsampled_vals, "Peak-preserving downsampler must retain exact global minimum")
        self.assertIn(188.4, downsampled_vals, "Peak-preserving downsampler must retain exact global maximum")

        s_30d = projection.build_series(store, c, "pm2_5", now=now_dt, range_key="30d")
        self.assertEqual(s_30d["window_h"], 720)
        self.assertEqual(s_30d["max_value"], 188.4)
        self.assertGreater(s_30d["min_value"], 2.5)  # 2.5 was 32.5 days ago

        # HTTP endpoint validation
        srv = server.make_server(store, c, port=0)
        port = srv.server_address[1]
        server.serve_in_thread(srv)
        try:
            def get_json(path):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
                resp = conn.getresponse()
                data = json.loads(resp.read())
                conn.close()
                return resp.status, data

            room = c["room_id"]
            for rk in ("24h", "7d", "30d", "all"):
                st, doc = get_json(f"/api/v1/rooms/{room}/series?metric=pm2_5&range={rk}")
                self.assertEqual(st, 200, rk)
                self.assertEqual(doc["range"], rk)
            self.assertEqual(get_json(f"/api/v1/rooms/{room}/series?metric=pm2_5&range=99y")[0], 400)
            self.assertEqual(get_json(f"/api/v1/rooms/{room}/series?metric=pm2_5&hours=25")[0], 400)
        finally:
            srv.shutdown()
            srv.server_close()
            store.close()

        if shutil.which("node"):
            js_path = Path(__file__).resolve().parents[2] / "room_display" / "static" / "preview.js"
            proc = subprocess.run(
                [
                    "node", "-e",
                    f"""
                    const assert = require('assert');
                    const core = require({json.dumps(str(js_path))});
                    assert.deepStrictEqual(Object.keys(core.MODAL_RANGES), ['24h', '7d', '30d', 'all']);
                    const t = '2024-05-06T08:30:00Z';
                    assert.match(core.formatAxisTime(t, 24 * 3600 * 1000), /^\\d{{2}}:\\d{{2}}$/);
                    assert.match(core.formatAxisTime(t, 7 * 86400 * 1000), /^05-06 \\d{{2}}:\\d{{2}}$/);
                    assert.strictEqual(core.formatAxisTime(t, 30 * 86400 * 1000), '05-06');
                    assert.strictEqual(core.formatAxisTime(t, 400 * 86400 * 1000), '2024-05-06');
                    // Verify inferGapMs does NOT split bucketed min-max pairs into disconnected solo dots
                    const pts = {json.dumps(s_all["points"])};
                    const gap = core.inferGapMs(pts, 60);
                    const endMs = Date.parse({json.dumps(adapter.iso(now_dt))});
                    const startMs = Date.parse(pts[0][0]);
                    const g = core.computeChartGeometry(pts, {{width: 860, height: 260, endMs, windowMs: endMs - startMs, gapMs: gap}});
                    assert.strictEqual(g.segments.length, 1, 'continuous min-max downsampled archive must form 1 connected curve');
                    """,
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_bind_and_sync_cloud_history_redacts_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            db_path = Path(tmp) / "c01.sqlite3"
            config.init_config(cfg_path)
            doc = json.loads(cfg_path.read_text(encoding="utf-8"))
            doc["db_path"] = str(db_path)
            doc["source"]["mode"] = "LIVE"
            doc["source"]["protocol"] = "mqtt"
            doc["source"]["mqtt_mode"] = "listen"
            doc["source"]["mqtt_listen_host"] = "127.0.0.1"
            doc["source"]["mqtt_listen_port"] = 18831
            cfg_path.write_text(json.dumps(doc), encoding="utf-8")

            t_obs = adapter.utc_now() - dt.timedelta(hours=1)
            self.cloud_srv.add_record(t_obs, pm02=17.4, rco2=545)

            out_buf = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(self.cloud_srv.valid_token + "\n")), \
                 mock.patch("sys.stdout", out_buf):
                rc = cli_main.main([
                    "--config", str(cfg_path),
                    "--location-id", self.cloud_srv.location_id,
                    "--token-stdin",
                    "--earliest-date", adapter.iso(adapter.utc_now() - dt.timedelta(days=2)),
                    "--cloud-base-url", self.cloud_srv.base_url,
                    "bind-airgradient-cloud",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(cfg_path.stat().st_mode & 0o777, 0o600)
            bind_out = out_buf.getvalue()
            self.assertNotIn(self.cloud_srv.valid_token, bind_out)
            self.assertNotIn(self.cloud_srv.location_id, bind_out)

            sync_buf = io.StringIO()
            with mock.patch("sys.stdout", sync_buf):
                rc2 = cli_main.main(["--config", str(cfg_path), "sync-cloud-history"])
            self.assertEqual(rc2, 0)
            sync_doc = json.loads(sync_buf.getvalue())
            self.assertTrue(sync_doc["full_backfill_done"])
            self.assertGreater(sync_doc["observations_written"], 0)
            self.assertNotIn(self.cloud_srv.valid_token, sync_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
