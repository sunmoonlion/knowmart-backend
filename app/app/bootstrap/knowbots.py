"""分身存放的装配：每个请求用自己的数据库会话开一个服务。"""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.expert_catalog import ExpertCatalog
from app.application.services.knowbot_service import KnowbotService
from app.application.services.market import MarketAdmin, MarketService
from app.infrastructure.external.knowbase_user_data import (
    KnowbaseUserData,
    KnowbaseVault,
)
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository
from app.infrastructure.repositories.market import SqlMarketRepository
from core.config import get_settings


def knowbot_service(session: AsyncSession) -> KnowbotService:
    return KnowbotService(
        SqlKnowbotRepository(session),
        public_market=get_settings().public_market_enabled,
    )


def expert_catalog(session: AsyncSession) -> ExpertCatalog:
    """专家目录（SDD 0032）：公开开关从配置读。"""
    return ExpertCatalog(
        SqlKnowbotRepository(session),
        public_market=get_settings().public_market_enabled,
    )


def market_service(session: AsyncSession) -> MarketService:
    """供需市场（SDD 0018 第十一节）：知识的复制经 knowbase（没配就买不了知识）。"""
    vault = (
        KnowbaseVault(KnowbaseUserData())
        if os.environ.get("KNOWBASE_INTERNAL_URL", "").strip()
        else None
    )
    return MarketService(
        SqlMarketRepository(session), SqlKnowbotRepository(session), vault
    )


def market_admin(session: AsyncSession) -> MarketAdmin:
    return MarketAdmin(SqlMarketRepository(session))
