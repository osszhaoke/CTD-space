"""Loopback-only read API + static Chinese display. No device access from the browser.

Routes: /healthz, /readyz, /api/v1/rooms/<room>/display, /api/v1/sources/capabilities,
GET/PATCH /api/v1/admin/display-fields (loopback, JSON, strict allowlist), static UI.
"""
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adapter, catalog, projection

log = logging.getLogger("ctds.c01.server")
STATIC = Path(__file__).absolute().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/admin": ("admin.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 16384
LABEL_RE = re.compile(r"[^\x00-\x1f\x7f<>&\"'`]{1,20}")
SEC_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def validate_fields_patch(doc):
    """Return normalized items or raise ValueError. Whole-list replace of allowlisted metrics."""
    if type(doc) is not dict or set(doc) != {"fields"} or type(doc["fields"]) is not list:
        raise ValueError("body must be {\"fields\": [...]}")
    items, seen = [], set()
    if not 1 <= len(doc["fields"]) <= len(catalog.METRICS):
        raise ValueError("fields length out of range")
    for it in doc["fields"]:
        if type(it) is not dict or set(it) != {"metric_id", "enabled", "display_order", "label_zh"}:
            raise ValueError("each field needs exactly metric_id/enabled/display_order/label_zh")
        mid = it["metric_id"]
        if mid not in catalog.METRICS or mid in seen:
            raise ValueError("metric_id not allowlisted or duplicated")
        seen.add(mid)
        if type(it["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        if type(it["display_order"]) is not int or not 1 <= it["display_order"] <= 99:
            raise ValueError("display_order must be int 1..99")
        if type(it["label_zh"]) is not str or not LABEL_RE.fullmatch(it["label_zh"]):
            raise ValueError("label_zh must be 1..20 safe characters")
        items.append(dict(it))
    return items


class App:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.port = cfg["listen_port"]


class Handler(BaseHTTPRequestHandler):
    server_version = "ctds-c01"
    sys_version = ""
    app: App = None  # set by make_server

    def log_message(self, fmt, *args):  # minimal, no query strings/bodies
        log.debug("%s %s", self.command, self.path.split("?", 1)[0])

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _guard(self):
        """Loopback peer + exact Host header (DNS-rebinding defence)."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, {"error": "LOOPBACK_ONLY"})
            return False
        allowed = {f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}"}
        if self.headers.get("Host") not in allowed:
            self._send(421, {"error": "HOST_NOT_ALLOWED"})
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        app = self.app
        try:
            if path == "/healthz":
                return self._send(200, {"status": "ok"})
            if path == "/readyz":
                src = app.store.source(app.cfg["source"]["source_id"])
                ready = src is not None and src["status"] == "ACTIVE"
                return self._send(200 if ready else 503, {"ready": ready,
                                                          "source_status": src["status"] if src else "UNREGISTERED"})
            if path == f"/api/v1/rooms/{app.cfg['room_id']}/display":
                return self._send(200, projection.build_display(app.store, app.cfg))
            if path.startswith("/api/v1/rooms/"):
                return self._send(404, {"error": "ROOM_NOT_FOUND"})
            if path == "/api/v1/sources/capabilities":
                verified = app.store.verified_metrics(app.cfg["source"]["source_id"])
                return self._send(200, {"airgradient": catalog.capabilities(verified),
                                        "pod5": catalog.POD5_DESCRIPTOR, "doc_ref": catalog.DOC_REF})
            if path == "/api/v1/admin/display-fields":
                return self._send(200, {"room_id": app.cfg["room_id"],
                                        "config_revision": app.store.config_revision(app.cfg["room_id"]),
                                        "fields": app.store.display_settings(app.cfg["room_id"])})
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                return self._send(200, (STATIC / name).read_bytes(), ctype)
            return self._send(404, {"error": "NOT_FOUND"})
        except Exception:  # never leak internals
            log.exception("GET failed")
            return self._send(500, {"error": "INTERNAL"})

    do_HEAD = do_GET

    def do_PATCH(self):
        if not self._guard():
            return
        if self.path.split("?", 1)[0] != "/api/v1/admin/display-fields":
            return self._send(405, {"error": "METHOD_NOT_ALLOWED"})
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{self.app.port}", f"http://localhost:{self.app.port}"):
            return self._send(403, {"error": "ORIGIN_NOT_ALLOWED"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self._send(415, {"error": "JSON_REQUIRED"})
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"error": "BODY_SIZE"})
        try:
            items = validate_fields_patch(adapter.strict_json(self.rfile.read(length)))
        except (ValueError, adapter.AdapterError) as exc:
            return self._send(400, {"error": "INVALID_FIELDS", "detail": str(exc)})
        rev = self.app.store.save_display_settings(self.app.cfg["room_id"], items, action="LOCAL_ADMIN_PATCH",
                                                   updated_at=adapter.iso(adapter.utc_now()))
        return self._send(200, {"config_revision": rev, "fields": self.app.store.display_settings(self.app.cfg["room_id"])})

    def _deny(self):
        self._send(405, {"error": "METHOD_NOT_ALLOWED"})

    do_POST = do_PUT = do_DELETE = do_OPTIONS = _deny


def make_server(store, cfg, port=None):
    app = App(store, cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer(("127.0.0.1", cfg["listen_port"] if port is None else port), handler)
    app.port = srv.server_address[1]
    srv.daemon_threads = True
    return srv


def serve_in_thread(srv):
    t = threading.Thread(target=srv.serve_forever, name="ctds-c01-http", daemon=True)
    t.start()
    return t
