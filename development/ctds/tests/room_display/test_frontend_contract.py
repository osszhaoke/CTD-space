"""Front-end contract and history API tests (2026-10-23 U4/U5/U7 remediation).

Guards against the regressions found by the independent audit:
- JS referencing DOM ids / CSS classes that do not exist (modal never showed, popover empty);
- hard-coded readings and Eight Sleep cards driven by AirGradient's source_mode;
- dual-zone / bed-angle cards coming back;
- card trend window (1 h) vs modal / timeline window (24 h);
- chart geometry: time-proportional x, gaps break the line, points outside the window dropped.
"""
import datetime as dt
import http.client
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from _helpers import Clock, cfg

from room_display import collector, config, eight_sleep, projection, server
from room_display.__main__ import _describe_bind_error
from room_display.adapter import AdapterError
from room_display.store import Store

STATIC = Path(__file__).resolve().parents[2] / "room_display" / "static"
HTML = (STATIC / "preview.html").read_text(encoding="utf-8")
JS = (STATIC / "preview.js").read_text(encoding="utf-8")
CSS = (STATIC / "preview.css").read_text(encoding="utf-8")
HTML_IDS = set(re.findall(r'\bid="([^"]+)"', HTML))


def _registry_keys(name):
    block = re.search(rf"const {name} = \[(.*?)\];", JS, re.S).group(1)
    return [(k, mk, mid, chart == "true") for k, mk, mid, chart in
            re.findall(r'key: "([a-z0-9-]+)", metaKey: "([a-z0-9_]+)", metricId: "([a-z0-9_]+)", chart: (true|false)', block)]


AG_CARDS = _registry_keys("AG_CARDS")
ES_CARDS = _registry_keys("ES_CARDS")


