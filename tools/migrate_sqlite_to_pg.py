#!/usr/bin/env python3
"""Migrate observations from legacy CTDS C01 SQLite store (ctds_c01.sqlite3) into PostgreSQL."""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

import psycopg2
from psycopg2.extras import Json


def migrate(sqlite_path: str, pg_host: str, pg_port: int, pg_db: str, pg_user: str, pg_pass: str):
    p = Path(sqlite_path)
    if not p.is_file():
        print(f"[ERROR] SQLite database not found: {p}", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to SQLite: {p}...")
    s_conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    s_conn.row_factory = sqlite3.Row

    print(f"Connecting to PostgreSQL: {pg_user}@{pg_host}:{pg_port}/{pg_db}...")
    pg_conn = psycopg2.connect(
        host=pg_host, port=pg_port, dbname=pg_db, user=pg_user, password=pg_pass
    )
    pg_conn.autocommit = False

    cursor = pg_conn.cursor()

    # 1. Migrate sources
    print("Migrating source registrations...")
    sources = s_conn.execute("SELECT * FROM source_registration").fetchall()
    for s in sources:
        cursor.execute(
            """
            INSERT INTO sources (id, name, kind, metric, unit, enabled, status, metadata, last_seen)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                last_seen = EXCLUDED.last_seen
            """,
            (
                s["source_id"],
                f"AirGradient · {s['source_id']}",
                "airgradient",
                "air_quality",
                "ppm",
                True,
                "streaming" if s["status"] == "ACTIVE" else "discovered",
                Json({"device_class": s["device_class"], "provider": s["provider"]}),
                s["updated_at"],
            ),
        )

    # 2. Group observations by attempt / gateway_received_at to form snapshots
    print("Reconstructing and importing environmental snapshots...")
    obs_rows = s_conn.execute(
        """SELECT source_id, gateway_received_at, metric_id, value, source_event_identity
           FROM observation
           WHERE value_status = 'OBSERVED' AND value IS NOT NULL
           ORDER BY gateway_received_at ASC"""
    ).fetchall()

    snapshots = {}
    for r in obs_rows:
        key = (r["source_id"], r["gateway_received_at"])
        if key not in snapshots:
            snapshots[key] = {}
        # Map internal metric_id back to AirGradient provider field
        field_map = {
            "temperature": "atmp",
            "humidity": "rhum",
            "co2": "rco2",
            "pm2_5": "pm02",
            "pm1": "pm01",
            "pm10": "pm10",
            "tvoc_index": "tvocIndex",
            "nox_index": "noxIndex",
        }
        field = field_map.get(r["metric_id"], r["metric_id"])
        snapshots[key][field] = r["value"]

    count = 0
    for (src_id, ts), measurements in snapshots.items():
        co2_val = measurements.get("rco2")
        cursor.execute(
            """
            INSERT INTO samples (source_id, value, recorded_at, metadata)
            VALUES (%s, %s, %s, %s)
            """,
            (src_id, co2_val, ts, Json({"measurements": measurements, "migrated": True})),
        )
        count += 1

    pg_conn.commit()
    print(f"Successfully migrated {len(sources)} source(s) and {count} snapshot(s) into PostgreSQL!")
    cursor.close()
    pg_conn.close()
    s_conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="var/ctds-c01/ctds_c01.sqlite3", help="Path to ctds_c01.sqlite3")
    parser.add_argument("--host", default=os.getenv("POSTGRES_HOST", "127.0.0.1"), help="PG host")
    parser.add_argument("--port", type=int, default=int(os.getenv("POSTGRES_PORT", "5432")), help="PG port")
    parser.add_argument("--db", default=os.getenv("POSTGRES_DB", "postgres"), help="PG database")
    parser.add_argument("--user", default=os.getenv("POSTGRES_USER", "postgres"), help="PG user")
    parser.add_argument("--password", default=os.getenv("POSTGRES_PASSWORD", "docker"), help="PG password")
    args = parser.parse_args()
    migrate(args.sqlite, args.host, args.port, args.db, args.user, args.password)
