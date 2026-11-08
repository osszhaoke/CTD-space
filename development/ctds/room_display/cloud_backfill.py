"""Read-only AirGradient Cloud API v1 historical backfill & gap auto-healer (stdlib only).

Complements real-time local MQTT / HTTP ingestion without replacing it:
  1. Startup resumable full backfill:
     - Recent 48 hours via `/public/api/v1/locations/{locationId}/measures/raw` in 3-hour windows
       (staying well below the 200-record per-call cap).
     - Earlier full archive down to `cloud_earliest_date` (default `2022-01-01T00:00:00Z`) via
       `/public/api/v1/locations/{locationId}/measures/past` in 9-day windows, checkpointed in SQLite `meta`.
  2. Automatic gap healing:
     - Whenever the host Mac wakes from sleep/shutdown or local MQTT recovers from a gap > 270s,
       fetches only the missed `[last_synced, now]` interval and skips points within ±45s of
       already-recorded local MQTT/HTTP observations so real-time local data stays authoritative.

Strict safety & privacy invariants:
  - Read-only HTTPS `GET` only (`https://api.airgradient.com`), no redirects, bounded response size.
  - `provider_observed_at` stores the cloud record's UTC measurement timestamp; `gateway_received_at`
    stores CTDS's actual receipt timestamp (`time_basis = "PROVIDER_OBSERVED"`).
  - Never logs or persists `cloud_api_token`, `cloud_location_id`, `serialno`, `locationName`, or GPS fields.
  - Cloud API `401`/`403` or transient network errors back off independently and NEVER pause local MQTT.
"""
import bisect
import datetime as dt
import hashlib
import ipaddress
import logging
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import adapter, catalog

log = logging.getLogger("ctds.c01.cloud_backfill")

OFFICIAL_CLOUD_BASE_URL = "https://api.airgradient.com"
MAX_CLOUD_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap per API page
RAW_WINDOW = dt.timedelta(hours=3)
RAW_HORIZON = dt.timedelta(hours=48)
PAST_WINDOW = dt.timedelta(days=9)
LOCAL_TWIN_TOLERANCE_S = 45.0
GAP_HEAL_THRESHOLD_S = 270.0
DEFAULT_EARLIEST_UTC = "2022-01-01T00:00:00Z"
DEFAULT_GAP_CHECK_S = 900

META_FULL_DONE = "ag_cloud_full_backfill_done"
META_OLDEST_SCANNED = "ag_cloud_oldest_scanned_utc"
META_LAST_SYNCED = "ag_cloud_last_synced_utc"
META_TOTAL_WRITTEN = "ag_cloud_backfilled_observations"

_TOKEN_SCRUB_RE = re.compile(r"(token=)[^&\s]+", re.IGNORECASE)
_LOC_SCRUB_RE = re.compile(r"(/locations/)\d+", re.IGNORECASE)


def scrub_text(text):
    """Strip token query params and numeric location IDs from any diagnostic string."""
    s = _TOKEN_SCRUB_RE.sub(r"\1***", str(text or ""))
    return _LOC_SCRUB_RE.sub(r"\1<redacted>", s)


def is_allowed_cloud_base_url(url):
    if not isinstance(url, str) or not url:
        return False
    p = urllib.parse.urlsplit(url)
    if p.username or p.password or p.query or p.fragment or not p.hostname:
        return False
    if p.scheme == "https" and p.hostname.lower() == "api.airgradient.com":
        return True
    if os.environ.get("CTDS_TEST_SANDBOX") == "1" and p.scheme in ("http", "https"):
        host = p.hostname.lower()
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False
    return False


