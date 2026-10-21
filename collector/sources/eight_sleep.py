"""Complete Eight Sleep snapshots plus resumable, deduplicated sleep history."""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone

import httpx

from collector.eight_api import APIError, EightAPI, INTERVAL, finite_number, history_start, measurements, snapshot

KIND = "eight_sleep"
logger = logging.getLogger(__name__)


async def save_days(store, source_id, uid, response):
    """Upsert whole day objects, including every session and timeseries point."""
    rows = []
    for day in response.get("days") or []:
        stamp = day.get("day") or day.get("date")
        if stamp is None:
            raise ValueError("Eight Sleep 历史记录缺少日期")
        rows.append((source_id, uid, date.fromisoformat(str(stamp)[:10]), json.dumps(day, allow_nan=False)))
    async with store.pool.acquire() as conn:
        async with conn.transaction():
            enabled = await conn.fetchval("SELECT enabled FROM sources WHERE id=$1 FOR UPDATE", source_id)
            if not enabled:
                return False
            await conn.executemany("""
                INSERT INTO eight_sleep_days(source_id,user_id,day,data) VALUES($1,$2,$3,$4::jsonb)
                ON CONFLICT(source_id,user_id,day) DO UPDATE SET data=EXCLUDED.data,fetched_at=now()
            """, rows)
    return True


async def backfill_one(api, store, source_id, uid, profile):
    """One 30-day window per pass; never mistake an empty month for end of history."""
    today = datetime.now(timezone.utc).date()
    cursor = await store.pool.fetchrow(
        "SELECT next_before,complete FROM eight_sleep_sync WHERE source_id=$1 AND user_id=$2", source_id, uid
    )
    if cursor and cursor["complete"]:
        return False
    end = cursor["next_before"] if cursor else today + timedelta(days=1)
    earliest = history_start(profile)
    start = max(earliest, end - timedelta(days=30))
    if start < end:
        tz = (profile.get("currentDevice") or {}).get("timeZone") or "UTC"
        response = await api.trends(uid, tz, start, end)
        if not isinstance(response.get("days"), list):
            raise APIError()
        if not await save_days(store, source_id, uid, response):
            return False
    # Advance only after successful fetch + commit; a crash safely repeats the window.
    await store.pool.execute("""
        INSERT INTO eight_sleep_sync(source_id,user_id,next_before,complete) VALUES($1,$2,$3,$4)
        ON CONFLICT(source_id,user_id) DO UPDATE SET next_before=EXCLUDED.next_before,complete=EXCLUDED.complete
    """, source_id, uid, start, start <= earliest)
    return start > earliest



def merge_display(previous, current, recorded_at):
    """Retain each last valid reading with its real reception time."""
    previous = previous or {}
    values = dict(previous.get("measurements") or {})
    updated_at = dict(previous.get("updated_at") or {})
    for key, value in current.items():
        if not finite_number(value) or (key == "heartRate" and value <= 0):
            continue
        if current.get("hub_online") is False and key in ("currentLevel", "currentDeviceLevel"):
            continue
        values[key] = value
        updated_at[key] = recorded_at
    if isinstance(current.get("sleep_day"), str):
        values["sleep_day"] = current["sleep_day"]
    return {"measurements": values, "updated_at": updated_at}


async def restore_display(store, source_id):
    # The latest snapshot persists the display cache across page/collector restarts.
    # Older snapshots seed the cache once when upgrading from the prior format.
    rows = await store.pool.fetch(
        """SELECT recorded_at, metadata->'display' AS display,
                  metadata->'measurements' AS measurements
           FROM samples WHERE source_id=$1 ORDER BY recorded_at DESC,id DESC LIMIT 120""", source_id)
    state, pending = {}, []
    for row in rows:
        saved = row["display"]
        if saved:
            state = json.loads(saved) if isinstance(saved, str) else saved
            break
        pending.append(row)
    for row in reversed(pending):
        value = row["measurements"]
        current = json.loads(value) if isinstance(value, str) else value or {}
        state = merge_display(state, current, row["recorded_at"].isoformat())
    return state


