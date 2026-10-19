"""Polling collector: timeout, bounded retry/backoff, auth pause, clean stop."""
import logging
import threading
import uuid

from . import adapter, catalog, synthetic

log = logging.getLogger("ctds.c01.collector")
TRANSIENT = {"TIMEOUT", "NO_CONNECTION", "HTTP_ERROR"}


def register(store, cfg, clock=adapter.utc_now):
    src = cfg["source"]
    store.register_source(source_id=src["source_id"], provider="airgradient", device_class="air_quality_monitor",
                          room_id=cfg["room_id"], binding_revision=src["binding_revision"],
                          configuration_version=catalog.MAPPING_VERSION, status="ACTIVE",
                          capability_version=catalog.DOC_REF, updated_at=adapter.iso(clock()))


def ingest_once(store, cfg, fetcher, *, provenance, clock=adapter.utc_now):
    """One read -> one attempt row (+ observations). Returns outcome string."""
    src = cfg["source"]
    attempt_id = uuid.uuid4().hex
    res = fetcher(src["base_url"], timeout=cfg["timeout_s"])
    outcome, observations, payload_sha = res["outcome"], [], None
    if outcome == "OK":
        try:
            observations, payload_sha = adapter.normalize(
                res["body"], source_id=src["source_id"], room_id=cfg["room_id"], attempt_id=attempt_id,
                received_at=res["response_received_at"], provenance=provenance)
        except adapter.AdapterError as exc:
            outcome, payload_sha = exc.code, adapter.sha256_hex(res["body"])
    store.record_attempt({
        "attempt_id": attempt_id, "source_id": src["source_id"],
        "request_started_at": adapter.iso(res["request_started_at"]),
        "response_received_at": adapter.iso(res["response_received_at"]) if res["response_received_at"] else None,
        "outcome": outcome, "http_status": res["http_status"], "payload_sha256": payload_sha,
        "data_provenance": provenance,
    }, observations)
    return outcome


class Collector:
    def __init__(self, store, cfg, *, fetcher=None, clock=adapter.utc_now, sleeper=None, config_path=None):
        self.store, self.cfg, self.clock = store, cfg, clock
        self.config_path = config_path
        live = cfg["source"]["mode"] == "LIVE"
        self.provenance = "LIVE" if live else "SYNTHETIC"
        live_fetcher = lambda url, timeout=5.0: adapter.fetch(url, timeout=timeout, clock=clock)
        self.fetcher = fetcher or (live_fetcher if live else synthetic.make_fetcher(clock))
        self.stop_event = threading.Event()
        self.sleep = sleeper or (lambda s: self.stop_event.wait(s))
        self.consecutive_failures = 0
        self.paused_reason = None

    def round(self):
        """One polling round with bounded retries. Returns final outcome."""
        delays = [2 * (2 ** i) for i in range(self.cfg["retries"])]
        outcome = ingest_once(self.store, self.cfg, self.fetcher, provenance=self.provenance, clock=self.clock)
        for d in delays:
            if outcome not in TRANSIENT or self.stop_event.is_set():
                break
            self.sleep(d)
            outcome = ingest_once(self.store, self.cfg, self.fetcher, provenance=self.provenance, clock=self.clock)
        if outcome in ("NO_CONNECTION", "TIMEOUT") and self.config_path is not None and not self.stop_event.is_set():
            from . import discover
            bound = discover.auto_bind(self.config_path)
            if bound["discovered"]:
                self.cfg.update(bound["config"])
                self.provenance = "LIVE"
                self.fetcher = lambda url, timeout=5.0: adapter.fetch(url, timeout=timeout, clock=self.clock)
                register(self.store, self.cfg, self.clock)
                outcome = ingest_once(self.store, self.cfg, self.fetcher, provenance=self.provenance, clock=self.clock)
        if outcome == "OK":
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
        if outcome == "AUTH_REJECTED":
            self.paused_reason = "AUTH_PAUSED"
            self.store.set_source_status(self.cfg["source"]["source_id"], "AUTH_PAUSED", adapter.iso(self.clock()))
        es_mode = (self.cfg.get("eight_sleep") or {}).get("mode", "READY")
        if es_mode in ("LIVE", "SYNTHETIC") and not self.stop_event.is_set():
            from . import eight_sleep
            eight_sleep.ingest_once(self.store, self.cfg, clock=self.clock)
        return outcome

    def next_delay(self):
        base = self.cfg["poll_interval_s"]
        if self.consecutive_failures <= 1:
            return base
        return min(base * (2 ** (self.consecutive_failures - 1)), self.cfg["max_backoff_s"])

    def run(self, max_rounds=None):
        rounds = 0
        while not self.stop_event.is_set():
            outcome = self.round()
            rounds += 1
            log.info("round=%d outcome=%s consecutive_failures=%d", rounds, outcome, self.consecutive_failures)
            if self.paused_reason:
                log.warning("source paused: %s (restart after operator fixes binding)", self.paused_reason)
                return self.paused_reason
            if max_rounds is not None and rounds >= max_rounds:
                break
            self.sleep(self.next_delay())
        return "STOPPED"

    def stop(self):
        self.stop_event.set()
