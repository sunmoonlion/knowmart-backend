"""分身存放的装配：每个请求用自己的数据库会话开一个服务。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.knowbot_service import KnowbotService
from app.infrastructure.repositories.knowbots import SqlKnowbotRepository


def knowbot_service(session: AsyncSession) -> KnowbotService:
    return KnowbotService(SqlKnowbotRepository(session))
