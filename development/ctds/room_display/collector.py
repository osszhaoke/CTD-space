"""Polling collector: timeout, bounded retry/backoff, auth pause, clean stop."""
import logging
import os
import threading
import uuid

from . import adapter, catalog, cloud_backfill, mqtt_adapter, synthetic

log = logging.getLogger("ctds.c01.collector")
TRANSIENT = {"TIMEOUT", "NO_CONNECTION", "HTTP_ERROR"}


def register(store, cfg, clock=adapter.utc_now):
    src = cfg["source"]
    is_mqtt = src.get("mode") == "LIVE" and src.get("protocol", "http") == "mqtt"
    store.register_source(
        source_id=src["source_id"], provider="airgradient", device_class="air_quality_monitor",
        room_id=cfg["room_id"], binding_revision=src["binding_revision"],
        configuration_version=catalog.MQTT_MAPPING_VERSION if is_mqtt else catalog.MAPPING_VERSION,
        status="ACTIVE",
        capability_version=catalog.MQTT_DOC_REF if is_mqtt else catalog.DOC_REF,
        updated_at=adapter.iso(clock()),
    )


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
        self.is_mqtt_live = live and cfg["source"].get("protocol", "http") == "mqtt"
        self.provenance = "LIVE" if live else "SYNTHETIC"
        live_fetcher = lambda url, timeout=5.0: adapter.fetch(url, timeout=timeout, clock=clock)
        self.fetcher = fetcher or (live_fetcher if live else synthetic.make_fetcher(clock))
        self.stop_event = threading.Event()
        self.sleep = sleeper or (lambda s: self.stop_event.wait(s))
        self.consecutive_failures = 0
        self.paused_reason = None
        self._last_es_poll_at = 0.0
        self._es_next_poll_at = 0.0
        self.es_consecutive_failures = 0
        self.es_paused_reason = None
        self._es_config_mtime_ns = self._stat_config_mtime_ns()
        self._mqtt_receiver = None
        self._mqtt_rx_event = threading.Event()
        self._mqtt_last_outcome = None
        self._mqtt_rx_count = 0
        self._last_mqtt_ok_ts = None
        self._mqtt_disconnect_recorded = False
        self.mqtt_bound_address = None
        self._mqtt_dedup = mqtt_adapter.MqttDeduplicator(
            window_s=cfg["source"].get("mqtt_dedup_window_s", 15),
        )
        self.cloud_backfill = cloud_backfill.CloudBackfillManager(
            store, cfg, clock=clock,
        )
        store._cloud_backfill_mgr = self.cloud_backfill

    def _now_ts(self):
        try:
            return self.clock().timestamp()
        except Exception:
            import time
            return time.time()

    def _on_mqtt_outcome(self, outcome, rx_dt):
        self._mqtt_last_outcome = outcome
        if outcome in mqtt_adapter.MQTT_SUCCESS_OUTCOMES:
            prev_ok_ts = self._last_mqtt_ok_ts
            now_rx_ts = rx_dt.timestamp()
            self._last_mqtt_ok_ts = now_rx_ts
            self._mqtt_disconnect_recorded = False
            self.consecutive_failures = 0
            self._mqtt_rx_count += 1
            self._mqtt_rx_event.set()
            if prev_ok_ts is not None and (now_rx_ts - prev_ok_ts) >= cloud_backfill.GAP_HEAL_THRESHOLD_S:
                self.cloud_backfill.trigger_gap_check()
            return
        self.consecutive_failures += 1
        if outcome == mqtt_adapter.MQTT_AUTH_REJECTED:
            self.paused_reason = "AUTH_PAUSED"
            self.store.set_source_status(
                self.cfg["source"]["source_id"], "AUTH_PAUSED", adapter.iso(rx_dt),
            )
            self._mqtt_rx_event.set()

    def _start_mqtt_receiver(self):
        if self._mqtt_receiver is not None:
            return self._mqtt_receiver
        mode = self.cfg["source"].get("mqtt_mode", "listen")
        cls = (mqtt_adapter.MqttReadOnlyListener if mode == "listen"
               else mqtt_adapter.MqttReadOnlySubscriber)
        rec = cls(
            self.store, self.cfg,
            deduplicator=self._mqtt_dedup,
            provenance=self.provenance,
            clock=self.clock,
            on_outcome=self._on_mqtt_outcome,
        )
        addr = rec.start()
        self._mqtt_receiver = rec
        self.mqtt_bound_address = getattr(rec, "bound_address", addr)
        return rec

    def _stop_mqtt_receiver(self):
        rec = self._mqtt_receiver
        self._mqtt_receiver = None
        if rec is not None:
            rec.stop()

    def check_mqtt_watchdog(self):
        """Record one MQTT_DISCONNECTED attempt when silent past mqtt_inactivity_s."""
        if self._last_mqtt_ok_ts is None:
            self._last_mqtt_ok_ts = self._now_ts()
        inact_s = self.cfg["source"].get("mqtt_inactivity_s", 240)
        if (self._now_ts() - self._last_mqtt_ok_ts) >= inact_s and not self._mqtt_disconnect_recorded:
            mqtt_adapter.record_mqtt_attempt(
                self.store, self.cfg, mqtt_adapter.MQTT_DISCONNECTED,
                clock=self.clock, provenance=self.provenance,
            )
            self._mqtt_disconnect_recorded = True
            self.consecutive_failures += 1
            return True
        return False

    def _stat_config_mtime_ns(self):
        if not self.config_path:
            return None
        try:
            return os.stat(self.config_path).st_mtime_ns
        except OSError:
            return None

    def _sync_eight_sleep_config_if_rebound(self):
        if not self.config_path:
            return
        mtime_ns = self._stat_config_mtime_ns()
        if mtime_ns is None or mtime_ns == self._es_config_mtime_ns:
            return
        self._es_config_mtime_ns = mtime_ns
        try:
            from . import config as _cfg
            disk_cfg = _cfg.load(self.config_path)
        except Exception:
            return
        disk_es = disk_cfg.get("eight_sleep")
        if not isinstance(disk_es, dict):
            return
        cur_es = self.cfg.get("eight_sleep") or {}
        if (disk_es.get("binding_revision") != cur_es.get("binding_revision")
                or disk_es.get("mode") != cur_es.get("mode")):
            self.cfg["eight_sleep"] = dict(disk_es)
            self.es_paused_reason = None
            self.es_consecutive_failures = 0
            self._es_next_poll_at = 0.0

    def _execute_eight_sleep_poll(self, now_ts):
        self._sync_eight_sleep_config_if_rebound()
        es_cfg = self.cfg.get("eight_sleep") or {}
        es_mode = es_cfg.get("mode", "READY")
        if es_mode not in ("LIVE", "SYNTHETIC") or self.stop_event.is_set():
            return None
        if self.es_paused_reason:
            return self.es_paused_reason
        self._last_es_poll_at = now_ts
        base = float(self.cfg.get("poll_interval_s", 60))
        max_bo = float(self.cfg.get("max_backoff_s", 300))
        meta = {}
        try:
            from . import eight_sleep
            outcome = eight_sleep.ingest_once(
                self.store, self.cfg, clock=self.clock, config_path=self.config_path, out_meta=meta,
            )
            self._es_config_mtime_ns = self._stat_config_mtime_ns()
        except Exception as exc:
            log.warning("Eight Sleep poll failed: %s", type(exc).__name__)
            self.es_consecutive_failures += 1
            delay = min(base * (2 ** max(0, self.es_consecutive_failures - 1)), max_bo)
            self._es_next_poll_at = now_ts + delay
            return "ERROR"

        if outcome == "OK":
            self.es_consecutive_failures = 0
            self._es_next_poll_at = now_ts + base
            return outcome

        self.es_consecutive_failures += 1
        if outcome == "AUTH_REJECTED":
            self.es_paused_reason = "AUTH_PAUSED"
            log.warning("Eight Sleep source paused: AUTH_PAUSED (AirGradient unaffected; run bind-eight-sleep to resume)")
            return outcome

        if meta.get("http_status") == 429:
            delay = min(3600.0, max(900.0, float(meta.get("retry_after_s") or 0), base * (2 ** self.es_consecutive_failures)))
            log.warning("Eight Sleep rate-limited (HTTP 429); backing off %.0fs to allow window reset", delay)
        else:
            delay = min(base * (2 ** max(0, self.es_consecutive_failures - 1)), max_bo)
        self._es_next_poll_at = now_ts + delay
        return outcome

    def _poll_eight_sleep_once_in_round(self):
        self._sync_eight_sleep_config_if_rebound()
        if self.es_paused_reason or self.stop_event.is_set():
            return
        now_ts = self._now_ts()
        if self.es_consecutive_failures > 0 and now_ts < self._es_next_poll_at:
            return
        self._execute_eight_sleep_poll(now_ts)

    def round(self, *, mqtt_wait_s=None):
        """One polling round with bounded retries (or bounded single MQTT wait). Returns final outcome."""
        if self.is_mqtt_live:
            self._mqtt_rx_event.clear()
            self._mqtt_last_outcome = None
            wait_s = float(self.cfg["timeout_s"] if mqtt_wait_s is None else mqtt_wait_s)
            try:
                self._start_mqtt_receiver()
            except (OSError, adapter.AdapterError) as exc:
                log.warning("MQTT receiver start failed in round: %s", type(exc).__name__)
                self._poll_eight_sleep_once_in_round()
                self.consecutive_failures += 1
                return mqtt_adapter.record_mqtt_attempt(
                    self.store, self.cfg, mqtt_adapter.MQTT_DISCONNECTED,
                    clock=self.clock, provenance=self.provenance,
                )
            try:
                self._poll_eight_sleep_once_in_round()
                self._mqtt_rx_event.wait(timeout=max(0.05, wait_s))
            finally:
                self._stop_mqtt_receiver()
            if self._mqtt_last_outcome is not None:
                return self._mqtt_last_outcome
            self.consecutive_failures += 1
            return mqtt_adapter.record_mqtt_attempt(
                self.store, self.cfg, mqtt_adapter.MQTT_DISCONNECTED,
                clock=self.clock, provenance=self.provenance,
            )

        delays = [2 * (2 ** i) for i in range(self.cfg["retries"])]
        outcome = ingest_once(self.store, self.cfg, self.fetcher, provenance=self.provenance, clock=self.clock)
        for d in delays:
            if outcome not in TRANSIENT or self.stop_event.is_set():
                break
            self.sleep(d)
            outcome = ingest_once(self.store, self.cfg, self.fetcher, provenance=self.provenance, clock=self.clock)
        if (
            outcome in ("NO_CONNECTION", "TIMEOUT")
            and self.config_path is not None
            and not self.stop_event.is_set()
            and os.environ.get("CTDS_TEST_SANDBOX") != "1"
        ):
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
        self._poll_eight_sleep_once_in_round()
        return outcome

    def next_delay(self):
        base = self.cfg["poll_interval_s"]
        if self.consecutive_failures <= 1:
            return base
        return min(base * (2 ** (self.consecutive_failures - 1)), self.cfg["max_backoff_s"])

    def poll_eight_sleep_if_due(self):
        """Poll Eight Sleep on its own cadence (with bounded backoff / AUTH_PAUSED compliance) even if AirGradient is in backoff."""
        if self.stop_event.is_set():
            return
        self._sync_eight_sleep_config_if_rebound()
        es_mode = (self.cfg.get("eight_sleep") or {}).get("mode", "READY")
        if es_mode not in ("LIVE", "SYNTHETIC") or self.es_paused_reason:
            return
        now_ts = self._now_ts()
        es_interval = float(self.cfg.get("poll_interval_s", 60))
        due_at = self._es_next_poll_at if self._es_next_poll_at > 0 else (
            (self._last_es_poll_at + es_interval) if self._last_es_poll_at > 0 else 0.0
        )
        if now_ts >= due_at:
            self._execute_eight_sleep_poll(now_ts)

    def _run_mqtt(self, max_rounds=None):
        self._last_mqtt_ok_ts = self._now_ts()
        self._mqtt_disconnect_recorded = False
        start_rx = self._mqtt_rx_count
        bind_backoff = 5.0
        next_bind_try_ts = 0.0
        last_loop_ts = self._now_ts()
        try:
            self.poll_eight_sleep_if_due()
            while not self.stop_event.is_set():
                loop_now = self._now_ts()
                if (loop_now - last_loop_ts) >= cloud_backfill.GAP_HEAL_THRESHOLD_S:
                    self.cloud_backfill.trigger_gap_check()
                last_loop_ts = loop_now

                if self._mqtt_receiver is None and not self.paused_reason:
                    now_ts = self._now_ts()
                    if now_ts >= next_bind_try_ts:
                        try:
                            self._start_mqtt_receiver()
                            bind_backoff = 5.0
                        except (OSError, adapter.AdapterError) as exc:
                            log.warning("MQTT receiver start failed: %s (retrying in %.0fs; Eight Sleep continues)",
                                        type(exc).__name__, bind_backoff)
                            if not self._mqtt_disconnect_recorded:
                                mqtt_adapter.record_mqtt_attempt(
                                    self.store, self.cfg, mqtt_adapter.MQTT_DISCONNECTED,
                                    clock=self.clock, provenance=self.provenance,
                                )
                                self._mqtt_disconnect_recorded = True
                                self.consecutive_failures += 1
                            next_bind_try_ts = now_ts + bind_backoff
                            bind_backoff = min(bind_backoff * 2.0, float(self.cfg["max_backoff_s"]))

                if self.paused_reason:
                    self._stop_mqtt_receiver()
                    log.warning("source paused: %s (restart after operator fixes binding)", self.paused_reason)
                    if max_rounds is None:
                        while not self.stop_event.is_set():
                            self.sleep(1.0)
                            self.poll_eight_sleep_if_due()
                    return self.paused_reason
                if self._mqtt_receiver is not None:
                    self.check_mqtt_watchdog()
                self.poll_eight_sleep_if_due()
                if max_rounds is not None and (self._mqtt_rx_count - start_rx) >= max_rounds:
                    break
                self.sleep(1.0)
            return "STOPPED"
        finally:
            self._stop_mqtt_receiver()

    def run(self, max_rounds=None):
        if max_rounds is None:
            self.cloud_backfill.start()
        try:
            if self.is_mqtt_live:
                return self._run_mqtt(max_rounds=max_rounds)
            rounds = 0
            while not self.stop_event.is_set():
                outcome = self.round()
                rounds += 1
                log.info("round=%d outcome=%s consecutive_failures=%d", rounds, outcome, self.consecutive_failures)
                if self.paused_reason:
                    log.warning("source paused: %s (restart after operator fixes binding)", self.paused_reason)
                    # AirGradient paused must not stop Eight Sleep: keep its own cadence until stopped.
                    if max_rounds is None:
                        while not self.stop_event.is_set():
                            self.sleep(1.0)
                            self.poll_eight_sleep_if_due()
                    return self.paused_reason
                if max_rounds is not None and rounds >= max_rounds:
                    break
                delay = self.next_delay()
                chunk = 1.0
                slept = 0.0
                while slept < delay and not self.stop_event.is_set():
                    to_sleep = min(chunk, delay - slept)
                    self.sleep(to_sleep)
                    slept += to_sleep
                    self.poll_eight_sleep_if_due()
            return "STOPPED"
        finally:
            if max_rounds is None:
                self.cloud_backfill.stop()

    def stop(self):
        self.stop_event.set()
        self._mqtt_rx_event.set()
        self._stop_mqtt_receiver()
        self.cloud_backfill.stop()
