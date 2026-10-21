import asyncio
import logging
from pathlib import Path
import signal

import asyncpg

from collector.runtime import database_options
from collector.sources import COLLECTORS
from collector.store import Store

logger = logging.getLogger("collector")


async def ensure_schema(pool):
    """Idempotently verify and initialize database schema on startup."""
    init_sql = Path(__file__).resolve().parent.parent / "db" / "init.sql"
    if init_sql.exists():
        sql = init_sql.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            await conn.execute(sql)
        logger.info("PostgreSQL 数据库结构自动就绪 (init.sql 执行成功)")


async def main():
    db_opts = database_options()
    pool = None
    logger.info("正在连接 PostgreSQL (%s:%s)...", db_opts.get("host"), db_opts.get("port"))
    for attempt in range(30):
        try:
            pool = await asyncpg.create_pool(**db_opts)
            break
        except Exception as e:
            logger.info("等待数据库就绪中 (%s)... 稍后重试", e)
            await asyncio.sleep(2)

    if not pool:
        raise RuntimeError("无法连接到 PostgreSQL 数据库，请检查数据库服务状态。")

    await ensure_schema(pool)
    store = Store(pool)
    errors = {}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(name, stop.set)
        except NotImplementedError:
            pass

    async def supervise(module):
        while not stop.is_set():
            try:
                errors.pop(module.__name__, None)
                await module.run(store)
                raise RuntimeError("采集模块意外退出")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                message = str(error) or type(error).__name__
                errors[module.__name__] = message
                logger.warning("%s 异常：%s，将在 10 秒后重试", module.__name__, message)
                await asyncio.sleep(10)

    async def heartbeat():
        while not stop.is_set():
            try:
                await store.heartbeat("; ".join(errors.values()) or None)
            except Exception:
                pass
            await asyncio.sleep(5)

    tasks = [asyncio.create_task(heartbeat())]
    tasks.extend(asyncio.create_task(supervise(module)) for module in COLLECTORS)
    logger.info("CTDS 数据采集引擎已启动，正在运行 %d 个数据源模块", len(COLLECTORS))
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    asyncio.run(main())
