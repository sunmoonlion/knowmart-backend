"""供需市场的定时活（SDD 0018 第十一节第 5 小节；「待你决定」第 7 条）。

- sweep（每小时）：资质到期前提醒、过期的改回自用或下架、审过一年的证件照片清掉内容。
- check_offers（每 10 分钟）：待审、在架的知识条目，用到的资料在 knowbase 没了或不再
  出售的自动下架（不等卖家打开「我发布的」）。knowbase 连不上就这一轮不核。
重复跑不会多做。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.bootstrap.knowbots import market_admin
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


async def _with_admin(work: str) -> dict[str, Any]:
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session:
            return await getattr(market_admin(session), work)()
    finally:
        await postgres.shutdown()


@celery_app.task(name="app.tasks.market.sweep")
def sweep() -> dict[str, Any]:
    return asyncio.run(_with_admin("sweep"))


@celery_app.task(name="app.tasks.market.check_offers")
def check_offers() -> dict[str, Any]:
    return asyncio.run(_with_admin("check_offers"))
