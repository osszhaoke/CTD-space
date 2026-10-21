import json
from datetime import datetime
import asyncpg


class Store:
    """Shared persistence contract for environment and microenvironment sources."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def discover(
        self, source_id, name, kind, metric, unit, metadata=None, *, enabled=False
    ):
        await self.pool.execute(
            """
            INSERT INTO sources (id, name, kind, metric, unit, metadata, enabled)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                last_seen = now(),
                metadata = EXCLUDED.metadata
            """,
            source_id, name, kind, metric, unit, json.dumps(metadata or {}), enabled,
        )

    async def enabled(self, kind):
        rows = await self.pool.fetch(
            "SELECT id FROM sources WHERE kind = $1 AND enabled", kind
        )
        return {row["id"] for row in rows}

    async def status(self, source_id, status, error=None, *, seen=False):
        await self.pool.execute(
            """UPDATE sources SET status = $2, error = $3,
                last_seen = CASE WHEN $4 THEN now() ELSE last_seen END WHERE id = $1""",
            source_id, status, error, seen,
        )

    async def write(
        self, source_id: str, value: float | None, recorded_at: datetime, metadata=None
    ):
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                source = await connection.fetchrow(
                    "SELECT enabled, kind FROM sources WHERE id = $1 FOR UPDATE", source_id
                )
                if not source or not source["enabled"]:
                    return
                await connection.execute(
                    """
                    INSERT INTO samples (source_id, value, recorded_at, metadata)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    source_id, value, recorded_at, json.dumps(metadata or {}, allow_nan=False),
                )
                await connection.execute(
                    """
                    UPDATE sources SET sample_count = sample_count + 1,
                        last_sample_at = $2, last_seen = now(),
                        status = 'streaming', error = NULL
                    WHERE id = $1
                    """,
                    source_id, recorded_at,
                )

    async def heartbeat(self, error=None):
        await self.pool.execute(
            """
            INSERT INTO collector_status (id, heartbeat, error) VALUES (1, now(), $1)
            ON CONFLICT (id) DO UPDATE SET heartbeat = now(), error = EXCLUDED.error
            """,
            error,
        )
