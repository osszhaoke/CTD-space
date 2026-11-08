"""Stage C isolated test suite for AirGradient ONE 8PSL CHN (FW 8.7.6) MQTT & multi-model compatibility.

Covers all 12 required verification items without touching var/ctds-c01/ production state:
 1. 9PSL original HTTP JSON regression (0.1.0 mapping unchanged).
 2. 8PSL CHN empirical JSON format (FW 8.7.6: 13 keys, tvoc in ppb, forbidden ssid/wifi/boot/wdog/hwVersion/light).
 3. Units verification (pm2_5 µg/m³, co2 ppm, temperature ℃, humidity %RH, tvoc_ppb ppb).
 4. Missing (NOT_PROVIDED), null (EXPLICIT_NULL), 0 (OBSERVED 0.0), and invalid values handled distinctly.
 5. Legacy firmware HTTP 404 (NOT_FOUND).
 6. HTTP timeout & connection refused (NO_CONNECTION).
 7. MQTT QoS 1 duplicates, control-topic rejection, http_status=None, disconnect watchdog & recovery, subscriber reconnect.
 8. Receipt time vs observation time separation (provider_observed_at=None, RECEIPT_ONLY).
 9. LIVE vs SYNTHETIC strict isolation & cross-protocol event identity separation.
10. Eight Sleep independent polling unaffected by MQTT normal, disconnected, or AUTH_PAUSED states.
11. Production runtime database (var/ctds-c01/ctds_c01.sqlite3) strictly unmutated.
12. StarMap loopback API (/display, /series, /timeline, /capabilities) reading MQTT-ingested history.
"""
import datetime as dt
import http.client
import json
import socket
import threading
import unittest

import _helpers
from _helpers import REAL_DB_VIOLATIONS, SAMPLE, T0, Clock, FakeDevice, body, cfg

from room_display import adapter, catalog, collector, config, mqtt_adapter, projection, server
from room_display.store import Store

# Shape modeled on empirical AirGradient ONE 8PSL CHN (HW 8.0.0 / FW 8.7.6) payload; all identifiers synthetic.
SAMPLE_8PSL = {
    "wifi": -41,
    "ssid": "SyntheticHomeWiFi",
    "hwVersion": 8,
    "rco2": 509,
    "pm01": 18,
    "pm02": 27,
    "pm10": 31,
    "atmp": 21.3,
    "rhum": 67.0,
    "tvoc": 0,
    "light": 12,
    "boot": 4,
    "wdog": 0,
}


def body_8psl(**overrides):
    doc = dict(SAMPLE_8PSL)
    for k, v in overrides.items():
        if v is ...:
            doc.pop(k, None)
        else:
            doc[k] = v
    return json.dumps(doc).encode("utf-8")


def mqtt_cfg(**kw):
    source_extra = kw.pop("source_extra", {})
    src = {
        "source_id": "ag-8psl-01",
        "mode": kw.pop("mode", "LIVE"),
        "protocol": "mqtt",
        "mqtt_mode": "listen",
        "mqtt_listen_host": "127.0.0.1",
        "mqtt_listen_port": 0,
        "mqtt_inactivity_s": 120,
        "mqtt_dedup_window_s": 15,
        "binding_revision": 1,
    }
    src.update(source_extra)
    doc = {
        "room_id": "demo-room-01",
        "db_path": ":memory:",
        "source": src,
    }
    doc.update(kw)
    return config.validate(doc)


def _encode_publish_packet(topic, payload_bytes, *, qos=1, packet_id=1, dup=False, retain=False):
    flags = ((1 if dup else 0) << 3) | ((qos & 0x03) << 1) | (1 if retain else 0)
    first = 0x30 | flags
    var_hdr = mqtt_adapter._encode_utf8(topic)
    if qos > 0:
        var_hdr += int(packet_id).to_bytes(2, "big")
    rem = var_hdr + bytes(payload_bytes)
    return bytes([first]) + mqtt_adapter._encode_varlen(len(rem)) + rem


def _send_8psl_session(host, port, topic, payload_bytes, *, qos=1, packet_id=1, dup=False,
                       username=None, password=None):
    """Simulate 8PSL FW 8.7.6 short-lived MQTT v3.1.1 TCP publish session."""
    with socket.create_connection((host, port), timeout=3.0) as s:
        s.settimeout(3.0)
        s.sendall(mqtt_adapter._build_connect_packet(
            "airgradient-sim-8psl", keep_alive_s=120, username=username, password=password,
        ))
        first, connack_body = mqtt_adapter._read_packet(s)
        rc = connack_body[1]
        if rc != 0:
            return rc, None
        s.sendall(_encode_publish_packet(topic, payload_bytes, qos=qos, packet_id=packet_id, dup=dup))
        if qos == 1:
            p_first, puback_body = mqtt_adapter._read_packet(s)
            return rc, (p_first >> 4, int.from_bytes(puback_body[:2], "big"))
        return rc, None