def next_poll_at(started, finished):
    # Anchor normal polls to their start, skip missed ticks, never overlap polls.
    return started + max(1, int((finished - started) // INTERVAL) + 1) * INTERVAL


async def run(store):
    email, password = os.getenv("EIGHT_EMAIL", ""), os.getenv("EIGHT_PASSWORD", "")
    if not email or not password:
        logger.info("Eight Sleep 未配置账号，等待配置后重建采集容器")
        await asyncio.Event().wait()
        return
    source_id = "eight_sleep:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    await store.discover(source_id, "Eight Sleep", KIND, "sleep_score", "分",
                         {"poll_seconds": INTERVAL}, enabled=True)
    # HTTPX INFO logs include user IDs in URLs; the module logs only aggregate results.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    profiles = {}
    history_retry = {}
    next_poll = 0
    next_history = 0
    display = await restore_display(store, source_id)
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as http:
        api = EightAPI(email, password, http)
        while True:
            if source_id not in await store.enabled(KIND):
                await store.status(source_id, "paused")
                next_poll = 0
                await asyncio.sleep(2)
                continue
            try:
                if time.monotonic() >= next_poll:
                    started = time.monotonic()
                    await store.status(source_id, "waiting")
                    payload, profiles = await snapshot(api)
                    preview = measurements(payload)
                    for uid in profiles:
                        trends = payload["responses"].get(f"user/{uid}/trends")
                        if trends is not None:
                            await save_days(store, source_id, uid, trends)
                    received = datetime.now(timezone.utc)
                    display = merge_display(display, preview, received.isoformat())
                    await store.write(source_id, preview.get("sleep_score"), received, {
                        **payload, "measurements": preview, "display": display, "poll_seconds": INTERVAL,
                    })
                    issues = {key: value for key, value in payload["errors"].items()
                              if value not in ("HTTP 404", "HTTP 403")}
                    if issues:
                        message = (
                            "云端报告 Pod 离线，底座和播放器实时状态暂不可读；其余数据已保存"
                            if any("PodOffline" in error for error in issues.values())
                            else f"{len(issues)} 个接口暂不可用；完整状态见采集数据"
                        )
                        await store.status(source_id, "partial", message)
                    logger.info("Eight Sleep 已保存快照：%d 个接口，%d 个用户，%d 个接口不可用",
                                len(payload["responses"]), len(profiles), len(issues))
                    next_poll = next_poll_at(started, time.monotonic())
                # Slow, serial backfill shares authentication and rate limits with polling.
                pending = False
                due_history = time.monotonic() >= next_history
                for uid, profile in profiles.items() if due_history else []:
                    if source_id not in await store.enabled(KIND):
                        break
                    if time.monotonic() < history_retry.get(uid, 0):
                        continue
                    try:
                        pending |= await backfill_one(api, store, source_id, uid, profile)
                    except APIError as error:
                        if error.status in (401, 429):
                            raise
                        history_retry[uid] = time.monotonic() + INTERVAL
                        await store.status(source_id, "partial", f"历史补采 {error}，稍后重试；当前快照已保存")
                if due_history:
                    next_history = time.monotonic() + (10 if pending else 300)
                await asyncio.sleep(min(2, max(0, next_poll - time.monotonic())))
            except APIError as error:
                await store.status(source_id, "error", f"Eight Sleep {error}，稍后自动重试")
                logger.warning("Eight Sleep 读取失败：%s", error)
                # Poll controls remain responsive during authentication/rate-limit backoff.
                retry_at = time.monotonic() + error.retry_after
                while time.monotonic() < retry_at:
                    await asyncio.sleep(2)
                    if source_id not in await store.enabled(KIND):
                        await store.status(source_id, "paused")
                next_poll = 0
