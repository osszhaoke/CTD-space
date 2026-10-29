"""AirGradient local read-only adapter: one GET /measures/current, safe normalization.

- Only plain GET to the operator-configured base URL; no redirects, no proxies,
  bounded size and timeout. No scanning/discovery.
- Response has no observation timestamp (per pinned doc) -> RECEIPT_ONLY.
- Identity/network fields (serialno, wifi, ...) are dropped; only a payload hash is kept.
"""
import datetime as dt
import hashlib
import json
import math
import socket
import urllib.error
import urllib.parse
import urllib.request

from . import catalog

PATH = "/measures/current"
MAX_BYTES = 65536
PROVENANCES = ("LIVE", "REPLAY", "SYNTHETIC")
NO_CONNECTION = "NO_CONNECTION"
AUTH_REJECTED = "AUTH_REJECTED"


def get_ssl_context():
    """Create an SSL context with fallback to macOS system root certificates.

    Resolves the classic macOS python.org bug where OpenSSL root certificates are uninstalled.
    """
    import os, ssl
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
        return ctx
    except Exception:
        pass
    for path in (
        "/etc/ssl/cert.pem",
        "/private/etc/ssl/cert.pem",
        "/usr/local/etc/openssl/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
    ):
        if os.path.isfile(path):
            try:
                ctx.load_verify_locations(path)
                return ctx
            except Exception:
                pass
    return ctx


class AdapterError(Exception):
    def __init__(self, code, message=""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t):
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise AdapterError("INVALID_PAYLOAD", "duplicate JSON key")
        out[k] = v
    return out


def _constant(_):
    raise AdapterError("INVALID_PAYLOAD", "non-finite JSON constant")


def strict_json(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except AdapterError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise AdapterError("INVALID_PAYLOAD", "invalid JSON") from exc


def validate_base_url(base_url):
    if not isinstance(base_url, str):
        raise AdapterError("CONFIG_INVALID", "base_url must be a string")
    p = urllib.parse.urlsplit(base_url)
    if p.scheme != "http" or not p.hostname:
        raise AdapterError("CONFIG_INVALID", "base_url must be http://host[:port]")
    if p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
        raise AdapterError("CONFIG_INVALID", "base_url must not carry credentials/path/query")
    return f"http://{p.netloc}{PATH}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def fetch(base_url, *, timeout=5.0, max_bytes=MAX_BYTES, clock=utc_now):
    """Single read. Returns dict(outcome, http_status, body, started_at, received_at)."""
    url = validate_base_url(base_url)
    started = clock()
    result = {"outcome": None, "http_status": None, "body": None,
              "request_started_at": started, "response_received_at": None}
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json",
                                                             "User-Agent": "ctds-c01-readonly/0.1"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            result["http_status"] = resp.status
            body = resp.read(max_bytes + 1)
            result["response_received_at"] = clock()
            if len(body) > max_bytes:
                result["outcome"] = "TOO_LARGE"
            else:
                result["outcome"], result["body"] = "OK", body
    except urllib.error.HTTPError as exc:
        result["http_status"] = exc.code
        result["response_received_at"] = clock()
        if exc.code in (401, 403):
            result["outcome"] = "AUTH_REJECTED"
        elif exc.code == 404:
            result["outcome"] = "NOT_FOUND"
        elif 300 <= exc.code < 400:
            result["outcome"] = "REDIRECT_REFUSED"
        elif exc.code == 429:
            result["outcome"] = "RATE_LIMITED"
        else:
            result["outcome"] = "HTTP_ERROR"
        exc.close()
    except (socket.timeout, TimeoutError):
        result["outcome"] = "TIMEOUT"
    except urllib.error.URLError as exc:
        result["outcome"] = "TIMEOUT" if isinstance(exc.reason, (socket.timeout, TimeoutError)) else "NO_CONNECTION"
    except (ConnectionError, OSError):
        result["outcome"] = "NO_CONNECTION"
    return result


def _classify(raw, present, rng):
    if not present:
        return "NOT_PROVIDED", None
    if raw is None:
        return "EXPLICIT_NULL", None
    if type(raw) not in (int, float) or not math.isfinite(raw):
        return "INVALID", None
    if not (rng[0] <= raw <= rng[1]):
        return "INVALID", None
    return "OBSERVED", float(raw)


def normalize(body, *, source_id, room_id, attempt_id, received_at, provenance):
    """Map one response body to allowlisted observations. Raises AdapterError on bad payload."""
    if provenance not in PROVENANCES:
        raise AdapterError("INVALID_PROVENANCE")
    doc = strict_json(body)
    if type(doc) is not dict:
        raise AdapterError("INVALID_PAYLOAD", "top-level object required")
    observations = []
    for metric_id, m in catalog.METRICS.items():
        status, value = _classify(doc.get(m["field"]), m["field"] in doc, m["range"])
        observations.append({
            "event_id": sha256_hex(f"{source_id}|{attempt_id}|{metric_id}"),
            "source_id": source_id,
            "room_id": room_id,
            "metric_id": metric_id,
            "value_status": status,
            "value": value,
            "unit": m["unit"],
            "provider_observed_at": None,
            "gateway_received_at": iso(received_at),
            "time_basis": "RECEIPT_ONLY",
            "mapping_version": catalog.MAPPING_VERSION,
            "data_provenance": provenance,
            "source_event_identity": attempt_id,
        })
    return observations, sha256_hex(body)
