"""分身存放的装配：每个请求用自己的数据库会话开一个服务。"""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.expert_catalog import ExpertCatalog
from app.application.services.knowbot_service import KnowbotService
from app.application.services.market import MarketAdmin, MarketService
from app.infrastructure.external.knowbase_user_data import (
    KnowbaseLibrary,
    KnowbaseVault,
)
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository
from app.infrastructure.repositories.market import SqlMarketRepository
from core.config import get_settings


def knowbot_service(session: AsyncSession) -> KnowbotService:
    """收清单时硬查、后台定时核所绑数据都经 knowbase（0043 第 4.3 节）。"""
    return KnowbotService(
        SqlKnowbotRepository(session),
        public_market=get_settings().public_market_enabled,
        bindings=_vault(),
    )


def expert_catalog(session: AsyncSession) -> ExpertCatalog:
    """专家目录（SDD 0032）：公开开关从配置读。"""
    return ExpertCatalog(
        SqlKnowbotRepository(session),
        public_market=get_settings().public_market_enabled,
    )


def _vault() -> KnowbaseVault | None:
    """knowbase 资料库（user-data/v2）；没配就卖不了、买不了知识，后台也不核资料。"""
    if not os.environ.get("KNOWBASE_INTERNAL_URL", "").strip():
        return None
    return KnowbaseVault(KnowbaseLibrary())


def market_service(session: AsyncSession) -> MarketService:
    """供需市场（SDD 0018 第十一节）：选资料、成交建使用权经 knowbase。"""
    settings = get_settings()
    return MarketService(
        SqlMarketRepository(session),
        SqlKnowbotRepository(session),
        _vault(),
        id_hash_key=settings.publisher_id_hash_key,
        public_market=settings.public_market_enabled,
        platform_credit_code=settings.platform_publisher_credit_code,
    )


def market_admin(session: AsyncSession) -> MarketAdmin:
    return MarketAdmin(SqlMarketRepository(session), vault=_vault())
