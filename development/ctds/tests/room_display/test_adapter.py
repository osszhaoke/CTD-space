import socket
import unittest

from _helpers import T0, FakeDevice, body

from room_display import adapter


def norm(b, prov="SYNTHETIC"):
    obs, sha = adapter.normalize(b, source_id="ag-test-01", room_id="demo-room-01", attempt_id="a1",
                                 received_at=T0, provenance=prov)
    return {o["metric_id"]: o for o in obs}, sha


class NormalizeTest(unittest.TestCase):
    def test_observed_fields_and_time_basis(self):
        o, sha = norm(body())
        self.assertEqual(o["pm2_5"]["value_status"], "OBSERVED")
        self.assertEqual(o["pm2_5"]["value"], 7.0)
        self.assertEqual(o["co2"]["value"], 447.0)
        self.assertEqual(o["temperature"]["value"], 25.87)  # raw atmp, not atmpCompensated
        for x in o.values():
            self.assertIsNone(x["provider_observed_at"])
            self.assertEqual(x["time_basis"], "RECEIPT_ONLY")
            self.assertEqual(x["gateway_received_at"], "2026-10-12T08:00:00.000Z")
        self.assertEqual(len(sha), 64)

    def test_identity_fields_never_emitted(self):
        o, _ = norm(body())
        blob = repr(o)
        for forbidden in ("fake000000", "serialno", "wifi", "I-9PSL", "3.1.3"):
            self.assertNotIn(forbidden, blob)

    def test_zero_is_valid(self):
        o, _ = norm(body(pm02=0))
        self.assertEqual((o["pm2_5"]["value_status"], o["pm2_5"]["value"]), ("OBSERVED", 0.0))

    def test_null_absent_invalid_distinct(self):
        self.assertEqual(norm(body(pm02=None))[0]["pm2_5"]["value_status"], "EXPLICIT_NULL")
        self.assertEqual(norm(body(pm02=...))[0]["pm2_5"]["value_status"], "NOT_PROVIDED")
        self.assertEqual(norm(body(pm02="7"))[0]["pm2_5"]["value_status"], "INVALID")
        self.assertEqual(norm(body(pm02=True))[0]["pm2_5"]["value_status"], "INVALID")
        self.assertEqual(norm(body(rhum=150))[0]["humidity"]["value_status"], "INVALID")
        self.assertIsNone(norm(body(pm02=None))[0]["pm2_5"]["value"])

    def test_negative_temperature_valid(self):
        o, _ = norm(body(atmp=-5.5))
        self.assertEqual((o["temperature"]["value_status"], o["temperature"]["value"]), ("OBSERVED", -5.5))

    def test_compensated_never_fills_raw(self):
        o, _ = norm(body(pm02=..., pm02Compensated=5.1))
        self.assertEqual(o["pm2_5"]["value_status"], "NOT_PROVIDED")
        self.assertIsNone(o["pm2_5"]["value"])
        self.assertEqual(o["pm2_5_compensated"]["value"], 5.1)
        # documented example lacks pm02Compensated (fw < 3.1.4)
        self.assertEqual(norm(body())[0]["pm2_5_compensated"]["value_status"], "NOT_PROVIDED")

    def test_bad_payloads(self):
        for bad in (b'{"pm02":1,"pm02":2}', b'{"pm02":NaN}', b"[1,2]", b"\xff", b"{"):
            with self.assertRaises(adapter.AdapterError) as cm:
                norm(bad)
            self.assertEqual(cm.exception.code, "INVALID_PAYLOAD")

    def test_event_identity_idempotent(self):
        a, _ = norm(body())
        b, _ = norm(body(pm02=8))
        self.assertEqual(a["pm2_5"]["event_id"], b["pm2_5"]["event_id"])  # same attempt+metric -> same id

    def test_provenance_required(self):
        with self.assertRaises(adapter.AdapterError):
            norm(body(), prov="REAL")


class BaseUrlTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(adapter.validate_base_url("http://192.0.2.10"), "http://192.0.2.10/measures/current")
        self.assertEqual(adapter.validate_base_url("http://host.local:80/"), "http://host.local:80/measures/current")

    def test_rejects(self):
        for bad in ("https://h", "ftp://h", "http://u:p@h", "http://h/x", "http://h?q=1", "http://h#f", "", None, "h"):
            with self.assertRaises(adapter.AdapterError):
                adapter.validate_base_url(bad)


class FetchLoopbackTest(unittest.TestCase):
    """Loopback fake device only; proves adapter error handling, not real device access."""

    @classmethod
    def setUpClass(cls):
        cls.dev = FakeDevice()

    @classmethod
    def tearDownClass(cls):
        cls.dev.close()

    def get(self, mode, timeout=3.0):
        self.dev.mode = mode
        return adapter.fetch(self.dev.base_url, timeout=timeout)

    def test_ok(self):
        r = self.get("ok")
        self.assertEqual((r["outcome"], r["http_status"]), ("OK", 200))
        self.assertIsNotNone(r["response_received_at"])

    def test_status_classes(self):
        expect = {"401": "AUTH_REJECTED", "403": "AUTH_REJECTED", "404": "NOT_FOUND", "500": "HTTP_ERROR",
                  "429": "RATE_LIMITED", "302": "REDIRECT_REFUSED"}
        for mode, outcome in expect.items():
            self.assertEqual(self.get(mode)["outcome"], outcome, mode)

    def test_redirect_not_followed(self):
        before = self.dev.hits
        self.assertEqual(self.get("302")["outcome"], "REDIRECT_REFUSED")
        self.assertEqual(self.dev.hits, before + 1)

    def test_too_large(self):
        r = self.get("big")
        self.assertEqual(r["outcome"], "TOO_LARGE")
        self.assertIsNone(r["body"])

    def test_timeout(self):
        self.assertEqual(self.get("slow", timeout=0.5)["outcome"], "TIMEOUT")

    def test_no_connection(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        self.assertEqual(adapter.fetch(f"http://127.0.0.1:{port}", timeout=1)["outcome"], "NO_CONNECTION")


if __name__ == "__main__":
    unittest.main()
