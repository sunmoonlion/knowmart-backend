"""供需市场的定时活（SDD 0018 第十一节第 5 小节）。

资质到期前提醒、过期的改回自用或下架、审过一年的证件照片清掉内容。
每小时跑一次；重复跑不会多做。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.bootstrap.knowbots import market_admin
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


async def _sweep() -> dict[str, Any]:
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session:
            return await market_admin(session).sweep()
    finally:
        await postgres.shutdown()


@celery_app.task(name="app.tasks.market.sweep")
def sweep() -> dict[str, Any]:
    return asyncio.run(_sweep())
