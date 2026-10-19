"""CTDS C01 SQLite store: append-only observations/attempts, idempotent by event_id.

UPDATE/DELETE on history tables are rejected by triggers (no silent rewrite of history).
"""
import sqlite3
import threading
from pathlib import Path

from . import catalog

SCHEMA_VERSION = "ctds.c01.store/0.1.0"

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_registration (
  source_id TEXT PRIMARY KEY, provider TEXT NOT NULL, device_class TEXT NOT NULL,
  room_id TEXT NOT NULL, binding_revision INTEGER NOT NULL, configuration_version TEXT NOT NULL,
  status TEXT NOT NULL, capability_version TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ingestion_attempt (
  attempt_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, request_started_at TEXT NOT NULL,
  response_received_at TEXT, outcome TEXT NOT NULL, http_status INTEGER, payload_sha256 TEXT,
  data_provenance TEXT NOT NULL CHECK (data_provenance IN ('LIVE','REPLAY','SYNTHETIC')),
  observations_written INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_attempt_src_time ON ingestion_attempt(source_id, request_started_at);
CREATE TABLE IF NOT EXISTS observation (
  event_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, room_id TEXT NOT NULL, metric_id TEXT NOT NULL,
  value_status TEXT NOT NULL CHECK (value_status IN ('OBSERVED','EXPLICIT_NULL','NOT_PROVIDED','INVALID','NO_OBSERVATION')),
  value REAL, unit TEXT NOT NULL, provider_observed_at TEXT, gateway_received_at TEXT NOT NULL,
  time_basis TEXT NOT NULL, mapping_version TEXT NOT NULL,
  data_provenance TEXT NOT NULL CHECK (data_provenance IN ('LIVE','REPLAY','SYNTHETIC')),
  source_event_identity TEXT, revision INTEGER NOT NULL DEFAULT 1,
  CHECK ((value_status = 'OBSERVED') = (value IS NOT NULL)));
CREATE INDEX IF NOT EXISTS ix_obs_room_metric_time ON observation(room_id, metric_id, gateway_received_at);
CREATE TABLE IF NOT EXISTS display_settings (
  room_id TEXT NOT NULL, metric_id TEXT NOT NULL, enabled INTEGER NOT NULL, display_order INTEGER NOT NULL,
  label_zh TEXT NOT NULL, config_revision INTEGER NOT NULL, local_operator_action TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY (room_id, metric_id));
CREATE TRIGGER IF NOT EXISTS no_update_observation BEFORE UPDATE ON observation
  BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_observation BEFORE DELETE ON observation
  BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS no_update_attempt BEFORE UPDATE ON ingestion_attempt
  BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
CREATE TRIGGER IF NOT EXISTS no_delete_attempt BEFORE DELETE ON ingestion_attempt
  BEGIN SELECT RAISE(ABORT, 'APPEND_ONLY'); END;
"""

OBS_COLS = ("event_id", "source_id", "room_id", "metric_id", "value_status", "value", "unit",
            "provider_observed_at", "gateway_received_at", "time_basis", "mapping_version",
            "data_provenance", "source_event_identity")


class Store:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(DDL)
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (SCHEMA_VERSION,))

    def close(self):
        self.conn.close()

    def _tx(self, fn):
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self.conn)
                self.conn.execute("COMMIT")
                return out
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def _q(self, sql, args=()):
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    # --- sources -------------------------------------------------------
    def register_source(self, *, source_id, provider, device_class, room_id, binding_revision,
                        configuration_version, status, capability_version, updated_at):
        self._tx(lambda c: c.execute(
            "INSERT INTO source_registration VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
            "provider=excluded.provider, device_class=excluded.device_class, room_id=excluded.room_id, "
            "binding_revision=excluded.binding_revision, configuration_version=excluded.configuration_version, "
            "status=excluded.status, capability_version=excluded.capability_version, updated_at=excluded.updated_at",
            (source_id, provider, device_class, room_id, binding_revision, configuration_version, status,
             capability_version, updated_at)))

    def set_source_status(self, source_id, status, updated_at):
        self._tx(lambda c: c.execute("UPDATE source_registration SET status=?, updated_at=? WHERE source_id=?",
                                     (status, updated_at, source_id)))

    def source(self, source_id):
        rows = self._q("SELECT * FROM source_registration WHERE source_id=?", (source_id,))
        return dict(rows[0]) if rows else None

    def sources_for_room(self, room_id):
        return [dict(r) for r in self._q("SELECT * FROM source_registration WHERE room_id=? ORDER BY source_id", (room_id,))]

    # --- ingest --------------------------------------------------------
    def record_attempt(self, attempt, observations=()):
        """Atomically append one attempt and its observations. Re-recording is a no-op."""
        def run(c):
            written = 0
            for o in observations:
                cur = c.execute(f"INSERT INTO observation ({','.join(OBS_COLS)}) VALUES "
                                f"({','.join('?' * len(OBS_COLS))}) ON CONFLICT(event_id) DO NOTHING",
                                tuple(o[k] for k in OBS_COLS))
                written += cur.rowcount
            c.execute("INSERT INTO ingestion_attempt VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(attempt_id) DO NOTHING",
                      (attempt["attempt_id"], attempt["source_id"], attempt["request_started_at"],
                       attempt.get("response_received_at"), attempt["outcome"], attempt.get("http_status"),
                       attempt.get("payload_sha256"), attempt["data_provenance"], written))
            return written
        return self._tx(run)

    @staticmethod
    def _prov(provenance):
        return (" AND data_provenance=?", (provenance,)) if provenance else ("", ())

    def latest_attempt(self, source_id, *, provenance=None):
        extra, args = self._prov(provenance)
        rows = self._q(f"SELECT * FROM ingestion_attempt WHERE source_id=?{extra} "
                       "ORDER BY request_started_at DESC, rowid DESC LIMIT 1", (source_id, *args))
        return dict(rows[0]) if rows else None

    def last_success(self, source_id, *, provenance=None):
        extra, args = self._prov(provenance)
        rows = self._q(f"SELECT * FROM ingestion_attempt WHERE source_id=? AND outcome='OK'{extra} "
                       "ORDER BY request_started_at DESC, rowid DESC LIMIT 1", (source_id, *args))
        return dict(rows[0]) if rows else None

    def recent_attempts(self, source_id, limit=20):
        return [dict(r) for r in self._q("SELECT * FROM ingestion_attempt WHERE source_id=? "
                                         "ORDER BY request_started_at DESC, rowid DESC LIMIT ?", (source_id, limit))]

    def latest_observation(self, room_id, metric_id, *, observed_only=False, provenance=None):
        extra = " AND value_status='OBSERVED'" if observed_only else ""
        pextra, args = self._prov(provenance)
        rows = self._q(f"SELECT * FROM observation WHERE room_id=? AND metric_id=?{extra}{pextra} "
                       "ORDER BY gateway_received_at DESC, rowid DESC LIMIT 1", (room_id, metric_id, *args))
        return dict(rows[0]) if rows else None

    def series(self, room_id, metric_id, since_iso, limit=720, *, provenance=None):
        pextra, args = self._prov(provenance)
        rows = self._q("SELECT gateway_received_at AS t, value, data_provenance FROM observation "
                       f"WHERE room_id=? AND metric_id=? AND value_status='OBSERVED' AND gateway_received_at>=?{pextra} "
                       "ORDER BY gateway_received_at DESC LIMIT ?", (room_id, metric_id, since_iso, *args, limit))
        return [dict(r) for r in reversed(rows)]

    def verified_metrics(self, source_id):
        return {r[0] for r in self._q("SELECT DISTINCT metric_id FROM observation WHERE source_id=? "
                                      "AND value_status='OBSERVED' AND data_provenance='LIVE'", (source_id,))}

    def counts(self):
        return {t: self._q(f"SELECT COUNT(*) FROM {t}")[0][0] for t in ("observation", "ingestion_attempt")}

    # --- display settings ---------------------------------------------
    def display_settings(self, room_id):
        stored = {r["metric_id"]: dict(r) for r in self._q("SELECT * FROM display_settings WHERE room_id=?", (room_id,))}
        out = []
        for mid, m in catalog.METRICS.items():
            s = stored.get(mid)
            out.append({"metric_id": mid,
                        "enabled": bool(s["enabled"]) if s else m["default"],
                        "display_order": s["display_order"] if s else m["order"],
                        "label_zh": s["label_zh"] if s else m["label_zh"],
                        "config_revision": s["config_revision"] if s else 0})
        out.sort(key=lambda x: (x["display_order"], x["metric_id"]))
        return out

    def config_revision(self, room_id):
        return self._q("SELECT COALESCE(MAX(config_revision),0) FROM display_settings WHERE room_id=?", (room_id,))[0][0]

    def save_display_settings(self, room_id, items, *, action, updated_at):
        def run(c):
            rev = c.execute("SELECT COALESCE(MAX(config_revision),0) FROM display_settings WHERE room_id=?",
                            (room_id,)).fetchone()[0] + 1
            for it in items:
                c.execute("INSERT INTO display_settings VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(room_id, metric_id) DO UPDATE SET "
                          "enabled=excluded.enabled, display_order=excluded.display_order, label_zh=excluded.label_zh, "
                          "config_revision=excluded.config_revision, local_operator_action=excluded.local_operator_action, "
                          "updated_at=excluded.updated_at",
                          (room_id, it["metric_id"], int(it["enabled"]), int(it["display_order"]), it["label_zh"],
                           rev, action, updated_at))
            return rev
        return self._tx(run)
