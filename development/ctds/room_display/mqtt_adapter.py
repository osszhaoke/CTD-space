"""AirGradient read-only MQTT v3.1.1 / v5.0 adapter (pure Python stdlib).

Supports AirGradient ONE 8PSL CHN (FW 8.7.6) and 9PSL local MQTT telemetry:
- Strictly read-only: outbound PUBLISH frames (0x30..0x3F) are forbidden by hard assertion.
- Only `airgradient/readings/<hex-sensor-id>` is accepted; control/config topics are rejected.
- Never disguises MQTT results as HTTP (`http_status` is always None; outcomes are `MQTT_*`).
- Never fabricates device observation timestamps (`provider_observed_at=None`, `RECEIPT_ONLY`).
- Bounded packet size (`MAX_BYTES`), client concurrency, socket timeouts, and QoS 1 deduplication.
"""
from collections import deque
import hmac
import ipaddress
import logging
import re
import socket
import threading
import urllib.parse
import uuid

from . import adapter, catalog

log = logging.getLogger("ctds.c01.mqtt")

MQTT_OK = "MQTT_OK"
MQTT_DUPLICATE = "MQTT_DUPLICATE"
MQTT_TOPIC_REJECTED = "MQTT_TOPIC_REJECTED"
MQTT_SENSOR_MISMATCH = "MQTT_SENSOR_MISMATCH"
MQTT_TOO_LARGE = "MQTT_TOO_LARGE"
MQTT_AUTH_REJECTED = "MQTT_AUTH_REJECTED"
MQTT_DISCONNECTED = "MQTT_DISCONNECTED"

MQTT_SUCCESS_OUTCOMES = frozenset({MQTT_OK, MQTT_DUPLICATE})
READINGS_TOPIC_RE = re.compile(r"^airgradient/readings/([0-9a-fA-F]{6,16})$")
_REDACT_TOPIC_RE = re.compile(r"(airgradient/readings/)[0-9a-fA-F]{6,16}")
SENSOR_ID_RE = re.compile(r"^[0-9a-fA-F]{6,16}$")


def redact_topic(topic):
    if not isinstance(topic, str):
        return "<NON_STR_TOPIC>"
    return _REDACT_TOPIC_RE.sub(r"\1<REDACTED>", topic)


def validate_mqtt_broker_url(url):
    """Validate `mqtt://host[:port]` (no path/query/credentials). Returns `(host, port)`."""
    if not isinstance(url, str):
        raise adapter.AdapterError("CONFIG_INVALID", "mqtt_broker_url must be a string")
    p = urllib.parse.urlsplit(url)
    if p.scheme != "mqtt" or not p.hostname:
        raise adapter.AdapterError("CONFIG_INVALID", "mqtt_broker_url must be mqtt://host[:port]")
    if p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
        raise adapter.AdapterError("CONFIG_INVALID", "mqtt_broker_url must not carry credentials/path/query")
    try:
        port = p.port if p.port is not None else 1883
    except ValueError as exc:
        raise adapter.AdapterError("CONFIG_INVALID", "invalid mqtt_broker_url port") from exc
    if not (1 <= port <= 65535):
        raise adapter.AdapterError("CONFIG_INVALID", "mqtt_broker_url port out of range")
    return p.hostname, port