class FrontendContractTest(unittest.TestCase):
    def test_registry_matches_backend_metrics(self):
        self.assertEqual([c[2] for c in AG_CARDS], ["co2", "pm2_5", "temperature", "humidity", "tvoc_index"])
        # Right module mirrors the left one diagonally: 2 + 3 cards, all backed by real Eight Sleep metrics
        self.assertEqual([c[0] for c in ES_CARDS], ["es-left", "es-right", "es-state", "es-night", "es-elev"])
        self.assertTrue({c[2] for c in ES_CARDS} <= set(eight_sleep.EIGHT_SLEEP_METRICS))
        for personal in ("pod_heart_rate", "pod_hrv", "pod_respiratory_rate"):
            self.assertNotIn(personal, JS)

    def test_every_js_dom_reference_exists_in_html(self):
        literal = set(re.findall(r'\$\("([^"]+)"\)', JS)) | set(re.findall(r'setText\("([^"]+)"', JS))
        for args in re.findall(r'renderChart\("([^"]+)", "([^"]+)", "([^"]+)"', JS):
            literal |= set(args)
        missing = sorted(i for i in literal if i not in HTML_IDS)
        self.assertEqual(missing, [], f"JS references ids missing from preview.html: {missing}")
        for key, _, _, chart in AG_CARDS + ES_CARDS:
            for prefix in ("card-", "val-", "tag-") + (("svg-", "trend-box-", "tip-") if chart else ()):
                self.assertIn(prefix + key, HTML_IDS)
        for key, _, _, _ in ES_CARDS:
            self.assertIn("badge-" + key, HTML_IDS)
        for extra in ("sub-es-left", "sub-es-right", "detail-es-state-left", "detail-es-state-right",
                      "detail-es-elev-left", "detail-es-elev-right"):
            self.assertIn(extra, HTML_IDS)

    def test_every_card_and_help_key_has_metadata(self):
        meta_block = re.search(r"const METRICS_META = \{(.*?)\n  \};", JS, re.S).group(1)
        meta_keys = set(re.findall(r"^    ([a-z0-9_]+): \{", meta_block, re.M))
        for attr in ("data-metric", "data-help"):
            for key in re.findall(rf'{attr}="([^"]+)"', HTML):
                self.assertIn(key, meta_keys, f"{attr}={key} has no METRICS_META entry")
        self.assertEqual({c[1] for c in AG_CARDS + ES_CARDS}, set(re.findall(r'data-metric="([^"]+)"', HTML)))

    def test_css_defines_classes_used_by_js(self):
        self.assertIn(".modal-backdrop.open", CSS)
        self.assertIn('MODAL_OPEN_CLASS = "open"', JS)
        for cls in ("trend-line", "trend-area", "trend-clover", "trend-dot-solo", "card-detail-rows", "detail-row", "trend-hover-dot", "trend-guide",
                    "trend-empty-line", "trend-empty-text", "trend-axis-text", "hour-node", "hour-dot", "hour-label",
                    "crosshair-hidden"):
            self.assertIn(cls, JS + HTML)
            self.assertRegex(CSS, rf"\.{re.escape(cls)}\b")

    def test_no_hardcoded_readings_or_fake_series(self):
        self.assertIsNone(re.search(r"render(?:Smooth)?Chart\([^;]*\[\s*-?\d", JS), "literal data array passed to a chart")
        self.assertIsNone(re.search(r'textContent\s*=\s*"[^"]*\d', JS), "literal number written to the screen")
        for fake in ("21.8", "21.5, 21.6", "实际 0档", "实际 +1档", "0° /", "微仰", "贴身舒适", ': "27"', "'27'"):
            self.assertNotIn(fake, JS)
        self.assertNotIn("Math.sin", JS)
        self.assertNotIn("Math.random", JS)

    def test_eight_sleep_rendering_never_reads_airgradient_mode(self):
        body = re.search(r"function getEsBadgeInfo\(es\) \{(.*?)\n  \}", JS, re.S).group(1)
        self.assertNotIn("source_mode", body)
        es_block = re.search(r"function renderEsCards\(esMap, ctx\) \{(.*?)\n  \}\n", JS, re.S).group(1)
        self.assertNotIn("source_mode", es_block)

    def test_timeline_nodes_are_clean_clovers(self):
        # Timeline nodes are the 4SEAS clover; no leftover round-dot border may draw a box around it
        self.assertIn('dot.innerHTML = `<svg viewBox="0 0 100 100" aria-hidden="true"><path d="${CLOVER_PATH}"/></svg>`', JS)
        for block in re.findall(r"\.hour-node\.empty \.hour-dot\s*\{([^}]*)\}", CSS):
            self.assertNotIn("dashed", block)
        self.assertNotRegex(CSS, r"\.hour-dot\s*\{[^}]*border-style:\s*dashed")
        # Owner request 2026-10-24: all timeline nodes look the same; "no reading" is only stated in the tooltip
        self.assertNotIn(".hour-node.empty", CSS)

    def test_window_labels(self):
        self.assertIn('content: "近 1 小时"', CSS)
        self.assertIn("CARD_WINDOW_MS = 60 * 60 * 1000", JS)
        self.assertIn("HISTORY_HOURS = 24", JS)