class FakeMqttBroker:
    """Minimal loopback MQTT v3.1.1 broker for testing MqttReadOnlySubscriber."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.url = f"mqtt://127.0.0.1:{self.port}"
        self._stop = threading.Event()
        self._sub_ready = threading.Event()
        self._client_lock = threading.Lock()
        self._client = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            conn.settimeout(1.0)
            with self._client_lock:
                self._client = conn
            try:
                first, _ = mqtt_adapter._read_packet(conn)
                if (first >> 4) != 1:
                    conn.close()
                    continue
                conn.sendall(b"\x20\x02\x00\x00")
                first, sub_body = mqtt_adapter._read_packet(conn)
                if (first >> 4) == 8:
                    pkt_id = sub_body[:2]
                    conn.sendall(b"\x90\x03" + pkt_id + b"\x01")
                    self._sub_ready.set()
                while not self._stop.is_set():
                    try:
                        p_first, _ = mqtt_adapter._read_packet(conn)
                        if (p_first >> 4) == 12:
                            conn.sendall(b"\xD0\x00")
                        elif (p_first >> 4) == 14:
                            break
                    except (socket.timeout, TimeoutError):
                        continue
            except (EOFError, ConnectionError, OSError, adapter.AdapterError):
                pass
            finally:
                with self._client_lock:
                    if self._client is conn:
                        self._client = None
                try:
                    conn.close()
                except OSError:
                    pass

    def wait_subscriber(self, timeout=3.0):
        return self._sub_ready.wait(timeout)

    def push(self, topic, payload_bytes, packet_id=1):
        with self._client_lock:
            c = self._client
        if c is None:
            raise RuntimeError("no subscriber connected")
        c.sendall(_encode_publish_packet(topic, payload_bytes, qos=1, packet_id=packet_id))

    def drop_subscriber(self):
        self._sub_ready.clear()
        with self._client_lock:
            c = self._client
            self._client = None
        if c is not None:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self.drop_subscriber()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class Ag8pslSchemaAndRegressionTest(unittest.TestCase):
    """Items 1, 2, 3, 4, 5, 6, 8: 9PSL regression + 8PSL schema, units, missing/null/0/invalid, HTTP 404/timeout."""

    def test_item1_9psl_http_regression_unchanged(self):
        obs, sha = adapter.normalize(
            body(), source_id="ag-9psl-01", room_id="demo-room-01",
            attempt_id="att-9psl", received_at=T0, provenance="LIVE",
        )
        self.assertEqual(len(obs), 9)
        by_id = {o["metric_id"]: o for o in obs}
        self.assertNotIn("tvoc_ppb", by_id)
        self.assertEqual(by_id["pm2_5"]["value"], 7.0)
        self.assertEqual(by_id["co2"]["value"], 447.0)
        self.assertEqual(by_id["temperature"]["value"], 25.87)
        self.assertEqual(by_id["humidity"]["value"], 43.0)
        self.assertEqual(by_id["tvoc_index"]["value"], 100.0)
        self.assertEqual(by_id["nox_index"]["value"], 1.0)
        self.assertEqual(by_id["pm2_5_compensated"]["value_status"], "NOT_PROVIDED")
        self.assertTrue(all(o["mapping_version"] == catalog.MAPPING_VERSION for o in obs))
        self.assertEqual(len(sha), 64)

    def test_item2_and_item3_and_item8_8psl_empirical_schema_units_and_time_separation(self):
        obs, _ = adapter.normalize(
            body_8psl(), source_id="ag-8psl-01", room_id="demo-room-01",
            attempt_id="att-8psl", received_at=T0, provenance="LIVE",
            mapping_version=catalog.MQTT_MAPPING_VERSION,
            event_identity="mqtt:deadbeefcafe:att-8psl",
        )
        self.assertEqual(len(obs), 10)
        by_id = {o["metric_id"]: o for o in obs}

        # Core metrics & canonical units (Item 3)
        self.assertEqual((by_id["pm2_5"]["value_status"], by_id["pm2_5"]["value"], by_id["pm2_5"]["unit"]),
                         ("OBSERVED", 27.0, "µg/m³"))
        self.assertEqual((by_id["co2"]["value_status"], by_id["co2"]["value"], by_id["co2"]["unit"]),
                         ("OBSERVED", 509.0, "ppm"))
        self.assertEqual((by_id["temperature"]["value_status"], by_id["temperature"]["value"], by_id["temperature"]["unit"]),
                         ("OBSERVED", 21.3, "℃"))
        self.assertEqual((by_id["humidity"]["value_status"], by_id["humidity"]["value"], by_id["humidity"]["unit"]),
                         ("OBSERVED", 67.0, "%RH"))
        self.assertEqual((by_id["pm1"]["value_status"], by_id["pm1"]["value"], by_id["pm1"]["unit"]),
                         ("OBSERVED", 18.0, "µg/m³"))
        self.assertEqual((by_id["pm10"]["value_status"], by_id["pm10"]["value"], by_id["pm10"]["unit"]),
                         ("OBSERVED", 31.0, "µg/m³"))

        # Legacy 8PSL tvoc (ppb) mapped separately; SGP41 tvocIndex & noxIndex stay NOT_PROVIDED (never filled with 0!)
        self.assertEqual((by_id["tvoc_ppb"]["value_status"], by_id["tvoc_ppb"]["value"], by_id["tvoc_ppb"]["unit"]),
                         ("OBSERVED", 0.0, "ppb"))
        self.assertEqual((by_id["tvoc_index"]["value_status"], by_id["tvoc_index"]["value"], by_id["tvoc_index"]["unit"]),
                         ("NOT_PROVIDED", None, "指数"))
        self.assertEqual((by_id["nox_index"]["value_status"], by_id["nox_index"]["value"], by_id["nox_index"]["unit"]),
                         ("NOT_PROVIDED", None, "指数"))
        self.assertEqual((by_id["pm2_5_compensated"]["value_status"], by_id["pm2_5_compensated"]["value"]),
                         ("NOT_PROVIDED", None))

        # Item 8: Receipt time vs observation time strictly separated (no fabricated provider_observed_at)
        for o in obs:
            self.assertIsNone(o["provider_observed_at"])
            self.assertEqual(o["gateway_received_at"], "2026-10-12T08:00:00.000Z")
            self.assertEqual(o["time_basis"], "RECEIPT_ONLY")
            self.assertEqual(o["mapping_version"], catalog.MQTT_MAPPING_VERSION)
            self.assertEqual(o["source_event_identity"], "mqtt:deadbeefcafe:att-8psl")

        # Forbidden network/device fields (especially Wi-Fi SSID!) never emitted
        serialized = json.dumps(obs, ensure_ascii=False)
        for secret in ("SyntheticHomeWiFi", "ssid", "wifi", "hwVersion", "light", "boot", "wdog", str(SAMPLE["serialno"])):
            self.assertNotIn(secret, serialized)

    def test_item4_missing_null_zero_and_invalid_distinct_on_8psl(self):
        def norm8(**kw):
            obs, _ = adapter.normalize(
                body_8psl(**kw), source_id="ag-8psl-01", room_id="demo-room-01",
                attempt_id="a1", received_at=T0, provenance="LIVE",
                mapping_version=catalog.MQTT_MAPPING_VERSION,
            )
            return {o["metric_id"]: o for o in obs}

        # Zero is valid OBSERVED 0.0
        z = norm8(pm02=0, rco2=0, atmp=0, rhum=0, tvoc=0)
        for mid in ("pm2_5", "co2", "temperature", "humidity", "tvoc_ppb"):
            self.assertEqual((z[mid]["value_status"], z[mid]["value"]), ("OBSERVED", 0.0), mid)

        # Missing is NOT_PROVIDED with None (never filled as 0!)
        m = norm8(pm02=..., rco2=..., tvoc=...)
        for mid in ("pm2_5", "co2", "tvoc_ppb", "tvoc_index", "nox_index"):
            self.assertEqual((m[mid]["value_status"], m[mid]["value"]), ("NOT_PROVIDED", None), mid)

        # Explicit null is EXPLICIT_NULL with None
        n = norm8(pm02=None, atmp=None, tvoc=None)
        for mid in ("pm2_5", "temperature", "tvoc_ppb"):
            self.assertEqual((n[mid]["value_status"], n[mid]["value"]), ("EXPLICIT_NULL", None), mid)

        # Invalid types and out-of-range numbers are INVALID with None
        inv = norm8(pm02=False, rco2="509", atmp=99.9, rhum=-1, tvoc=70000)
        for mid in ("pm2_5", "co2", "temperature", "humidity", "tvoc_ppb"):
            self.assertEqual((inv[mid]["value_status"], inv[mid]["value"]), ("INVALID", None), mid)

        # High-but-plausible PM2.5 spike (273 µg/m³ from Dashboard) is accepted as OBSERVED within [0, 5000]
        spike = norm8(pm02=273)
        self.assertEqual((spike["pm2_5"]["value_status"], spike["pm2_5"]["value"]), ("OBSERVED", 273.0))

    def test_item5_and_item6_legacy_http_404_timeout_and_econnrefused(self):
        dev = FakeDevice()
        try:
            # Item 5: HTTP 404
            dev.mode = "404"
            s = Store(":memory:")
            c = cfg(mode="LIVE", base_url=dev.base_url, retries=0)
            collector.register(s, c, lambda: T0)
            out_404 = collector.Collector(s, c, clock=lambda: T0, sleeper=lambda _: None).round()
            self.assertEqual(out_404, "NOT_FOUND")
            att = s.latest_attempt(c["source"]["source_id"])
            self.assertEqual((att["outcome"], att["http_status"]), ("NOT_FOUND", 404))
            disp = projection.build_display(s, c, now=T0)
            self.assertEqual(disp["actual_source_connectivity"], "ERROR")
            self.assertIn("不存在", disp["error_summary_zh"])
            s.close()

            # Item 6a: HTTP timeout
            dev.mode = "slow"
            r_slow = adapter.fetch(dev.base_url, timeout=0.3)
            self.assertEqual((r_slow["outcome"], r_slow["http_status"]), ("TIMEOUT", None))
        finally:
            dev.close()

        # Item 6b: TCP connection refused (matches empirical 8PSL port 80 ECONNREFUSED)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        r_refused = adapter.fetch(f"http://127.0.0.1:{closed_port}", timeout=1.0)
        self.assertEqual((r_refused["outcome"], r_refused["http_status"]), ("NO_CONNECTION", None))


class MqttWireAndResilienceTest(unittest.TestCase):
    """Items 7, 9, 10, 11, 12: MQTT wire listener/subscriber, dedup, disconnect/recovery, ES isolation, StarMap API."""

    def test_item7_listener_qos1_dedup_control_topic_rejection_and_no_http_disguise(self):
        clock = Clock(T0)
        s = Store(":memory:")
        c = mqtt_cfg(source_extra={"mqtt_expected_sensor_id": "a1b2c3d4"})
        collector.register(s, c, clock)
        outcomes = []
        done = threading.Event()

        def on_out(o, _dt):
            outcomes.append(o)
            done.set()

        listener = mqtt_adapter.MqttReadOnlyListener(s, c, clock=clock, on_outcome=on_out)
        host, port = listener.start()
        try:
            # 1. Valid QoS 1 publish from 8PSL
            done.clear()
            rc, ack = _send_8psl_session(host, port, "airgradient/readings/a1b2c3d4", body_8psl(pm02=27), packet_id=7)
            self.assertEqual(rc, 0)
            self.assertEqual(ack, (4, 7))  # PUBACK packet type 4, packet_id 7
            self.assertTrue(done.wait(2.0))
            self.assertEqual(outcomes[-1], "MQTT_OK")
            att1 = s.latest_attempt("ag-8psl-01")
            self.assertEqual(att1["outcome"], "MQTT_OK")
            self.assertIsNone(att1["http_status"])  # NEVER disguised as HTTP 200
            self.assertEqual(s.counts(), {"observation": 10, "ingestion_attempt": 1})

            # 2. QoS 1 duplicate retransmission (dup=True) -> MQTT_DUPLICATE, 0 new observations
            clock.advance(3)
            done.clear()
            _send_8psl_session(host, port, "airgradient/readings/a1b2c3d4", body_8psl(pm02=27), packet_id=7, dup=True)
            self.assertTrue(done.wait(2.0))
            self.assertEqual(outcomes[-1], "MQTT_DUPLICATE")
            self.assertIsNone(s.latest_attempt("ag-8psl-01")["http_status"])
            self.assertEqual(s.counts(), {"observation": 10, "ingestion_attempt": 2})

            # 3. Next normal ~90s cycle (dup=False, even with same sensor values) -> accepted as fresh observation!
            clock.advance(89)
            done.clear()
            _send_8psl_session(host, port, "airgradient/readings/a1b2c3d4", body_8psl(pm02=27), packet_id=8, dup=False)
            self.assertTrue(done.wait(2.0))
            self.assertEqual(outcomes[-1], "MQTT_OK")
            self.assertEqual(s.counts(), {"observation": 20, "ingestion_attempt": 3})

            # 4. Control / config topics strictly rejected (never treated as data source)
            for bad_topic in ("airgradient/control/a1b2c3d4", "airgradient/config/a1b2c3d4", "cmd/reboot"):
                done.clear()
                _send_8psl_session(host, port, bad_topic, body_8psl(), packet_id=9)
                self.assertTrue(done.wait(2.0))
                self.assertEqual(outcomes[-1], "MQTT_TOPIC_REJECTED")
            self.assertEqual(s.counts()["observation"], 20)

            # 5. Unexpected sensor ID rejected when mqtt_expected_sensor_id is pinned
            done.clear()
            _send_8psl_session(host, port, "airgradient/readings/ff00ff00", body_8psl(), packet_id=10)
            self.assertTrue(done.wait(2.0))
            self.assertEqual(outcomes[-1], "MQTT_SENSOR_MISMATCH")
            self.assertEqual(s.counts()["observation"], 20)

            # 6. Oversized payload (> MAX_BYTES) rejected safely
            done.clear()
            huge = b'{"pm02":1,"pad":"' + (b"x" * (adapter.MAX_BYTES + 200)) + b'"}'
            try:
                _send_8psl_session(host, port, "airgradient/readings/a1b2c3d4", huge, qos=0)
            except OSError:
                pass
            self.assertTrue(done.wait(2.0))
            self.assertEqual(outcomes[-1], "MQTT_TOO_LARGE")
            self.assertEqual(s.counts()["observation"], 20)

            # 7. Transport-level read-only hard assertion forbids outbound PUBLISH
            with self.assertRaises(AssertionError):
                mqtt_adapter._send_readonly_packet(None, b"\x30\x00")
        finally:
            listener.stop()
            s.close()

    def test_item7_disconnect_watchdog_and_auto_recovery_plus_subscriber_reconnect(self):
        # Part A: Inactivity watchdog -> MQTT_DISCONNECTED -> auto-recovery on next MQTT_OK
        clock = Clock(T0)
        s = Store(":memory:")
        c = mqtt_cfg(stale_after_s=180, source_extra={"mqtt_inactivity_s": 120})
        collector.register(s, c, clock)
        col = collector.Collector(s, c, clock=clock, sleeper=lambda _: None)

        mqtt_adapter.ingest_mqtt_message(
            s, c, topic="airgradient/readings/a1b2c3d4", payload=body_8psl(pm02=24, rco2=520),
            received_at=clock(), clock=clock,
        )
        col._on_mqtt_outcome("MQTT_OK", clock())
        d1 = projection.build_display(s, c, now=clock())
        self.assertEqual((d1["actual_source_connectivity"], d1["error_summary_zh"]), ("CONNECTED", None))

        # Advance past mqtt_inactivity_s (120s) -> watchdog records single MQTT_DISCONNECTED
        clock.advance(130)
        self.assertTrue(col.check_mqtt_watchdog())
        self.assertFalse(col.check_mqtt_watchdog())  # does not spam duplicate disconnect rows
        att_disc = s.latest_attempt("ag-8psl-01")
        self.assertEqual((att_disc["outcome"], att_disc["http_status"]), ("MQTT_DISCONNECTED", None))
        d2 = projection.build_display(s, c, now=clock())
        self.assertEqual(d2["actual_source_connectivity"], "DISCONNECTED")
        self.assertEqual(d2["error_summary_zh"], "MQTT 设备超时未上报")
        # Last known PM2.5 is still preserved while disconnected
        pm_card = next(m for m in d2["metrics"] if m["metric_id"] == "pm2_5")
        self.assertEqual(pm_card["value"], 24.0)

        # Device recovers and publishes again -> immediately back to CONNECTED
        clock.advance(10)
        mqtt_adapter.ingest_mqtt_message(
            s, c, topic="airgradient/readings/a1b2c3d4", payload=body_8psl(pm02=19, rco2=495),
            received_at=clock(), clock=clock,
        )
        col._on_mqtt_outcome("MQTT_OK", clock())
        d3 = projection.build_display(s, c, now=clock())
        self.assertEqual((d3["actual_source_connectivity"], d3["error_summary_zh"]), ("CONNECTED", None))
        self.assertEqual(next(m for m in d3["metrics"] if m["metric_id"] == "pm2_5")["value"], 19.0)
        s.close()

        # Part B: MqttReadOnlySubscriber against external broker + drop & reconnect recovery
        broker = FakeMqttBroker()
        s_sub = Store(":memory:")
        try:
            c_sub = mqtt_cfg(source_extra={"mqtt_mode": "subscribe", "mqtt_broker_url": broker.url})
            collector.register(s_sub, c_sub)
            ev = threading.Event()
            seen = []

            def on_sub(o, _dt):
                seen.append(o)
                if o == "MQTT_OK":
                    ev.set()

            sub = mqtt_adapter.MqttReadOnlySubscriber(
                s_sub, c_sub, on_outcome=on_sub, connect_timeout_s=2.0, keep_alive_s=10,
            )
            sub.start()
            try:
                self.assertTrue(broker.wait_subscriber(3.0))
                broker.push("airgradient/readings/a1b2c3d4", body_8psl(pm02=33), packet_id=1)
                self.assertTrue(ev.wait(2.0))
                self.assertEqual(s_sub.latest_observation("demo-room-01", "pm2_5", provenance="LIVE")["value"], 33.0)

                # Simulate broker connection drop -> subscriber records MQTT_DISCONNECTED, reconnects, and receives next push!
                ev.clear()
                broker.drop_subscriber()
                self.assertTrue(broker.wait_subscriber(4.0))
                broker.push("airgradient/readings/a1b2c3d4", body_8psl(pm02=21), packet_id=2)
                self.assertTrue(ev.wait(2.0))
                self.assertIn("MQTT_DISCONNECTED", seen)
                self.assertEqual(s_sub.latest_observation("demo-room-01", "pm2_5", provenance="LIVE")["value"], 21.0)
            finally:
                sub.stop()
        finally:
            broker.close()
            s_sub.close()

    def test_item9_live_vs_synthetic_and_cross_protocol_isolation(self):
        clock = Clock(T0)
        s = Store(":memory:")
        try:
            # 1. Write SYNTHETIC round first
            syn_c = cfg(mode="SYNTHETIC")
            collector.register(s, syn_c, clock)
            collector.Collector(s, syn_c, clock=clock, sleeper=lambda _: None).round()
            syn_val = s.latest_observation("demo-room-01", "pm2_5", provenance="SYNTHETIC")["value"]
            self.assertIsNotNone(syn_val)

            # 2. Switch view config to LIVE MQTT before any LIVE data arrives -> NEVER_CONNECTED, no synthetic bleed!
            live_c = mqtt_cfg(mode="LIVE")
            d_before = projection.build_display(s, live_c, now=clock())
            self.assertEqual(d_before["source_mode"], "LIVE")
            self.assertEqual(d_before["actual_source_connectivity"], "NEVER_CONNECTED")
            self.assertTrue(all(m["value"] is None for m in d_before["metrics"]))

            # 3. Ingest LIVE MQTT 8PSL observation with same attempt_id="same-id" as an HTTP REPLAY observation -> distinct event_ids!
            clock.advance(60)
            http_obs, _ = adapter.normalize(
                body(pm02=11), source_id="ag-8psl-01", room_id="demo-room-01",
                attempt_id="same-id", received_at=clock(), provenance="REPLAY",
            )
            s.record_attempt({
                "attempt_id": "http-same-id", "source_id": "ag-8psl-01",
                "request_started_at": adapter.iso(clock()), "response_received_at": adapter.iso(clock()),
                "outcome": "OK", "http_status": 200, "payload_sha256": "0" * 64, "data_provenance": "REPLAY",
            }, http_obs)
            mqtt_out = mqtt_adapter.ingest_mqtt_message(
                s, live_c, topic="airgradient/readings/a1b2c3d4", payload=body_8psl(pm02=42),
                received_at=clock(), clock=clock, attempt_id="same-id", provenance="LIVE",
            )
            self.assertEqual(mqtt_out, "MQTT_OK")

            # LIVE display sees only 42.0; SYNTHETIC display sees only syn_val!
            d_live = projection.build_display(s, live_c, now=clock())
            self.assertEqual(next(m for m in d_live["metrics"] if m["metric_id"] == "pm2_5")["value"], 42.0)
            d_syn = projection.build_display(s, syn_c, now=clock())
            self.assertEqual(next(m for m in d_syn["metrics"] if m["metric_id"] == "pm2_5")["value"], syn_val)
        finally:
            s.close()

    def test_item10_eight_sleep_polling_unaffected_by_mqtt_auth_pause_or_disconnect(self):
        clock = Clock(T0)
        s = Store(":memory:")
        c = mqtt_cfg(
            source_extra={"mqtt_username": "ag-op", "mqtt_password": "correct-password"},
            eight_sleep={"source_id": "es-room-01", "mode": "SYNTHETIC", "side": "solo", "binding_revision": 1},
        )
        collector.register(s, c, clock)
        es_polls_after_pause = threading.Event()
        orig_poll = collector.Collector.poll_eight_sleep_if_due
        calls = []

        def tracked_poll(col_self):
            clock.advance(65)
            orig_poll(col_self)
            calls.append(col_self.paused_reason)
            if col_self.paused_reason == "AUTH_PAUSED" and sum(1 for x in calls if x == "AUTH_PAUSED") >= 2:
                es_polls_after_pause.set()
                col_self.stop()

        col = collector.Collector(s, c, clock=clock, sleeper=lambda _: threading.Event().wait(0.01))
        col.poll_eight_sleep_if_due = lambda: tracked_poll(col)
        worker = threading.Thread(target=col.run, daemon=True)
        worker.start()
        try:
            deadline = threading.Event()
            for _ in range(40):
                if col.mqtt_bound_address is not None:
                    break
                deadline.wait(0.05)
            host, port = col.mqtt_bound_address
            # Trigger MQTT_AUTH_REJECTED with wrong password -> AirGradient pauses, Eight Sleep keeps polling!
            rc, _ = _send_8psl_session(
                host, port, "airgradient/readings/a1b2c3d4", body_8psl(),
                username="ag-op", password="wrong-password",
            )
            self.assertEqual(rc, 4)
            self.assertTrue(es_polls_after_pause.wait(3.0))
            worker.join(timeout=2.0)
            self.assertEqual(s.source("ag-8psl-01")["status"], "AUTH_PAUSED")
            d = projection.build_display(s, c, now=clock())
            self.assertEqual(d["actual_source_connectivity"], "AUTH_PAUSED")
            self.assertEqual(d["eight_sleep"]["mode"], "SYNTHETIC")
            es_bed = next(m for m in d["eight_sleep"]["metrics"] if m["metric_id"] == "pod_left_bed_temp")
            self.assertIsNotNone(es_bed["value"])
        finally:
            col.stop()
            worker.join(timeout=2.0)
            s.close()

    def test_item11_and_item12_runtime_db_untouched_and_starmap_http_api_reads_mqtt_series(self):
        # Item 11: Real runtime DB guard active and zero violations
        self.assertEqual(REAL_DB_VIOLATIONS, [])
        redirected = config.resolve_path("var/ctds-c01/ctds_c01.sqlite3")
        self.assertNotEqual(str(redirected.resolve()), _helpers.REAL_RUNTIME_DB)

        # Item 12: Multi-cycle MQTT ingestion -> StarMap /display, /series, /timeline, /capabilities
        clock = Clock(adapter.utc_now() - dt.timedelta(minutes=10))
        s = Store(":memory:")
        c = mqtt_cfg()
        collector.register(s, c, clock)
        samples = [(29, 509, 21.3, 67.0, 0), (26, 515, 21.4, 66.8, 12), (25, 512, 21.4, 66.5, 8)]
        for pm, co2, tmp, hum, tvoc_ppb in samples:
            out = mqtt_adapter.ingest_mqtt_message(
                s, c, topic="airgradient/readings/a1b2c3d4",
                payload=body_8psl(pm02=pm, rco2=co2, atmp=tmp, rhum=hum, tvoc=tvoc_ppb),
                received_at=clock(), clock=clock,
            )
            self.assertEqual(out, "MQTT_OK")
            clock.advance(90)

        srv = server.make_server(s, c, port=0)
        port = srv.server_address[1]
        server.serve_in_thread(srv)
        try:
            def get_json(path):
                hc = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                hc.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
                resp = hc.getresponse()
                raw = resp.read()
                hc.close()
                return resp.status, json.loads(raw)

            st, disp = get_json("/api/v1/rooms/demo-room-01/display")
            self.assertEqual(st, 200)
            self.assertEqual((disp["source_mode"], disp["actual_source_connectivity"]), ("LIVE", "CONNECTED"))
            self.assertIsNone(disp["error_summary_zh"])
            m_map = {m["metric_id"]: m for m in disp["metrics"]}
            self.assertEqual((m_map["pm2_5"]["value"], len(m_map["pm2_5"]["trend"])), (25.0, 3))
            self.assertEqual((m_map["co2"]["value"], len(m_map["co2"]["trend"])), (512.0, 3))
            self.assertEqual((m_map["temperature"]["value"], m_map["humidity"]["value"]), (21.4, 66.5))
            # Missing SGP41 tvocIndex stays NOT_PROVIDED with None (never 0!), while 8PSL tvoc_ppb is projected on display
            self.assertEqual((m_map["tvoc_index"]["status"], m_map["tvoc_index"]["value"]), ("NOT_PROVIDED", None))
            self.assertEqual(
                (m_map["tvoc_ppb"]["status"], m_map["tvoc_ppb"]["value"], m_map["tvoc_ppb"]["unit"], len(m_map["tvoc_ppb"]["trend"])),
                (m_map["pm2_5"]["status"], 8.0, "ppb", 3),
            )
            fresh_map = {m["metric_id"]: m for m in projection.build_display(s, c, now=clock())["metrics"]}
            self.assertEqual((fresh_map["tvoc_ppb"]["status"], fresh_map["tvoc_ppb"]["value"]), ("CURRENT", 8.0))

            # /series works for all core metrics AND legacy tvoc_ppb
            for mid, expected_unit, expected_vals in (
                ("pm2_5", "µg/m³", [29.0, 26.0, 25.0]),
                ("co2", "ppm", [509.0, 515.0, 512.0]),
                ("temperature", "℃", [21.3, 21.4, 21.4]),
                ("humidity", "%RH", [67.0, 66.8, 66.5]),
                ("tvoc_ppb", "ppb", [0.0, 12.0, 8.0]),
            ):
                st_s, ser = get_json(f"/api/v1/rooms/demo-room-01/series?metric={mid}&hours=24")
                self.assertEqual(st_s, 200, mid)
                self.assertEqual((ser["unit"], ser["provenance"]), (expected_unit, "LIVE"))
                self.assertEqual([p[1] for p in ser["points"]], expected_vals)

            # /timeline returns 24h nodes + now
            st_t, tl = get_json("/api/v1/rooms/demo-room-01/timeline?hours=24")
            self.assertEqual(st_t, 200)
            self.assertEqual(len(tl["nodes"]), 25)

            # /capabilities marks observed 8PSL metrics VERIFIED and unprovided SGP41 metrics DOC_ONLY
            st_c, cap = get_json("/api/v1/sources/capabilities")
            self.assertEqual(st_c, 200)
            self.assertEqual(cap["doc_ref"], catalog.MQTT_DOC_REF)
            cap_map = {x["metric_id"]: x["availability"] for x in cap["airgradient"]}
            for mid in ("pm2_5", "co2", "temperature", "humidity", "pm1", "pm10", "tvoc_ppb"):
                self.assertEqual(cap_map[mid], "VERIFIED", mid)
            for mid in ("tvoc_index", "nox_index", "pm2_5_compensated"):
                self.assertEqual(cap_map[mid], "DOC_ONLY", mid)
        finally:
            srv.shutdown()
            srv.server_close()
            s.close()

    def test_td_hardening_port_conflict_non_mqtt_fast_drop_and_half_open_ping(self):
        # 1. Non-MQTT traffic (e.g. stray HTTP GET on port 1883) fast-dropped on byte 1 with 0 attempts
        s1 = Store(":memory:")
        c1 = mqtt_cfg()
        listener = mqtt_adapter.MqttReadOnlyListener(s1, c1, socket_timeout_s=0.5)
        host, port = listener.start()
        try:
            with socket.create_connection((host, port), timeout=1.0) as raw:
                raw.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                raw.settimeout(1.0)
                try:
                    eof_or_rst = raw.recv(16)
                except ConnectionResetError:
                    eof_or_rst = b""
                self.assertEqual(eof_or_rst, b"")
            self.assertEqual(s1.counts(), {"observation": 0, "ingestion_attempt": 0})
        finally:
            listener.stop()
            s1.close()

        # 2. Port conflict (EADDRINUSE) in Collector.run() records MQTT_DISCONNECTED, keeps Eight Sleep polling,
        #    and recovers automatically once conflicting socket is released!
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        conflict_port = blocker.getsockname()[1]
        clock = Clock(T0)
        s2 = Store(":memory:")
        c2 = mqtt_cfg(
            source_extra={"mqtt_listen_host": "127.0.0.1", "mqtt_listen_port": conflict_port},
            eight_sleep={"source_id": "es-room-01", "mode": "SYNTHETIC", "side": "solo", "binding_revision": 1},
        )
        collector.register(s2, c2, clock)
        rebound = threading.Event()
        orig_poll = collector.Collector.poll_eight_sleep_if_due
        ticks = [0]

        def step_sleep(_sec):
            ticks[0] += 1
            clock.advance(65)
            if ticks[0] == 2:
                blocker.close()  # release port so collector's retry binds cleanly
            if col._mqtt_receiver is not None:
                rebound.set()
            threading.Event().wait(0.01)

        col = collector.Collector(s2, c2, clock=clock, sleeper=step_sleep)
        col.poll_eight_sleep_if_due = lambda: orig_poll(col)
        t = threading.Thread(target=col.run, daemon=True)
        t.start()
        try:
            self.assertTrue(rebound.wait(3.0))
            # Eight Sleep polled despite initial MQTT port conflict, and MQTT_DISCONNECTED was recorded
            self.assertIsNotNone(s2.latest_observation("demo-room-01", "pod_left_bed_temp", provenance="SYNTHETIC"))
            self.assertEqual(s2.latest_attempt("ag-8psl-01")["outcome"], "MQTT_DISCONNECTED")
            # Now send valid 8PSL reading to recovered listener -> MQTT_OK!
            rc, _ = _send_8psl_session("127.0.0.1", conflict_port, "airgradient/readings/a1b2c3d4", body_8psl(pm02=16))
            self.assertEqual(rc, 0)
            for _ in range(30):
                if s2.latest_attempt("ag-8psl-01")["outcome"] == "MQTT_OK":
                    break
                threading.Event().wait(0.05)
            self.assertEqual(s2.latest_attempt("ag-8psl-01")["outcome"], "MQTT_OK")
        finally:
            try:
                blocker.close()
            except OSError:
                pass
            col.stop()
            t.join(timeout=2.0)
            s2.close()

        # 3. Half-open silent broker (never replies PINGRESP) triggers timeout -> MQTT_DISCONNECTED & reconnect
        silent_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        silent_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        silent_srv.bind(("127.0.0.1", 0))
        silent_srv.listen(2)
        silent_port = silent_srv.getsockname()[1]
        stop_silent = threading.Event()
        conns_seen = []

        def silent_broker():
            silent_srv.settimeout(0.2)
            while not stop_silent.is_set():
                try:
                     sc, _ = silent_srv.accept()
                except (socket.timeout, TimeoutError, OSError):
                    continue
                conns_seen.append(sc)
                try:
                    sc.settimeout(1.0)
                    mqtt_adapter._read_packet(sc)
                    sc.sendall(b"\x20\x02\x00\x00")
                    _, sb = mqtt_adapter._read_packet(sc)
                    sc.sendall(b"\x90\x03" + sb[:2] + b"\x01")
                    # Intentionally blackhole subsequent PINGREQ!
                    stop_silent.wait(2.0)
                except (OSError, EOFError, ConnectionError, adapter.AdapterError):
                    pass
                finally:
                    try:
                        sc.close()
                    except OSError:
                        pass

        bt = threading.Thread(target=silent_broker, daemon=True)
        bt.start()
        s3 = Store(":memory:")
        c3 = mqtt_cfg(source_extra={"mqtt_mode": "subscribe", "mqtt_broker_url": f"mqtt://127.0.0.1:{silent_port}"})
        disc_ev = threading.Event()
        sub = mqtt_adapter.MqttReadOnlySubscriber(
            s3, c3, keep_alive_s=1, connect_timeout_s=1.0,
            on_outcome=lambda o, _: disc_ev.set() if o == "MQTT_DISCONNECTED" else None,
        )
        sub.start()
        try:
            self.assertTrue(disc_ev.wait(3.0))
            self.assertEqual(s3.latest_attempt("ag-8psl-01")["outcome"], "MQTT_DISCONNECTED")
        finally:
            sub.stop()
            stop_silent.set()
            try:
                silent_srv.close()
            except OSError:
                pass
            bt.join(timeout=2.0)
            s3.close()


if __name__ == "__main__":
    unittest.main()