def is_allowed_mqtt_peer(ip_str):
    """Allow only loopback or RFC1918/private LAN peers on the local MQTT listener."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip.is_multicast or ip.is_unspecified:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


class MqttDeduplicator:
    """Bounded time-window deduplicator for QoS 1 retransmissions and burst duplicates."""

    def __init__(self, *, window_s=15.0, dup_flag_window_s=120.0, max_entries=256):
        self.window_s = float(window_s)
        self.dup_flag_window_s = float(dup_flag_window_s)
        self._entries = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def check_and_record(self, topic_sha, payload_sha, rx_epoch_s, *, dup=False):
        eff_window = max(self.window_s, self.dup_flag_window_s) if dup else self.window_s
        with self._lock:
            if eff_window > 0:
                for t_sha, p_sha, seen_at in reversed(self._entries):
                    age = rx_epoch_s - seen_at
                    # Allow up to 5s minor backward NTP clock slew on duplicate retransmission
                    if t_sha == topic_sha and p_sha == payload_sha and -5.0 <= age <= eff_window:
                        return True
            self._entries.append((topic_sha, payload_sha, rx_epoch_s))
            return False


def record_mqtt_attempt(store, cfg, outcome, *, clock=adapter.utc_now, provenance="LIVE",
                        started_at=None, received_at=None, payload_sha=None, attempt_id=None):
    """Append a non-observation MQTT attempt (`http_status=None` always)."""
    now = clock()
    st = started_at or now
    aid = attempt_id or uuid.uuid4().hex
    store.record_attempt({
        "attempt_id": aid,
        "source_id": cfg["source"]["source_id"],
        "request_started_at": adapter.iso(st),
        "response_received_at": adapter.iso(received_at) if received_at is not None else None,
        "outcome": outcome,
        "http_status": None,
        "payload_sha256": payload_sha,
        "data_provenance": provenance,
    }, ())
    return outcome


def ingest_mqtt_message(store, cfg, *, topic, payload, received_at=None, dup=False,
                        deduplicator=None, provenance="LIVE", clock=adapter.utc_now,
                        attempt_id=None, max_bytes=adapter.MAX_BYTES):
    """Validate, deduplicate, normalize, and persist one MQTT message into Store."""
    rx_dt = received_at or clock()
    aid = attempt_id or uuid.uuid4().hex
    src = cfg["source"]

    m = READINGS_TOPIC_RE.fullmatch(topic or "")
    if not m:
        return record_mqtt_attempt(
            store, cfg, MQTT_TOPIC_REJECTED, clock=clock, provenance=provenance,
            started_at=rx_dt, received_at=rx_dt, attempt_id=aid,
        )

    sensor_id = m.group(1).lower()
    expected = (src.get("mqtt_expected_sensor_id") or "").strip().lower()
    if expected and sensor_id != expected:
        return record_mqtt_attempt(
            store, cfg, MQTT_SENSOR_MISMATCH, clock=clock, provenance=provenance,
            started_at=rx_dt, received_at=rx_dt, attempt_id=aid,
        )

    if not isinstance(payload, (bytes, bytearray)) or len(payload) > max_bytes:
        return record_mqtt_attempt(
            store, cfg, MQTT_TOO_LARGE, clock=clock, provenance=provenance,
            started_at=rx_dt, received_at=rx_dt, attempt_id=aid,
        )

    raw_bytes = bytes(payload)
    topic_sha = adapter.sha256_hex(topic)
    event_identity = f"mqtt:{topic_sha[:12]}:{aid}"
    try:
        observations, payload_sha = adapter.normalize(
            raw_bytes,
            source_id=src["source_id"],
            room_id=cfg["room_id"],
            attempt_id=aid,
            received_at=rx_dt,
            provenance=provenance,
            mapping_version=catalog.MQTT_MAPPING_VERSION,
            event_identity=event_identity,
        )
    except adapter.AdapterError as exc:
        return record_mqtt_attempt(
            store, cfg, exc.code, clock=clock, provenance=provenance,
            started_at=rx_dt, received_at=rx_dt,
            payload_sha=adapter.sha256_hex(raw_bytes), attempt_id=aid,
        )

    if deduplicator is not None:
        rx_epoch = rx_dt.timestamp()
        if deduplicator.check_and_record(topic_sha, payload_sha, rx_epoch, dup=bool(dup)):
            return record_mqtt_attempt(
                store, cfg, MQTT_DUPLICATE, clock=clock, provenance=provenance,
                started_at=rx_dt, received_at=rx_dt, payload_sha=payload_sha, attempt_id=aid,
            )

    store.record_attempt({
        "attempt_id": aid,
        "source_id": src["source_id"],
        "request_started_at": adapter.iso(rx_dt),
        "response_received_at": adapter.iso(rx_dt),
        "outcome": MQTT_OK,
        "http_status": None,
        "payload_sha256": payload_sha,
        "data_provenance": provenance,
    }, observations)
    return MQTT_OK


# --- Low-level MQTT v3.1.1 / v5.0 wire helpers (read-only) -------------------

def _encode_varlen(n):
    out = bytearray()
    while True:
        digit = n % 128
        n //= 128
        if n > 0:
            digit |= 0x80
        out.append(digit)
        if n == 0:
            break
    return bytes(out)


def _encode_utf8(s):
    raw = s.encode("utf-8")
    return len(raw).to_bytes(2, "big") + raw


def _read_utf8(buf, pos):
    if pos + 2 > len(buf):
        raise adapter.AdapterError("INVALID_PAYLOAD", "truncated MQTT string length")
    ln = (buf[pos] << 8) | buf[pos + 1]
    pos += 2
    if pos + ln > len(buf):
        raise adapter.AdapterError("INVALID_PAYLOAD", "truncated MQTT string")
    try:
        s = buf[pos:pos + ln].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise adapter.AdapterError("INVALID_PAYLOAD", "invalid UTF-8 in MQTT string") from exc
    if "\x00" in s:
        raise adapter.AdapterError("INVALID_PAYLOAD", "NUL character in MQTT string")
    return s, pos + ln


def _read_varint_from_buf(buf, pos):
    multiplier = 1
    value = 0
    for _ in range(4):
        if pos >= len(buf):
            raise adapter.AdapterError("INVALID_PAYLOAD", "truncated MQTT varint")
        b = buf[pos]
        pos += 1
        value += (b & 0x7F) * multiplier
        if (b & 0x80) == 0:
            return value, pos
        multiplier *= 128
    raise adapter.AdapterError("INVALID_PAYLOAD", "malformed MQTT varint")


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if len(buf) == 0:
                raise EOFError("clean EOF")
            raise ConnectionError("truncated MQTT frame")
        buf.extend(chunk)
    return bytes(buf)


def _read_packet(sock, max_bytes=adapter.MAX_BYTES, *, expected_ptype=None):
    first_b = _recv_exact(sock, 1)
    first = first_b[0]
    if expected_ptype is not None and (first >> 4) != expected_ptype:
        return first, b""
    multiplier = 1
    rem_len = 0
    for _ in range(4):
        b = _recv_exact(sock, 1)[0]
        rem_len += (b & 0x7F) * multiplier
        if (b & 0x80) == 0:
            break
        multiplier *= 128
    else:
        raise adapter.AdapterError("INVALID_PAYLOAD", "malformed MQTT remaining length")
    if rem_len > max_bytes:
        raise adapter.AdapterError(MQTT_TOO_LARGE, "MQTT packet exceeds max_bytes")
    body = _recv_exact(sock, rem_len) if rem_len > 0 else b""
    return first, body


def _send_readonly_packet(sock, packet):
    """Hard transport-level assertion: CTDS never transmits an outbound PUBLISH frame."""
    if not packet or (packet[0] & 0xF0) == 0x30:
        raise AssertionError("READ_ONLY_VIOLATION: outbound MQTT PUBLISH is forbidden")
    sock.sendall(packet)


def _parse_connect(body):
    proto_name, pos = _read_utf8(body, 0)
    if pos + 4 > len(body):
        raise adapter.AdapterError("INVALID_PAYLOAD", "truncated CONNECT header")
    proto_level = body[pos]
    flags = body[pos + 1]
    keep_alive = (body[pos + 2] << 8) | body[pos + 3]
    pos += 4
    if (proto_name, proto_level) not in (("MQTT", 4), ("MQTT", 5), ("MQIsdp", 3)):
        raise adapter.AdapterError("INVALID_PAYLOAD", "unsupported MQTT protocol")
    if proto_level == 5:
        prop_len, pos = _read_varint_from_buf(body, pos)
        pos += prop_len
        if pos > len(body):
            raise adapter.AdapterError("INVALID_PAYLOAD", "truncated MQTT v5 properties")
    _, pos = _read_utf8(body, pos)  # client_id (intentionally discarded)
    if flags & 0x04:  # will flag
        if proto_level == 5:
            wprop_len, pos = _read_varint_from_buf(body, pos)
            pos += wprop_len
        _, pos = _read_utf8(body, pos)  # will topic
        if pos + 2 > len(body):
            raise adapter.AdapterError("INVALID_PAYLOAD", "truncated will payload length")
        wlen = (body[pos] << 8) | body[pos + 1]
        pos += 2 + wlen
    username = None
    password = None
    if flags & 0x80:
        username, pos = _read_utf8(body, pos)
    if flags & 0x40:
        if pos + 2 > len(body):
            raise adapter.AdapterError("INVALID_PAYLOAD", "truncated password length")
        plen = (body[pos] << 8) | body[pos + 1]
        pos += 2
        if pos + plen > len(body):
            raise adapter.AdapterError("INVALID_PAYLOAD", "truncated password")
        password = body[pos:pos + plen]
        pos += plen
    return {
        "protocol_level": proto_level,
        "keep_alive_s": keep_alive,
        "username": username,
        "password": password,
    }


def _parse_publish(first, body, protocol_level=4):
    dup = bool(first & 0x08)
    qos = (first >> 1) & 0x03
    if qos == 3:
        raise adapter.AdapterError("INVALID_PAYLOAD", "invalid MQTT QoS 3")
    topic, pos = _read_utf8(body, 0)
    pkt_id = None
    if qos > 0:
        if pos + 2 > len(body):
            raise adapter.AdapterError("INVALID_PAYLOAD", "truncated MQTT packet identifier")
        pkt_id = body[pos:pos + 2]
        pos += 2
    if protocol_level == 5:
        prop_len, pos = _read_varint_from_buf(body, pos)
        pos += prop_len
        if pos > len(body):
            raise adapter.AdapterError("INVALID_PAYLOAD", "truncated PUBLISH v5 properties")
    return {
        "dup": dup,
        "qos": qos,
        "packet_id": pkt_id,
        "topic": topic,
        "payload": body[pos:],
    }


def _verify_credentials(cfg_src, conn_info):
    exp_user = cfg_src.get("mqtt_username")
    exp_pass = cfg_src.get("mqtt_password")
    if exp_user is None and exp_pass is None:
        return True
    got_user = conn_info.get("username")
    got_pass = conn_info.get("password")
    if exp_user is not None:
        if not isinstance(got_user, str) or not hmac.compare_digest(got_user.encode("utf-8"), exp_user.encode("utf-8")):
            return False
    if exp_pass is not None:
        raw_exp = exp_pass.encode("utf-8") if isinstance(exp_pass, str) else bytes(exp_pass)
        if got_pass is None or not hmac.compare_digest(bytes(got_pass), raw_exp):
            return False
    return True


class MqttReadOnlyListener:
    """Embedded LAN/loopback read-only MQTT v3.1.1/v5.0 ingestion listener."""

    def __init__(self, store, cfg, *, deduplicator=None, provenance="LIVE",
                 clock=adapter.utc_now, on_outcome=None, socket_timeout_s=5.0, max_clients=8):
        self.store = store
        self.cfg = cfg
        self.deduplicator = deduplicator if deduplicator is not None else MqttDeduplicator(
            window_s=cfg["source"].get("mqtt_dedup_window_s", 15),
        )
        self.provenance = provenance
        self.clock = clock
        self.on_outcome = on_outcome
        self.socket_timeout_s = float(socket_timeout_s)
        self._sem = threading.BoundedSemaphore(max_clients)
        self._stop = threading.Event()
        self._sock = None
        self._thread = None
        self._clients_lock = threading.Lock()
        self._clients = set()
        self.bound_address = None

    def _emit(self, outcome, rx_dt=None):
        if self.on_outcome is not None:
            try:
                self.on_outcome(outcome, rx_dt or self.clock())
            except Exception:
                log.debug("on_outcome callback failed", exc_info=True)

    def start(self):
        src = self.cfg["source"]
        host = src.get("mqtt_listen_host") or "0.0.0.0"
        port = int(src.get("mqtt_listen_port", 1883))
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        srv = socket.socket(family, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
            srv.listen(8)
            srv.settimeout(0.5)
        except BaseException:
            srv.close()
            raise
        self._sock = srv
        self.bound_address = srv.getsockname()[:2]
        self._thread = threading.Thread(target=self._accept_loop, name="ctds-c01-mqtt-listen", daemon=True)
        self._thread.start()
        return self.bound_address

    def stop(self):
        self._stop.set()
        srv = self._sock
        self._sock = None
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        with self._clients_lock:
            clients = list(self._clients)
        for cs in clients:
            try:
                cs.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                cs.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _accept_loop(self):
        while not self._stop.is_set():
            srv = self._sock
            if srv is None:
                break
            try:
                conn, addr = srv.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            peer_ip = addr[0]
            if not is_allowed_mqtt_peer(peer_ip) or not self._sem.acquire(blocking=False):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._clients_lock:
                self._clients.add(conn)
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            t.start()

    def _handle_client(self, conn):
        proto_level = 4
        try:
            conn.settimeout(self.socket_timeout_s)
            first, body = _read_packet(conn, max_bytes=adapter.MAX_BYTES, expected_ptype=1)
            if (first >> 4) != 1:
                return
            info = _parse_connect(body)
            proto_level = info["protocol_level"]
            if not _verify_credentials(self.cfg["source"], info):
                connack = b"\x20\x03\x00\x86\x00" if proto_level == 5 else b"\x20\x02\x00\x04"
                try:
                    _send_readonly_packet(conn, connack)
                except OSError:
                    pass
                rx_dt = self.clock()
                record_mqtt_attempt(
                    self.store, self.cfg, MQTT_AUTH_REJECTED, clock=self.clock,
                    provenance=self.provenance, started_at=rx_dt, received_at=rx_dt,
                )
                self._emit(MQTT_AUTH_REJECTED, rx_dt)
                return

            connack = b"\x20\x03\x00\x00\x00" if proto_level == 5 else b"\x20\x02\x00\x00"
            _send_readonly_packet(conn, connack)
            idle_timeout = min(max(float(info["keep_alive_s"] or 60) * 1.5, self.socket_timeout_s), 180.0)
            conn.settimeout(idle_timeout)

            while not self._stop.is_set():
                first, body = _read_packet(conn, max_bytes=adapter.MAX_BYTES)
                ptype = first >> 4
                if ptype == 3:  # PUBLISH
                    rx_dt = self.clock()
                    pub = _parse_publish(first, body, protocol_level=proto_level)
                    outcome = ingest_mqtt_message(
                        self.store,
                        self.cfg,
                        topic=pub["topic"],
                        payload=pub["payload"],
                        received_at=rx_dt,
                        dup=pub["dup"],
                        deduplicator=self.deduplicator,
                        provenance=self.provenance,
                        clock=self.clock,
                    )
                    if pub["qos"] == 1 and pub["packet_id"] is not None:
                        ack = (b"\x40\x04" + pub["packet_id"] + b"\x00\x00") if proto_level == 5 else (b"\x40\x02" + pub["packet_id"])
                        _send_readonly_packet(conn, ack)
                    elif pub["qos"] == 2 and pub["packet_id"] is not None:
                        rec = (b"\x50\x04" + pub["packet_id"] + b"\x00\x00") if proto_level == 5 else (b"\x50\x02" + pub["packet_id"])
                        _send_readonly_packet(conn, rec)
                    self._emit(outcome, rx_dt)
                elif ptype == 6:  # PUBREL -> PUBCOMP
                    if len(body) >= 2:
                        comp = (b"\x70\x04" + body[:2] + b"\x00\x00") if proto_level == 5 else (b"\x70\x02" + body[:2])
                        _send_readonly_packet(conn, comp)
                elif ptype == 8:  # SUBSCRIBE -> refuse all subscriptions (read-only sink, not a broker)
                    if len(body) >= 2:
                        pkt_id = body[:2]
                        suback = (b"\x90\x04" + pkt_id + b"\x00\x80") if proto_level == 5 else (b"\x90\x03" + pkt_id + b"\x80")
                        _send_readonly_packet(conn, suback)
                elif ptype == 12:  # PINGREQ -> PINGRESP
                    _send_readonly_packet(conn, b"\xD0\x00")
                elif ptype == 14:  # DISCONNECT
                    break
                else:
                    break
        except adapter.AdapterError as exc:
            rx_dt = self.clock()
            record_mqtt_attempt(
                self.store, self.cfg, exc.code, clock=self.clock,
                provenance=self.provenance, started_at=rx_dt, received_at=rx_dt,
            )
            self._emit(exc.code, rx_dt)
        except (EOFError, ConnectionError, socket.timeout, TimeoutError, OSError):
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass
            self._sem.release()


def _build_connect_packet(client_id, *, keep_alive_s=60, username=None, password=None):
    flags = 0x02  # Clean Session
    payload = bytearray(_encode_utf8(client_id))
    if username is not None:
        flags |= 0x80
        payload.extend(_encode_utf8(username))
    if password is not None:
        flags |= 0x40
        raw_pw = password.encode("utf-8") if isinstance(password, str) else bytes(password)
        payload.extend(len(raw_pw).to_bytes(2, "big") + raw_pw)
    var_hdr = _encode_utf8("MQTT") + bytes([0x04, flags]) + int(keep_alive_s).to_bytes(2, "big")
    rem = var_hdr + bytes(payload)
    return b"\x10" + _encode_varlen(len(rem)) + rem


def _build_subscribe_packet(packet_id, topic_filter, qos=1):
    body = int(packet_id).to_bytes(2, "big") + _encode_utf8(topic_filter) + bytes([qos & 0x01])
    return b"\x82" + _encode_varlen(len(body)) + body


class MqttReadOnlySubscriber:
    """Read-only MQTT v3.1.1 subscriber client for an external local broker (`mqtt://host:port`)."""

    def __init__(self, store, cfg, *, deduplicator=None, provenance="LIVE",
                 clock=adapter.utc_now, on_outcome=None, connect_timeout_s=5.0, keep_alive_s=30):
        self.store = store
        self.cfg = cfg
        self.deduplicator = deduplicator if deduplicator is not None else MqttDeduplicator(
            window_s=cfg["source"].get("mqtt_dedup_window_s", 15),
        )
        self.provenance = provenance
        self.clock = clock
        self.on_outcome = on_outcome
        self.connect_timeout_s = float(connect_timeout_s)
        self.keep_alive_s = int(keep_alive_s)
        self._stop = threading.Event()
        self._sock_lock = threading.Lock()
        self._sock = None
        self._thread = None

    def _emit(self, outcome, rx_dt=None):
        if self.on_outcome is not None:
            try:
                self.on_outcome(outcome, rx_dt or self.clock())
            except Exception:
                log.debug("on_outcome callback failed", exc_info=True)

    def start(self):
        host, port = validate_mqtt_broker_url(self.cfg["source"]["mqtt_broker_url"])
        self._thread = threading.Thread(target=self._run_loop, args=(host, port),
                                        name="ctds-c01-mqtt-sub", daemon=True)
        self._thread.start()
        return (host, port)

    def stop(self):
        self._stop.set()
        with self._sock_lock:
            s = self._sock
            self._sock = None
        if s is not None:
            try:
                _send_readonly_packet(s, b"\xE0\x00")
            except OSError:
                pass
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _run_loop(self, host, port):
        src = self.cfg["source"]
        expected = (src.get("mqtt_expected_sensor_id") or "").strip().lower()
        topic_filter = f"airgradient/readings/{expected}" if expected else "airgradient/readings/+"
        backoff = 1.0
        max_backoff = float(self.cfg.get("max_backoff_s", 300))

        while not self._stop.is_set():
            sock = None
            connected_ok = False
            try:
                sock = socket.create_connection((host, port), timeout=self.connect_timeout_s)
                with self._sock_lock:
                    if self._stop.is_set():
                        sock.close()
                        return
                    self._sock = sock
                client_id = f"ctds-c01-ro-{uuid.uuid4().hex[:8]}"
                pkt = _build_connect_packet(
                    client_id,
                    keep_alive_s=self.keep_alive_s,
                    username=src.get("mqtt_username"),
                    password=src.get("mqtt_password"),
                )
                _send_readonly_packet(sock, pkt)
                first, body = _read_packet(sock, max_bytes=adapter.MAX_BYTES)
                if (first >> 4) != 2 or len(body) < 2:
                    raise adapter.AdapterError("INVALID_PAYLOAD", "invalid CONNACK")
                rc = body[1]
                if rc in (4, 5):
                    rx_dt = self.clock()
                    record_mqtt_attempt(
                        self.store, self.cfg, MQTT_AUTH_REJECTED, clock=self.clock,
                        provenance=self.provenance, started_at=rx_dt, received_at=rx_dt,
                    )
                    self._emit(MQTT_AUTH_REJECTED, rx_dt)
                    return
                if rc != 0:
                    raise ConnectionError(f"CONNACK rejected rc={rc}")

                _send_readonly_packet(sock, _build_subscribe_packet(1, topic_filter, qos=1))
                first, body = _read_packet(sock, max_bytes=adapter.MAX_BYTES)
                if (first >> 4) != 9 or len(body) < 3 or body[2] == 0x80:
                    raise ConnectionError("SUBACK rejected")

                connected_ok = True
                backoff = 1.0
                awaiting_pingresp = False
                sock.settimeout(max(0.25, self.keep_alive_s / 2.0))

                while not self._stop.is_set():
                    try:
                        first, body = _read_packet(sock, max_bytes=adapter.MAX_BYTES)
                    except (socket.timeout, TimeoutError):
                        if awaiting_pingresp:
                            raise TimeoutError("MQTT broker PINGRESP timeout")
                        _send_readonly_packet(sock, b"\xC0\x00")  # PINGREQ
                        awaiting_pingresp = True
                        continue
                    awaiting_pingresp = False
                    ptype = first >> 4
                    if ptype == 3:
                        rx_dt = self.clock()
                        pub = _parse_publish(first, body, protocol_level=4)
                        outcome = ingest_mqtt_message(
                            self.store,
                            self.cfg,
                            topic=pub["topic"],
                            payload=pub["payload"],
                            received_at=rx_dt,
                            dup=pub["dup"],
                            deduplicator=self.deduplicator,
                            provenance=self.provenance,
                            clock=self.clock,
                        )
                        if pub["qos"] == 1 and pub["packet_id"] is not None:
                            _send_readonly_packet(sock, b"\x40\x02" + pub["packet_id"])
                        self._emit(outcome, rx_dt)
                    elif ptype == 13:  # PINGRESP
                        continue
                    elif ptype == 14:
                        break
            except adapter.AdapterError as exc:
                rx_dt = self.clock()
                record_mqtt_attempt(
                    self.store, self.cfg, exc.code, clock=self.clock,
                    provenance=self.provenance, started_at=rx_dt, received_at=rx_dt,
                )
                self._emit(exc.code, rx_dt)
            except (EOFError, ConnectionError, socket.timeout, TimeoutError, OSError):
                if not self._stop.is_set():
                    rx_dt = self.clock()
                    record_mqtt_attempt(
                        self.store, self.cfg, MQTT_DISCONNECTED, clock=self.clock,
                        provenance=self.provenance, started_at=rx_dt, received_at=None,
                    )
                    self._emit(MQTT_DISCONNECTED, rx_dt)
            finally:
                with self._sock_lock:
                    if self._sock is sock:
                        self._sock = None
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

            if self._stop.is_set():
                break
            wait_s = 1.0 if connected_ok else backoff
            if self._stop.wait(wait_s):
                break
            backoff = min(backoff * 2.0, max_backoff)
