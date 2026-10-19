"""Shared test helpers. All data here is SYNTHETIC or a loopback fake device; never LIVE evidence."""
import datetime as dt
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).absolute().parents[4]
sys.path.insert(0, str(ROOT / "development/ctds"))

T0 = dt.datetime(2026, 10, 12, 8, 0, 0, tzinfo=dt.timezone.utc)

SAMPLE = {  # shape from pinned doc example; values arbitrary, serial fake
    "wifi": -46, "serialno": "fake000000", "rco2": 447, "pm01": 3, "pm02": 7, "pm10": 8,
    "pm003Count": 442, "atmp": 25.87, "atmpCompensated": 24.47, "rhum": 43, "rhumCompensated": 49,
    "tvocIndex": 100, "tvocRaw": 33051, "noxIndex": 1, "noxRaw": 16307, "boot": 6, "bootCount": 6,
    "ledMode": "pm", "firmware": "3.1.3", "model": "I-9PSL", "monitorDisplayCompensatedValues": True,
}


def body(**overrides):
    d = dict(SAMPLE)
    for k, v in overrides.items():
        if v is ...:
            d.pop(k, None)
        else:
            d[k] = v
    return json.dumps(d).encode()


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t = self.t + dt.timedelta(seconds=seconds)
        return self.t


def cfg(**kw):
    from room_display import config
    doc = {"room_id": "demo-room-01", "db_path": ":memory:",
           "source": {"source_id": "ag-test-01", "mode": kw.pop("mode", "SYNTHETIC"),
                      "base_url": kw.pop("base_url", None), "binding_revision": 1}}
    doc.update(kw)
    return config.validate(doc)


class FakeDevice:
    """Loopback fake AirGradient. mode: ok|401|403|404|302|500|429|big|slow|badjson."""

    def __init__(self):
        self.mode = "ok"
        self.hits = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits += 1
                m = outer.mode
                if self.path != "/measures/current":
                    self.send_response(404); self.end_headers(); return
                if m == "slow":
                    threading.Event().wait(1.5)
                if m in ("401", "403", "404", "500", "429"):
                    self.send_response(int(m)); self.send_header("Content-Length", "0"); self.end_headers(); return
                if m == "302":
                    self.send_response(302); self.send_header("Location", "http://127.0.0.1:9/x")
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                data = b"{" + b" " * 70000 + b"}" if m == "big" else (b"{not json" if m == "badjson" else body())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