@unittest.skipUnless(shutil.which("node"), "Node.js not installed: behavioural JS checks skipped")
class FrontendBehaviourTest(unittest.TestCase):
    def run_node(self, script):
        js_path = json.dumps(str(STATIC / "preview.js"))
        proc = subprocess.run([shutil.which("node"), "-e", f"const assert=require('assert');const core=require({js_path});\n{script}\nconsole.log('OK');"],
                              capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK", proc.stdout)

    def test_eight_sleep_badges_and_card_badges(self):
        self.run_node("""
const b = core.getEsBadgeInfo;
assert.strictEqual(b({mode:'READY', status:'READY_NOT_CONNECTED'}).text, 'Pod 6 · 待接入');
assert.ok(!b({mode:'SYNTHETIC', status:'CONNECTED'}).text.includes('在线'));
assert.strictEqual(b({mode:'LIVE', status:'CONNECTED'}).text, 'Pod 6 · 实时在线');
assert.strictEqual(b({mode:'LIVE', status:'DISCONNECTED'}).text, 'Pod 6 · 离线');
assert.strictEqual(b({mode:'LIVE', status:'AUTH_PAUSED'}).text, '床垫认证失效 · 需重新绑定');
assert.strictEqual(core.getRoomStatusText({source_mode:'SYNTHETIC', actual_source_connectivity:'CONNECTED'}), '合成测试数据');
assert.strictEqual(core.sideState(22, 27, 1), '升温中');
assert.strictEqual(core.sideState(27, 22, 1), '降温中');
assert.strictEqual(core.sideState(25, 25.3, 1), '恒温维持');
assert.strictEqual(core.sideState(22, 27, 0), '待机');
assert.strictEqual(core.sideState(null, 27, 1), '—');
assert.strictEqual(core.sideState(22, 27, null), '—');
assert.strictEqual(core.levelBadge({status:'CURRENT', value:25, raw_level:-17}), '等级 -17');
assert.strictEqual(core.levelBadge({status:'STALE', value:25, raw_level:-17}), '等级 -17');
assert.strictEqual(core.levelBadge({status:'STALE', value:25, raw_level:-17, age_s:1801}), '—');
assert.strictEqual(core.levelBadge({status:'STALE', value:25, raw_level:-17, age_s:1799}), '等级 -17');
const s = core.sideSummary({pod_left_bed_temp:{status:'CURRENT', value:22}, pod_left_target_temp:{status:'CURRENT', value:27},
                            pod_left_heating:{status:'CURRENT', value:1}}, 'left');
assert.deepStrictEqual([s.cur, s.tgt, s.heating, s.state], [22, 27, 1, '升温中']);
const empty = core.sideSummary({}, 'right');
assert.deepStrictEqual([empty.cur, empty.state], [null, '—']);
assert.strictEqual(core.elevText(12, 4), '背 12° · 腿 4°');
assert.strictEqual(core.elevText(null, null), '—');
""")

    def test_chart_geometry_time_axis_gaps_and_window(self):
        self.run_node("""
const end = Date.parse('2026-10-23T06:00:00Z'), H = 3600000;
const pts = [];
for (let m = 59; m >= 0; m--) { if (m >= 15 && m < 30) continue; pts.push([new Date(end - m*60000).toISOString(), 20 + m/10]); }
pts.unshift([new Date(end - 2*H).toISOString(), 99]);           // outside the 1 h window -> dropped
const gap = core.inferGapMs(pts, 60);
assert.ok(gap >= 180000);
const g = core.computeChartGeometry(pts, {width: 308, height: 80, padX: 4, endMs: end, windowMs: H, gapMs: gap});
assert.strictEqual(g.segments.length, 2, 'a 15 min hole must split the line');
assert.ok(!g.all.some(p => p.v === 99), 'point outside window dropped');
const first = g.all[0], last = g.all[g.all.length-1];
assert.ok(Math.abs(first.x - (4 + 300/60)) < 0.01, 'x is proportional to time (59 min ago)');
assert.ok(Math.abs(last.x - 304) < 0.01, 'latest point at right edge');
const flat = core.computeChartGeometry([[new Date(end).toISOString(), 5]], {width:100,height:50,endMs:end,windowMs:H});
assert.strictEqual(flat.segments.length, 1);
assert.ok(core.computeChartGeometry([], {endMs:end, windowMs:H}).empty);
const path = core.monotonePath([{x:0,y:10},{x:10,y:0},{x:20,y:0},{x:30,y:10}]);
assert.ok(path.startsWith('M 0.0,10.0 C'));

// Symmetrical scale alignment test: minY/maxY overrides produce equal Y coords for equal readings
const leftSample = [[new Date(end - 30*60000).toISOString(), 20.0], [new Date(end).toISOString(), 21.0]];
const rightSample = [[new Date(end - 30*60000).toISOString(), 20.0], [new Date(end - 10*60000).toISOString(), 22.0], [new Date(end).toISOString(), 21.0]];
const gLeft = core.computeChartGeometry(leftSample, {width: 380, height: 70, endMs: end, windowMs: H, minY: 19.5, maxY: 22.5});
const gRight = core.computeChartGeometry(rightSample, {width: 380, height: 70, endMs: end, windowMs: H, minY: 19.5, maxY: 22.5});
assert.strictEqual(gLeft.all[1].y.toFixed(2), gRight.all[2].y.toFixed(2), '21.0 C on left and right must share exact Y coordinate');
""")

    def test_display_formatting_respects_status(self):
        self.run_node("""
const f = core.formatMetricDisplay;
assert.strictEqual(f({status:'NO_OBSERVATION', value:null}, false, null).valStr, '—');
assert.strictEqual(f({status:'STALE', value:12}, false, null).valStr, '12.0');
assert.strictEqual(f({status:'STALE', value:12, age_s: 1801}, false, null).valStr, '—');
assert.strictEqual(f({status:'STALE', value:12, age_s: 1799}, false, null).valStr, '12.0');
assert.strictEqual(f({status:'EXPLICIT_NULL', value:12}, false, null).valStr, '—');
assert.strictEqual(f({status:'CURRENT', value:12.34}, false, null).valStr, '12.3');
for (const id of ['val-es-left', 'sub-es-right', 'badge-es-state', 'detail-es-elev-right']) assert.ok(core.OFFLINE_RESET_ELEMENTS.includes(id), id);
assert.ok(core.OFFLINE_RESET_TAGS.includes('tag-es-elev'));
""")


class HistoryApiTest(unittest.TestCase):
    """1 h card trends, 24 h series and the 25-node timeline, all from real stored observations."""

    def setUp(self):
        self.clock, self.store, self.cfg = Clock(), Store(":memory:"), cfg(retries=0)
        collector.register(self.store, self.cfg, self.clock)
        col = collector.Collector(self.store, self.cfg, clock=self.clock, sleeper=lambda _: None)
        for minute in range(180):          # 3 h at 60 s with a 40-minute hole (minutes 60..99)
            if not 60 <= minute < 100:
                col.round()
            self.clock.advance(60)
        self.now = self.clock()

    def tearDown(self):
        self.store.close()

    def test_card_trend_is_last_hour_only(self):
        d = projection.build_display(self.store, self.cfg, now=self.now)
        self.assertEqual(d["trend_window_s"], 3600)
        co2 = next(m for m in d["metrics"] if m["metric_id"] == "co2")
        cutoff = self.now - dt.timedelta(hours=1)
        self.assertTrue(co2["trend"])
        for t, _ in co2["trend"]:
            self.assertGreaterEqual(dt.datetime.fromisoformat(t.replace("Z", "+00:00")), cutoff)

    def test_series_is_24h_downsampled_and_keeps_latest(self):
        s = projection.build_series(self.store, self.cfg, "co2", 24, now=self.now)
        self.assertEqual(s["provenance"], "SYNTHETIC")
        self.assertEqual(len(s["points"]), 140)           # 180 minutes minus the 40-minute hole
        self.assertLessEqual(len(s["points"]), projection.HISTORY_POINTS)
        latest = self.store.latest_observation(self.cfg["room_id"], "co2", observed_only=True)
        self.assertEqual(s["points"][-1][0], latest["gateway_received_at"])

    def test_eight_sleep_series_empty_when_not_connected(self):
        s = projection.build_series(self.store, self.cfg, "pod_heart_rate", 24, now=self.now)
        self.assertIsNone(s["provenance"])
        self.assertEqual(s["points"], [])

    def test_eight_sleep_synthetic_series_is_labelled(self):
        es_cfg = config.validate(dict(self.cfg, eight_sleep={"mode": "SYNTHETIC"}))
        for _ in range(3):
            eight_sleep.ingest_once(self.store, es_cfg, clock=self.clock)
            self.clock.advance(60)
        s = projection.build_series(self.store, es_cfg, "pod_heart_rate", 24, now=self.clock())
        self.assertEqual(s["provenance"], "SYNTHETIC")
        self.assertEqual(len(s["points"]), 3)

    def test_timeline_nodes_use_nearest_real_point_only(self):
        tl = projection.build_timeline(self.store, self.cfg, hours=24, now=self.now)
        nodes = tl["nodes"]
        self.assertEqual(len(nodes), 25)
        self.assertTrue(nodes[-1]["is_now"])
        self.assertFalse(any(n["is_now"] for n in nodes[:-1]))
        tolerance = dt.timedelta(minutes=30)
        for n in nodes:
            t = dt.datetime.fromisoformat(n["t"].replace("Z", "+00:00"))
            v = n["values"]["co2"]
            if v is not None:
                obs = dt.datetime.fromisoformat(v["observed_at"].replace("Z", "+00:00"))
                self.assertLessEqual(abs(obs - t), tolerance)
            for mid in eight_sleep.EIGHT_SLEEP_METRICS:
                self.assertIsNone(n["values"][mid])     # READY: never any Eight Sleep value
        # hours before the first observation have no value (no back-fill)
        self.assertIsNone(nodes[0]["values"]["co2"])
        self.assertIsNotNone(nodes[-1]["values"]["co2"])


class HistoryEndpointTest(unittest.TestCase):
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
        body = r.read()
        c.close()
        return r.status, json.loads(body)

    def test_series_and_timeline_validation(self):
        room = self.cfg["room_id"]
        st, doc = self.get(f"/api/v1/rooms/{room}/series?metric=co2&hours=24")
        self.assertEqual(st, 200)
        self.assertEqual(doc["window_h"], 24)
        for bad in ("hours=0", "hours=25", "hours=abc", "hours=-1", "hours=1e1"):
            self.assertEqual(self.get(f"/api/v1/rooms/{room}/series?metric=co2&{bad}")[0], 400, bad)
        for metric in ("evil", "co2%27", "", "store"):
            self.assertEqual(self.get(f"/api/v1/rooms/{room}/series?metric={metric}&hours=1")[0], 400, metric)
        st, doc = self.get(f"/api/v1/rooms/{room}/timeline?hours=24")
        self.assertEqual(st, 200)
        self.assertEqual(len(doc["nodes"]), 25)
        self.assertEqual(self.get(f"/api/v1/rooms/{room}/timeline?hours=99")[0], 400)
        self.assertEqual(self.get("/api/v1/rooms/other-room/timeline")[0], 404)


class EightSleepRealShapeTest(unittest.TestCase):
    """Shapes observed against the real service on 2026-10-23 (status codes / key names only)."""

    def setUp(self):
        import os
        from test_ui import FakeEightSleepServer
        self._old = os.environ.get("CTDS_TEST_SANDBOX")
        os.environ["CTDS_TEST_SANDBOX"] = "1"
        self.srv = FakeEightSleepServer()

    def tearDown(self):
        import os
        self.srv.close()
        if self._old is None:
            os.environ.pop("CTDS_TEST_SANDBOX", None)
        else:
            os.environ["CTDS_TEST_SANDBOX"] = self._old

    def client(self, **kw):
        return eight_sleep.EightSleepClient(email="user@example.com", password="valid-password",
                                            base_url=self.srv.base_url, auth_url=self.srv.auth_url, **kw)

    def test_fetch_uses_real_endpoints_and_reads_only_own_side(self):
        res = self.client().fetch_bundle()
        self.assertEqual(res["outcome"], "OK")
        gets = [h[1] for h in self.srv.hits if h[0] == "GET"]
        trends = next(p for p in gets if "/trends" in p)
        for param in ("tz=", "from=", "to="):
            self.assertIn(param, trends)            # without them the real API answers HTTP 400
        self.assertTrue(any(p.startswith("/v1/app/users/") and p.endswith("/temperature") for p in gets),
                        "currentDeviceLevel only exists on app-api")
        self.assertTrue(any(p.startswith("/v1/devices/") and "filter=" in p for p in gets))
        self.assertFalse(any(h[0] in ("PUT", "PATCH", "DELETE") for h in self.srv.hits))
        ext = eight_sleep.extract_six_metrics(res["trends_doc"], res["temp_doc"], res["device_doc"],
                                              side="solo", user_id=res["user_id"])
        m = ext["metrics"]
        self.assertEqual(m["pod_bed_temp"]["raw_level"], -17)
        self.assertEqual(m["pod_target_bed_temp"]["raw_level"], -17)   # own (left) side, not the other side's 60
        self.assertEqual(m["pod_target_bed_temp"]["value"], 25.0)
        for k in ("left", "right"):
            self.assertFalse(any(k in key.lower() for key in m["pod_target_bed_temp"]))

    def test_other_side_is_used_when_user_sleeps_on_the_right(self):
        dev = {"result": {"leftUserId": "a", "rightUserId": "me", "leftTargetHeatingLevel": -17,
                          "rightTargetHeatingLevel": 0, "leftHeatingLevel": -17, "rightHeatingLevel": 0}}
        ext = eight_sleep.extract_six_metrics({"days": []}, {}, dev, side="solo", user_id="me")
        self.assertEqual(ext["metrics"]["pod_target_bed_temp"]["raw_level"], 0)
        self.assertEqual(ext["metrics"]["pod_bed_temp"]["raw_level"], 0)

    def test_failed_endpoint_records_real_http_status(self):
        res = self.client(device_id="missing-device", user_id="mock-user-001").fetch_bundle()
        self.assertEqual(res["outcome"], "HTTP_ERROR")
        self.assertEqual(res["http_status"], 404)       # no longer flattened to 500

    def test_missing_trend_params_are_rejected_like_the_real_api(self):
        cl = self.client()
        cl.authenticate()
        cl.discover()
        with self.assertRaises(AdapterError) as ctx:
            cl._get(f"{self.srv.base_url}/users/{cl.user_id}/trends", label="trends")
        self.assertEqual(getattr(ctx.exception, "http_status", None), 400)
        self.assertEqual(getattr(ctx.exception, "endpoint", None), "trends")

    def test_invalid_payload_raises_adapter_error(self):
        import http.server
        import threading
        class NonJsonHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("content-type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>Service Temporarily Unavailable</html>")
            def log_message(self, *a): pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), NonJsonHandler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            cl = self.client()
            with self.assertRaises(AdapterError) as ctx:
                cl._get(f"http://127.0.0.1:{srv.server_address[1]}/test", label="test")
            self.assertEqual(ctx.exception.code, "INVALID_PAYLOAD")
            self.assertEqual(getattr(ctx.exception, "endpoint", None), "test")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_eight_sleep_stale_after_defaults_to_360_and_is_configurable(self):
        from room_display import config, store
        st = store.Store(":memory:")
        try:
            cfg = dict(config.DEFAULTS, eight_sleep=dict(config.DEFAULTS["eight_sleep"], mode="LIVE"))
            view = eight_sleep.build_view(st, cfg)
            self.assertEqual(view["metrics"][0]["status"], "NO_DATA")

            cfg["eight_sleep"]["stale_after_s"] = 400
            validated = config.validate(cfg)
            self.assertEqual(validated["eight_sleep"]["stale_after_s"], 400)
        finally:
            st.close()

    def test_effective_stale_window_is_360s_not_the_airgradient_180s(self):
        from room_display import store
        st = store.Store(":memory:")
        try:
            clock = Clock()
            es_cfg = config.validate(cfg(eight_sleep={"mode": "SYNTHETIC"}))
            self.assertEqual(es_cfg["stale_after_s"], 180)          # AirGradient window stays 180
            eight_sleep.ingest_once(st, es_cfg, clock=clock)
            t0 = clock()

            def bed_status(after_s):
                view = eight_sleep.build_view(st, es_cfg, now=t0 + dt.timedelta(seconds=after_s))
                return next(m for m in view["metrics"] if m["metric_id"] == "pod_bed_temp")["status"]

            self.assertEqual(bed_status(200), "CURRENT")             # would be STALE under 180
            self.assertEqual(bed_status(359), "CURRENT")
            self.assertEqual(bed_status(400), "STALE")
        finally:
            st.close()