def parse_cloud_timestamp(value):
    """Parse ISO 8601 UTC timestamp string (e.g. '2022-03-28T12:07:40Z' or '2022-03-28') into aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        raise adapter.AdapterError("INVALID_TIMESTAMP", "missing timestamp")
    s = value.strip()
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        s = s + "T00:00:00+00:00"
    elif s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    # Support basic ISO format YYYYMMDDTHHMMSS+00:00 if ever returned
    if len(s) == 21 and s[8] == "T" and s[15] in ("+", "-"):
        s = f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[9:11]}:{s[11:13]}:{s[13:15]}{s[15:]}"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError as exc:
        raise adapter.AdapterError("INVALID_TIMESTAMP", "unparseable timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def fmt_basic_utc(t):
    """Format aware UTC datetime as basic ISO 8601 required by AirGradient v1 query params (YYYYMMDDTHHMMSSZ)."""
    return t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_iso_epoch(iso_str):
    return parse_cloud_timestamp(iso_str).timestamp()


def _has_nearby_timestamp(sorted_epochs, target_epoch, tolerance_s):
    if not sorted_epochs:
        return False
    idx = bisect.bisect_left(sorted_epochs, target_epoch)
    if idx < len(sorted_epochs) and abs(sorted_epochs[idx] - target_epoch) <= tolerance_s:
        return True
    if idx > 0 and abs(sorted_epochs[idx - 1] - target_epoch) <= tolerance_s:
        return True
    return False


class AirGradientCloudClient:
    """Minimal stdlib read-only client for AirGradient Public API v1 historical measures."""

    def __init__(self, *, location_id, api_token, base_url=OFFICIAL_CLOUD_BASE_URL, timeout_s=15):
        if not is_allowed_cloud_base_url(base_url):
            raise adapter.AdapterError("INVALID_URL", "disallowed cloud_api_base_url")
        loc_str = str(location_id).strip()
        if not loc_str.isdigit():
            raise adapter.AdapterError("INVALID_LOCATION", "location_id must be numeric")
        if not isinstance(api_token, str) or not api_token.strip():
            raise adapter.AdapterError("INVALID_TOKEN", "api_token required")
        self.base_url = base_url.rstrip("/")
        self.location_id = loc_str
        self.location_hash = hashlib.sha256(loc_str.encode("utf-8")).hexdigest()[:12]
        self.api_token = api_token.strip()
        self.timeout_s = timeout_s
        scheme = urllib.parse.urlsplit(self.base_url).scheme.lower()
        handlers = [adapter._NoRedirect]
        if scheme == "https":
            handlers.append(urllib.request.HTTPSHandler(context=adapter.get_ssl_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def _get_json_list(self, endpoint_suffix, params):
        q = urllib.parse.urlencode({"token": self.api_token, **params})
        url = f"{self.base_url}/public/api/v1/locations/{self.location_id}/measures/{endpoint_suffix}?{q}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "CTDS-C01-Backfill/0.2"},
        )
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                status = resp.status
                raw = resp.read(MAX_CLOUD_BYTES + 1)
                if len(raw) > MAX_CLOUD_BYTES:
                    raise adapter.AdapterError("CLOUD_TOO_LARGE", "response exceeded MAX_CLOUD_BYTES")
                if status != 200:
                    raise adapter.AdapterError(f"CLOUD_HTTP_{status}", f"status={status}")
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            if code in (401, 403):
                raise adapter.AdapterError("CLOUD_AUTH_REJECTED", f"HTTP {code}") from None
            if code == 404:
                return [], b"[]", 200
            if code == 429:
                raise adapter.AdapterError("CLOUD_RATE_LIMITED", "HTTP 429") from None
            raise adapter.AdapterError(f"CLOUD_HTTP_{code}", f"HTTP {code}") from None
        except (socket.timeout, TimeoutError) as exc:
            raise adapter.AdapterError("CLOUD_TIMEOUT", "request timed out") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise adapter.AdapterError("CLOUD_TIMEOUT", "request timed out") from exc
            raise adapter.AdapterError("CLOUD_NO_CONNECTION", scrub_text(exc.reason)) from exc
        except OSError as exc:
            raise adapter.AdapterError("CLOUD_NO_CONNECTION", scrub_text(exc)) from exc

        doc = adapter.strict_json(raw)
        if type(doc) is not list:
            raise adapter.AdapterError("INVALID_PAYLOAD", "expected JSON array of measures")
        return doc, raw, 200

    def fetch_raw_window(self, start_dt, end_dt):
        return self._get_json_list("raw", {"from": fmt_basic_utc(start_dt), "to": fmt_basic_utc(end_dt)})

    def fetch_past_window(self, start_dt, end_dt):
        return self._get_json_list("past", {"from": fmt_basic_utc(start_dt), "to": fmt_basic_utc(end_dt)})

    def check_connectivity(self):
        """Read-only verification probe against `/measures/current` (returns outcome dict, zero secrets)."""
        q = urllib.parse.urlencode({"token": self.api_token})
        url = f"{self.base_url}/public/api/v1/locations/{self.location_id}/measures/current?{q}"
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "CTDS-C01-Backfill/0.2"})
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read(MAX_CLOUD_BYTES)
                doc = adapter.strict_json(raw)
                return {"ok": resp.status == 200 and isinstance(doc, (dict, list)), "outcome": "OK", "http_status": resp.status}
        except urllib.error.HTTPError as e:
            code = e.code
            e.close()
            outcome = "CLOUD_AUTH_REJECTED" if code in (401, 403) else ("CLOUD_RATE_LIMITED" if code == 429 else f"CLOUD_HTTP_{code}")
            return {"ok": False, "outcome": outcome, "http_status": code}
        except (socket.timeout, TimeoutError):
            return {"ok": False, "outcome": "CLOUD_TIMEOUT", "http_status": None}
        except (urllib.error.URLError, OSError):
            return {"ok": False, "outcome": "CLOUD_NO_CONNECTION", "http_status": None}


def ingest_cloud_window(store, cfg, client, *, mode, start_dt, end_dt, clock=adapter.utc_now, provenance="LIVE"):
    """Fetch one `[start_dt, end_dt]` window (`mode` in ('raw', 'past')) and write deduplicated observations."""
    req_started = clock()
    attempt_id = str(uuid.uuid4())
    src_id = cfg["source"]["source_id"]
    room_id = cfg["room_id"]

    try:
        if mode == "raw":
            items, raw_bytes, http_status = client.fetch_raw_window(start_dt, end_dt)
        else:
            items, raw_bytes, http_status = client.fetch_past_window(start_dt, end_dt)
    except adapter.AdapterError as err:
        resp_at = clock()
        http_code = None
        if err.code.startswith("CLOUD_HTTP_"):
            try:
                http_code = int(err.code.rsplit("_", 1)[1])
            except ValueError:
                http_code = None
        elif err.code == "CLOUD_AUTH_REJECTED":
            http_code = 401
        elif err.code == "CLOUD_RATE_LIMITED":
            http_code = 429
        store.record_attempt({
            "attempt_id": attempt_id,
            "source_id": src_id,
            "request_started_at": adapter.iso(req_started),
            "response_received_at": adapter.iso(resp_at),
            "outcome": err.code,
            "http_status": http_code,
            "payload_sha256": None,
            "data_provenance": provenance,
        })
        raise

    received_at = clock()
    # Load existing local MQTT/HTTP timestamps in [start - 60s, end + 60s] so we don't insert redundant twins
    pad = dt.timedelta(seconds=LOCAL_TWIN_TOLERANCE_S + 15)
    local_isos = store.local_receipt_timestamps_in_range(
        room_id, "pm2_5", adapter.iso(start_dt - pad), adapter.iso(end_dt + pad), provenance=provenance
    )
    local_epochs = [_parse_iso_epoch(t) for t in local_isos]

    all_observations = []
    skipped_local_twins = 0
    for item in items:
        if type(item) is not dict:
            continue
        ts_raw = item.get("timestamp") or item.get("date")
        if not ts_raw:
            continue
        try:
            obs_dt = parse_cloud_timestamp(ts_raw)
        except adapter.AdapterError:
            continue
        if not (start_dt <= obs_dt <= end_dt + dt.timedelta(seconds=1)):
            continue
        if _has_nearby_timestamp(local_epochs, obs_dt.timestamp(), LOCAL_TWIN_TOLERANCE_S):
            skipped_local_twins += 1
            continue
        obs_iso = adapter.iso(obs_dt)
        ident = f"cloud:{client.location_hash}:{obs_iso}"
        obs_rows, _ = adapter.normalize(
            item,
            source_id=src_id,
            room_id=room_id,
            attempt_id=attempt_id,
            received_at=received_at,
            provenance=provenance,
            mapping_version=catalog.CLOUD_MAPPING_VERSION,
            event_identity=ident,
            provider_observed_at=obs_dt,
        )
        all_observations.extend(obs_rows)

    written = store.record_attempt({
        "attempt_id": attempt_id,
        "source_id": src_id,
        "request_started_at": adapter.iso(req_started),
        "response_received_at": adapter.iso(received_at),
        "outcome": "CLOUD_BACKFILL_OK",
        "http_status": http_status,
        "payload_sha256": adapter.sha256_hex(raw_bytes),
        "data_provenance": provenance,
    }, all_observations)

    if written > 0:
        prev_total = int(store.get_meta(META_TOTAL_WRITTEN, "0") or 0)
        store.set_meta(META_TOTAL_WRITTEN, str(prev_total + written))

    return {
        "records_fetched": len(items),
        "skipped_local_twins": skipped_local_twins,
        "observations_written": written,
    }


def sync_once(store, cfg, *, client=None, clock=adapter.utc_now, stop_event=None, inter_window_pause_s=0.15, max_past_windows=None):
    """Execute one resumable backfill + recent gap-heal pass. Safe to call from CLI or background worker."""
    src = cfg["source"]
    if not src.get("cloud_backfill_enabled"):
        return {"enabled": False, "state": "DISABLED", "observations_written": 0, "windows_scanned": 0}

    provenance = "LIVE" if src.get("mode") == "LIVE" else "SYNTHETIC"
    if client is None:
        client = AirGradientCloudClient(
            location_id=src["cloud_location_id"],
            api_token=src["cloud_api_token"],
            base_url=src.get("cloud_api_base_url") or OFFICIAL_CLOUD_BASE_URL,
            timeout_s=max(10, int(cfg.get("timeout_s", 5)) * 2),
        )

    now = clock()
    earliest_dt = parse_cloud_timestamp(src.get("cloud_earliest_date") or DEFAULT_EARLIEST_UTC)
    total_written = 0
    windows_scanned = 0

    def _stopped():
        return stop_event is not None and stop_event.is_set()

    def _pause():
        if inter_window_pause_s > 0:
            if stop_event is not None:
                stop_event.wait(inter_window_pause_s)
            else:
                threading.Event().wait(inter_window_pause_s)

    # 1. Heal recent gap (from META_LAST_SYNCED or now - 48h up to now)
    last_synced_raw = store.get_meta(META_LAST_SYNCED)
    raw_horizon_start = max(earliest_dt, now - RAW_HORIZON)
    if last_synced_raw:
        try:
            gap_start = max(earliest_dt, parse_cloud_timestamp(last_synced_raw) - dt.timedelta(minutes=3))
        except adapter.AdapterError:
            gap_start = raw_horizon_start
    else:
        gap_start = raw_horizon_start

    # If host was powered off > 48h since last_synced, fill the older part via `/measures/past` first
    if gap_start < raw_horizon_start:
        cur = gap_start
        while cur < raw_horizon_start and not _stopped():
            nxt = min(raw_horizon_start, cur + PAST_WINDOW)
            res = ingest_cloud_window(store, cfg, client, mode="past", start_dt=cur, end_dt=nxt, clock=clock, provenance=provenance)
            total_written += res["observations_written"]
            windows_scanned += 1
            store.set_meta(META_LAST_SYNCED, adapter.iso(nxt))
            cur = nxt
            _pause()

    # Fill [max(gap_start, now - 48h), now] in 3h raw windows
    cur_raw = max(gap_start, raw_horizon_start)
    if (now - cur_raw).total_seconds() >= 60:
        while cur_raw < now and not _stopped():
            nxt_raw = min(now, cur_raw + RAW_WINDOW)
            res = ingest_cloud_window(store, cfg, client, mode="raw", start_dt=cur_raw, end_dt=nxt_raw, clock=clock, provenance=provenance)
            total_written += res["observations_written"]
            windows_scanned += 1
            store.set_meta(META_LAST_SYNCED, adapter.iso(nxt_raw))
            cur_raw = nxt_raw
            if cur_raw < now:
                _pause()

    # 2. Resumable backward deep-archive scan (`now - 48h` down to `earliest_dt`)
    full_done = store.get_meta(META_FULL_DONE) == "1"
    if not full_done and not _stopped():
        oldest_raw = store.get_meta(META_OLDEST_SCANNED)
        if oldest_raw:
            try:
                cursor_end = parse_cloud_timestamp(oldest_raw)
            except adapter.AdapterError:
                cursor_end = raw_horizon_start
        else:
            cursor_end = raw_horizon_start
            store.set_meta(META_OLDEST_SCANNED, adapter.iso(cursor_end))

        past_count = 0
        while cursor_end > earliest_dt and not _stopped():
            if max_past_windows is not None and past_count >= max_past_windows:
                break
            win_start = max(earliest_dt, cursor_end - PAST_WINDOW)
            res = ingest_cloud_window(store, cfg, client, mode="past", start_dt=win_start, end_dt=cursor_end, clock=clock, provenance=provenance)
            total_written += res["observations_written"]
            windows_scanned += 1
            past_count += 1
            cursor_end = win_start
            store.set_meta(META_OLDEST_SCANNED, adapter.iso(cursor_end))
            if cursor_end > earliest_dt:
                _pause()

        if cursor_end <= earliest_dt:
            store.set_meta(META_FULL_DONE, "1")
            full_done = True

    return {
        "enabled": True,
        "state": "READY" if full_done else "SYNCING",
        "full_backfill_done": full_done,
        "windows_scanned": windows_scanned,
        "observations_written": total_written,
        "oldest_scanned_at": store.get_meta(META_OLDEST_SCANNED),
        "last_synced_at": store.get_meta(META_LAST_SYNCED),
        "total_backfilled": int(store.get_meta(META_TOTAL_WRITTEN, "0") or 0),
    }


class CloudBackfillManager:
    """Background worker managing startup full backfill and event/periodic gap healing."""

    def __init__(self, store, cfg, *, clock=adapter.utc_now, client=None, inter_window_pause_s=0.2):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self._client = client
        self.inter_window_pause_s = inter_window_pause_s
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        enabled = bool(cfg["source"].get("cloud_backfill_enabled"))
        self._state = "IDLE" if enabled else "DISABLED"
        self._last_error_code = None

    def start(self):
        if not self.cfg["source"].get("cloud_backfill_enabled"):
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_loop, name="ctds-ag-cloud-backfill", daemon=True)
        self._thread.start()

    def trigger_gap_check(self):
        """Wake background worker immediately (e.g. when local MQTT reconnects after an outage)."""
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=3.0)
            self._thread = None

    def status_snapshot(self):
        with self._lock:
            state = self._state
            err = self._last_error_code
        enabled = bool(self.cfg["source"].get("cloud_backfill_enabled"))
        return {
            "enabled": enabled,
            "state": state if enabled else "DISABLED",
            "last_error_code": err,
            "full_backfill_done": self.store.get_meta(META_FULL_DONE) == "1",
            "oldest_scanned_at": self.store.get_meta(META_OLDEST_SCANNED),
            "last_synced_at": self.store.get_meta(META_LAST_SYNCED),
            "total_backfilled": int(self.store.get_meta(META_TOTAL_WRITTEN, "0") or 0),
        }

    def _run_loop(self):
        gap_interval = int(self.cfg["source"].get("cloud_gap_check_interval_s", DEFAULT_GAP_CHECK_S))
        backoff_s = 30
        max_backoff_s = min(300, int(self.cfg.get("max_backoff_s", 300)))

        while not self._stop.is_set():
            self._wake.clear()
            with self._lock:
                self._state = "SYNCING"
            try:
                res = sync_once(
                    self.store,
                    self.cfg,
                    client=self._client,
                    clock=self.clock,
                    stop_event=self._stop,
                    inter_window_pause_s=self.inter_window_pause_s,
                )
                with self._lock:
                    self._state = res["state"]
                    self._last_error_code = None
                backoff_s = 30
                if res["observations_written"] > 0:
                    log.info(
                        "AirGradient cloud backfill synced %d observations across %d windows (full_done=%s)",
                        res["observations_written"], res["windows_scanned"], res["full_backfill_done"],
                    )
                wait_s = gap_interval if res["full_backfill_done"] else 5
            except adapter.AdapterError as err:
                with self._lock:
                    self._last_error_code = err.code
                    self._state = "AUTH_REQUIRED" if err.code == "CLOUD_AUTH_REJECTED" else "OFFLINE_RETRY"
                wait_s = min(max_backoff_s, backoff_s)
                backoff_s = min(max_backoff_s, backoff_s * 2)
                log.warning("AirGradient cloud backfill deferred: %s (retrying in %ds; local collection unaffected)", err.code, wait_s)
            except Exception as exc:  # never crash daemon
                with self._lock:
                    self._last_error_code = type(exc).__name__
                    self._state = "OFFLINE_RETRY"
                wait_s = min(max_backoff_s, backoff_s)
                backoff_s = min(max_backoff_s, backoff_s * 2)
                log.warning("AirGradient cloud backfill unexpected error: %s (retrying in %ds)", type(exc).__name__, wait_s)

            if self._stop.is_set():
                break
            self._wake.wait(timeout=wait_s)
