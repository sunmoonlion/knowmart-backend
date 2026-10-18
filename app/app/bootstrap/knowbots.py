"""分身存放的装配：每个请求用自己的数据库会话开一个服务。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.expert_catalog import ExpertCatalog
from app.application.services.knowbot_service import KnowbotService
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository
from core.config import get_settings


def knowbot_service(session: AsyncSession) -> KnowbotService:
    return KnowbotService(SqlKnowbotRepository(session))


def expert_catalog(session: AsyncSession) -> ExpertCatalog:
    """专家目录（SDD 0032）：公开开关从配置读。"""
    return ExpertCatalog(
        SqlKnowbotRepository(session),
        public_market=get_settings().public_market_enabled,
    )