class CollectorResilienceTest(unittest.TestCase):
    def test_airgradient_auth_pause_keeps_eight_sleep_polling(self):
        import threading
        st = Store(":memory:")
        self.addCleanup(st.close)
        c = config.validate(cfg(mode="LIVE", base_url="http://192.0.2.10", eight_sleep={"mode": "SYNTHETIC"}))
        rejected = lambda url, timeout=5.0: {"outcome": "AUTH_REJECTED", "http_status": 401, "body": None,
                                             "request_started_at": Clock()(), "response_received_at": Clock()()}
        col = collector.Collector(st, c, fetcher=rejected)
        polls = []
        col.poll_eight_sleep_if_due = lambda: polls.append(1) or (len(polls) >= 3 and col.stop())
        col.sleep = lambda s: None
        result = {}
        th = threading.Thread(target=lambda: result.setdefault("r", col.run()), daemon=True)
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "collector must stop when asked")
        self.assertEqual(result.get("r"), "AUTH_PAUSED")
        self.assertGreaterEqual(len(polls), 3, "Eight Sleep keeps polling while AirGradient is paused")

    def test_tls_context_keeps_verification_strict(self):
        import ssl
        from room_display import adapter
        ctx = adapter.get_ssl_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        self.assertGreater(ctx.cert_store_stats()["x509_ca"], 0, "root CAs available even without python.org certs")

    def test_diag_tells_missing_local_ca_apart_from_untrusted_chain(self):
        import importlib.util
        import ssl
        spec = importlib.util.spec_from_file_location(
            "diag_es", Path(__file__).resolve().parents[4] / "delivery/scripts/diag_eight_sleep.py")
        if spec is None or not Path(spec.origin).is_file():
            self.skipTest("diagnostic script not shipped in this bundle")
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)
        missing = ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate")
        missing.verify_code = 20
        selfsigned = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate in chain")
        selfsigned.verify_code = 19
        self.assertEqual(diag.classify_tls_error(missing), "MISSING_LOCAL_CA")
        self.assertEqual(diag.classify_tls_error(selfsigned), "UNTRUSTED_CHAIN")

    def test_diag_mask_log_line_redacts_email_and_uuid(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "diag_es_mask", Path(__file__).resolve().parents[4] / "delivery/scripts/diag_eight_sleep.py")
        if spec is None or not Path(spec.origin).is_file():
            self.skipTest("diagnostic script not shipped in this bundle")
        diag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(diag)
        raw = "user=alice.smith@example.com id=12345678-90ab-cdef-1234-567890abcdef auth=Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        masked = diag.mask_log_line(raw)
        self.assertNotIn("alice.smith", masked)
        self.assertIn("a***@example.com", masked)
        self.assertNotIn("90ab-cdef-1234-567890abcdef", masked)
        self.assertIn("12345678-****-****-****-************", masked)
        self.assertIn("Bearer [MASKED_TOKEN]", masked)

    def test_lan_mode_keeps_admin_patch_loopback_only(self):
        import io
        from room_display import server
        st = Store(":memory:")
        self.addCleanup(st.close)
        app = server.App(st, config.validate(cfg()))
        app.allow_lan = True
        app.target_host = "0.0.0.0"

        class DummyHandler(server.Handler):
            def __init__(self, peer_ip, host_hdr):
                self.app = app
                self.client_address = (peer_ip, 54321)
                self.command = "PATCH"
                self.path = "/api/v1/admin/display-fields"
                self.headers = {"Host": host_hdr, "Content-Type": "application/json", "Content-Length": "2"}
                self.rfile = io.BytesIO(b"{}")
                self.wfile = io.BytesIO()
                self.sent_code = None

            def send_response(self, code, message=None):
                self.sent_code = code

            def send_header(self, keyword, value):
                pass

            def end_headers(self):
                pass

        # LAN peer can pass _guard() for read-only views, but PATCH /admin must reject with 403 LOOPBACK_ONLY
        lan_h = DummyHandler("192.168.1.50", f"192.168.1.10:{app.port}")
        self.assertTrue(lan_h._guard())
        lan_h.do_PATCH()
        self.assertEqual(lan_h.sent_code, 403)
        self.assertEqual(json.loads(lan_h.wfile.getvalue().decode("utf-8"))["error"], "LOOPBACK_ONLY")


