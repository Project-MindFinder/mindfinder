"""DB 시드 실행 유틸리티.

봇 시작 시 호출하여 기본 데이터를 채운다.
"""

from __future__ import annotations

import logging

from bot.data.hospitals_seed import seed_hospitals
from bot.db.database import Database

logger = logging.getLogger(__name__)


async def seed_all(db: Database) -> None:
    """모든 시드 데이터를 등록한다."""
    await seed_hospitals(db)
    logger.info("시드 데이터 등록 완료")
