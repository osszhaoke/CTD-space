"""Loopback-only read API + static Chinese display. No device access from the browser.

Routes: /healthz, /readyz, /api/v1/rooms/<room>/display, /api/v1/rooms/<room>/series?metric=&hours=,
/api/v1/rooms/<room>/timeline?hours=, /api/v1/sources/capabilities,
GET/PATCH /api/v1/admin/display-fields (loopback, JSON, strict allowlist), static UI.
"""
import json
import logging
import os
import re
import threading
import urllib.parse
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import adapter, catalog, projection

log = logging.getLogger("ctds.c01.server")
STATIC = Path(__file__).absolute().parent / "static"
STATIC_FILES = {
    "/": ("preview.html", "text/html; charset=utf-8"),
    "/preview.html": ("preview.html", "text/html; charset=utf-8"),
    "/preview": ("preview.html", "text/html; charset=utf-8"),
    "/static/preview.css": ("preview.css", "text/css; charset=utf-8"),
    "/static/preview.js": ("preview.js", "text/javascript; charset=utf-8"),
    "/admin": ("admin.html", "text/html; charset=utf-8"),
    "/static/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
}
MAX_BODY = 16384
HOURS_RE = re.compile(r"[0-9]{1,2}")
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


def is_in_container():
    """Detect if running strictly inside a container environment."""
    return (
        os.environ.get("CTDS_IN_CONTAINER") == "1"
        or os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
    )


def is_lan_mode():
    """Detect if server is running in LAN sharing mode (allow private network IPs)."""
    return os.environ.get("CTDS_ALLOW_LAN") == "1"


def _is_allowed_client_ip(client_str, is_container=False, allow_lan=False):
    """Check if client IP is loopback or an allowed container/LAN private network."""
    try:
        client_ip = ipaddress.ip_address(client_str)
    except ValueError:
        return False

    if client_ip.is_loopback or client_str in ("127.0.0.1", "::1"):
        return True

    if allow_lan or is_lan_mode():
        if client_ip.is_private:
            return True

    allowed_nets = [ipaddress.ip_network("172.16.0.0/12")] if is_container else []
    extra_nets = os.environ.get("CTDS_ALLOWED_NETWORKS", "")
    if extra_nets:
        for net_str in extra_nets.split(","):
            net_str = net_str.strip()
            if net_str:
                try:
                    allowed_nets.append(ipaddress.ip_network(net_str))
                except ValueError:
                    pass
    return any(client_ip in net for net in allowed_nets)


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

    def _is_container(self):
        return is_in_container()

    def _guard(self):
        """Loopback peer + exact Host header (DNS-rebinding defence).
        In container mode or LAN sharing mode, allowed subnet/private IPs are permitted."""
        client_str = self.client_address[0]
        is_container = self._is_container()
        allow_lan = getattr(self.app, "allow_lan", False) or is_lan_mode()
        if not _is_allowed_client_ip(client_str, is_container=is_container, allow_lan=allow_lan):
            self._send(403, {"error": "LOOPBACK_ONLY"})
            return False

        # Host whitelist check (DNS rebinding defence)
        allowed_hosts = {
            f"127.0.0.1:{self.app.port}",
            f"localhost:{self.app.port}",
            "127.0.0.1",
            "localhost",
        }
        target_host = getattr(self.app, "target_host", "127.0.0.1")
        if target_host in ("0.0.0.0", "::") or is_container or allow_lan:
            allowed_hosts.update({
                f"0.0.0.0:{self.app.port}",
                "0.0.0.0",
                f"host.docker.internal:{self.app.port}",
                "host.docker.internal",
            })
        extra_hosts = os.environ.get("CTDS_ALLOWED_HOSTS", "")
        if extra_hosts:
            for h in extra_hosts.split(","):
                h = h.strip()
                if h:
                    allowed_hosts.add(h)
                    if ":" not in h:
                        allowed_hosts.add(f"{h}:{self.app.port}")

        host_hdr = (self.headers.get("Host") or "").strip()
        # If in LAN sharing mode or target_host is 0.0.0.0, allow private IP host headers
        host_without_port = host_hdr.split(":")[0] if ":" in host_hdr else host_hdr
        is_allowed_private_host = False
        if allow_lan or target_host in ("0.0.0.0", "::"):
            try:
                parsed_host_ip = ipaddress.ip_address(host_without_port)
                if parsed_host_ip.is_private or parsed_host_ip.is_loopback:
                    is_allowed_private_host = True
            except ValueError:
                pass

        if host_hdr not in allowed_hosts and not is_allowed_private_host:
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
            if path == "/api/v1/rooms":
                return self._send(200, {"room_id": app.cfg["room_id"], "rooms": [app.cfg["room_id"]]})
            if path == f"/api/v1/rooms/{app.cfg['room_id']}/display":
                return self._send(200, projection.build_display(app.store, app.cfg))
            if path in (f"/api/v1/rooms/{app.cfg['room_id']}/series", f"/api/v1/rooms/{app.cfg['room_id']}/timeline"):
                query = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                hours_raw = (query.get("hours") or ["24"])[0]
                if not HOURS_RE.fullmatch(hours_raw) or not 1 <= int(hours_raw) <= projection.HISTORY_MAX_HOURS:
                    return self._send(400, {"error": "INVALID_HOURS"})
                if path.endswith("/timeline"):
                    return self._send(200, projection.build_timeline(app.store, app.cfg, hours=int(hours_raw)))
                metric = (query.get("metric") or [""])[0]
                if metric not in projection.history_metric_ids(app.cfg):
                    return self._send(400, {"error": "METRIC_NOT_ALLOWED"})
                return self._send(200, projection.build_series(app.store, app.cfg, metric, int(hours_raw)))
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
        # Under --lan sharing mode, display viewing is shared on LAN, while /admin writes stay host-only.
        if not _is_allowed_client_ip(self.client_address[0], is_container=self._is_container(), allow_lan=False):
            return self._send(403, {"error": "LOOPBACK_ONLY"})
        if self.path.split("?", 1)[0] != "/api/v1/admin/display-fields":
            return self._send(405, {"error": "METHOD_NOT_ALLOWED"})
        origin = self.headers.get("Origin")
        if origin is not None:
            allowed_origins = {
                f"http://127.0.0.1:{self.app.port}",
                f"http://localhost:{self.app.port}",
            }
            if self._is_container():
                allowed_origins.update({
                    f"http://0.0.0.0:{self.app.port}",
                    f"http://host.docker.internal:{self.app.port}",
                })
                extra_origins = os.environ.get("CTDS_ALLOWED_ORIGINS", "")
                if extra_origins:
                    for o in extra_origins.split(","):
                        o = o.strip()
                        if o:
                            allowed_origins.add(o)
            if origin not in allowed_origins:
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


def make_server(store, cfg, port=None, host=None, allow_lan=False):
    app = App(store, cfg)
    app.allow_lan = allow_lan or is_lan_mode()
    default_host = "0.0.0.0" if app.allow_lan else "127.0.0.1"
    target_host = host or os.environ.get("CTDS_LISTEN_HOST") or cfg.get("listen_host", default_host)
    app.target_host = target_host
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((target_host, cfg["listen_port"] if port is None else port), handler)
    app.port = srv.server_address[1]
    srv.daemon_threads = True
    return srv


def serve_in_thread(srv):
    t = threading.Thread(target=srv.serve_forever, name="ctds-c01-http", daemon=True)
    t.start()
    return t
