"""대표 병원 및 별칭 시드 데이터.

봇 시작 시 DB 에 없으면 자동으로 등록한다.
관리자가 자유롭게 확장할 수 있도록 별도 모듈로 분리했다.
"""

from __future__ import annotations

import logging

from bot.db.database import Database
from bot.db.repositories import HospitalRepository
from bot.utils.hospitals import (
    get_default_hospitals,
    normalize_name,
    _build_default_hospitals,
)

logger = logging.getLogger(__name__)


async def seed_hospitals(db: Database) -> None:
    """기본 병원 목록과 별칭을 DB 에 등록한다 (없는 것만)."""
    repo = HospitalRepository(db)
    defaults = _build_default_hospitals()

    for canonical in get_default_hospitals():
        normalized = normalize_name(canonical)
        hospital = await repo.get_by_normalized(normalized)
        if hospital is None:
            hospital = await repo.create(canonical, normalized)
            logger.info("시드 병원 등록: %s", canonical)

        # 별칭 등록
        for alias in defaults.get(canonical, []):
            alias_norm = normalize_name(alias)
            existing = await repo.get_by_alias(alias_norm)
            if existing is None:
                await repo.add_alias(hospital.id, alias_norm)
