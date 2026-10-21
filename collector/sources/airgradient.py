"""One complete local-API snapshot per second for the configured or auto-discovered AirGradient monitor."""

import asyncio
import http.client
import json
import logging
import math
import os
import socket
import urllib.parse
from datetime import datetime, timezone

from collector.store import Store

logger = logging.getLogger(__name__)
KIND = "airgradient"
INTERVAL = 1.0
TIMEOUT = 5.0


def _parse_candidate_url(raw_url: str) -> str:
    if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
        raw_url = f"http://{raw_url}"
    parsed = urllib.parse.urlsplit(raw_url)
    return parsed.hostname or raw_url


async def resolve_address() -> str:
    """Resolve AirGradient address via environment variable, mDNS, or fallback probing."""
    configured = os.getenv("AIRGRADIENT_HOST") or os.getenv("AIRGRADIENT_BASE_URL")
    if configured:
        return _parse_candidate_url(configured.strip())

    # Try common local mDNS hostnames
    loop = asyncio.get_running_loop()
    candidates = ["airgradient.local", "airgradient_default.local"]
    for host in candidates:
        try:
            async with asyncio.timeout(2.0):
                addresses = await loop.getaddrinfo(host, 80, family=socket.AF_INET, type=socket.SOCK_STREAM)
                if addresses and addresses[0][4][0]:
                    return addresses[0][4][0]
        except Exception:
            pass

    # Fallback to local loopback or default IP if configured in container
    return "airgradient.local"


def reject_nonfinite(value):
    raise ValueError(f"设备返回了无效 JSON 数值：{value}")


def parse_snapshot(body):
    payload = json.loads(body, parse_constant=reject_nonfinite)
    if not isinstance(payload, dict):
        raise ValueError("返回的设备数据格式无效")
    
    # Extract dynamic serial number
    serialno = str(payload.get("serialno") or "default")
    
    # Primary metric preview (CO2)
    value = payload.get("rco2")
    if type(value) not in (int, float) or not math.isfinite(value):
        value = None
    return serialno, value, payload


def fetch_snapshot(address):
    connection = http.client.HTTPConnection(address, 80, timeout=TIMEOUT)
    try:
        connection.request("GET", "/measures/current", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"AirGradient HTTP {response.status}")
        body = response.read(256 * 1024 + 1)
        if len(body) > 256 * 1024:
            raise ValueError("AirGradient 响应超过 256 KiB")
        return parse_snapshot(body)
    finally:
        connection.close()


def next_deadline(previous, now):
    deadline = previous + INTERVAL
    return deadline if deadline > now else now + INTERVAL


def merge_display(previous, current, recorded_at):
    """Retain valid numeric readings separately from the raw snapshot."""
    previous = previous or {}
    values = dict(previous.get("measurements") or {})
    times = dict(previous.get("updated_at") or {})
    for key, value in current.items():
        if type(value) not in (int, float) or not math.isfinite(value):
            continue
        values[key] = value
        times[key] = recorded_at
    return {"measurements": values, "updated_at": times}


async def restore_display(store, source_id):
    rows = await store.pool.fetch(
        """SELECT recorded_at, metadata->'display' AS display,
                  metadata->'measurements' AS measurements
           FROM samples WHERE source_id=$1 ORDER BY recorded_at DESC, id DESC LIMIT 120""",
        source_id,
    )
    state, pending = {}, []
    for row in rows:
        saved = row["display"]
        if saved:
            state = json.loads(saved) if isinstance(saved, str) else saved
            break
        pending.append(row)
    for row in reversed(pending):
        current = row["measurements"]
        current = json.loads(current) if isinstance(current, str) else current or {}
        state = merge_display(state, current, row["recorded_at"].isoformat())
    return state


async def run(store: Store):
    address = None
    source_id = "airgradient:default"
    display = {}
    streaming = False
    registered = False
    loop = asyncio.get_running_loop()
    deadline = loop.time()

    while True:
        try:
            if address is None:
                await store.status(source_id, "connecting")
                address = await resolve_address()
                logger.info("AirGradient 尝试连接地址：%s", address)

            serialno, value, payload = await asyncio.to_thread(fetch_snapshot, address)
            current_source_id = f"airgradient:{serialno}"

            if not registered or current_source_id != source_id:
                source_id = current_source_id
                display = await restore_display(store, source_id)
                await store.discover(
                    source_id,
                    f"AirGradient · {serialno[-6:] if len(serialno) >= 6 else serialno}",
                    KIND,
                    "air_quality",
                    "ppm",
                    {"serialno": serialno, "address": address},
                    enabled=True,
                )
                registered = True
                logger.info("已注册并发现 AirGradient 设备：%s", source_id)

            if source_id not in await store.enabled(KIND):
                streaming = False
                await asyncio.sleep(INTERVAL)
                deadline = loop.time()
                continue

            received = datetime.now(timezone.utc)
            display = merge_display(display, payload, received.isoformat())
            await store.write(
                source_id,
                value,
                received,
                {"measurements": payload, "address": address, "display": display},
            )
            if not streaming:
                logger.info("%s: 持续采集 1Hz 快照中（当前包含 %d 个指标）", source_id, len(payload))
                streaming = True

        except asyncio.CancelledError:
            raise
        except Exception as error:
            message = str(error) or type(error).__name__
            if isinstance(error, (socket.timeout, TimeoutError)):
                message = f"AirGradient ({address}) 连接超时，等待重试..."
            logger.warning("AirGradient 采集异常：%s", message)
            await store.status(source_id, "error", message)
            streaming = False
            # If configured via env var, keep address; otherwise reset to re-resolve
            if not os.getenv("AIRGRADIENT_HOST") and not os.getenv("AIRGRADIENT_BASE_URL"):
                address = None
            await asyncio.sleep(5)
            deadline = loop.time()
            continue

        deadline = next_deadline(deadline, loop.time())
        await asyncio.sleep(max(0, deadline - loop.time()))