class BundleConfigTest(unittest.TestCase):
    """The packaged config ships AirGradient in LIVE mode so the target Mac keeps re-scanning (never synthetic)."""

    def load_builder(self):
        import importlib.util
        path = Path(__file__).resolve().parents[4] / "delivery" / "scripts" / "build_mac_bundle.py"
        if not path.exists():
            self.skipTest("build_mac_bundle.py is not shipped in the deployed bundle (packaging-machine test)")
        spec = importlib.util.spec_from_file_location("build_mac_bundle_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_synthetic_source_becomes_live_and_local_file_is_untouched(self):
        import tempfile
        builder = self.load_builder()
        with tempfile.TemporaryDirectory() as tmp:
            for src, expected_url in (({"source_id": "ag-room-01", "mode": "SYNTHETIC", "base_url": "http://10.0.0.5"}, "http://10.0.0.5"),
                                      ({"source_id": "ag-room-01", "mode": "SYNTHETIC", "base_url": None}, "http://192.0.2.10")):
                p = Path(tmp) / "config.json"
                original = json.dumps({"room_id": "demo-room-01", "source": src, "eight_sleep": {"mode": "LIVE"}})
                p.write_text(original, encoding="utf-8")
                shipped = json.loads(builder.bundle_config_bytes(p))
                self.assertEqual(shipped["source"]["mode"], "LIVE")
                self.assertEqual(shipped["source"]["base_url"], expected_url)
                self.assertEqual(shipped["eight_sleep"], {"mode": "LIVE"})
                self.assertEqual(p.read_text(encoding="utf-8"), original)   # local config unchanged
                config.validate(shipped)                                     # still a valid config


class RealDbGuardTest(unittest.TestCase):
    """Audit P2: the whole suite runs under a guard that forbids non-read-only access to the real runtime DB."""

    def test_guard_blocks_writable_access_and_allows_strict_read_only(self):
        import sqlite3
        import _helpers
        self.assertTrue(getattr(sqlite3.connect, "_ctds_real_db_guard", False))
        real = _helpers.REAL_RUNTIME_DB
        for attempt in (lambda: sqlite3.connect(real),
                        lambda: sqlite3.connect(f"file:{real}?mode=ro", uri=True),
                        lambda: sqlite3.connect(f"file:{real}?mode=ro&immutable=1")):   # uri flag missing
            with self.assertRaises(_helpers.RealRuntimeDbAccess):
                attempt()
        _helpers.REAL_DB_VIOLATIONS.clear()   # the three attempts above are intentional
        if Path(real).exists():
            conn = sqlite3.connect(f"file:{real}?mode=ro&immutable=1", uri=True)
            conn.close()
        import contextlib
        with contextlib.closing(sqlite3.connect(":memory:")) as other:   # unrelated databases are unaffected
            other.execute("select 1")


class BindErrorMessageTest(unittest.TestCase):
    def test_messages_distinguish_causes_and_carry_no_secret(self):
        secret = "hunter2-secret"
        cases = [
            (AdapterError("NO_CONNECTION", "Auth network error: timed out"), "AUTH", "无法连接"),
            (AdapterError("AUTH_REJECTED", "Authentication failed: HTTP 401"), "AUTH", "拒绝了账号或密码"),
            (AdapterError("HTTP_ERROR", "Auth HTTP error: 400"), "AUTH", "400"),
            (AdapterError("HTTP_ERROR", "Eight Sleep HTTP 404"), "DISCOVER", "登录成功"),
        ]
        for exc, stage, expected in cases:
            msg = _describe_bind_error(exc, stage)
            self.assertIn(expected, msg)
            self.assertNotIn(secret, msg)


if __name__ == "__main__":
    unittest.main()
