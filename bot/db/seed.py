"""DB 시드 실행 유틸리티.

봇 시작 시 호출하여 기본 데이터를 채운다.
"""

from __future__ import annotations

import logging

from bot.data.hospitals_seed import seed_hospitals
from bot.db.database import Database
from bot.db.repositories import AdmissionRepository, VerificationRepository

logger = logging.getLogger(__name__)


async def seed_all(db: Database) -> None:
    """모든 시드 데이터를 등록한다."""
    await seed_hospitals(db)
    await reset_failed_verifications(db)
    logger.info("시드 데이터 등록 완료")


async def reset_failed_verifications(db: Database) -> None:
    """실패한 인증 시도를 초기화해 재시도할 수 있게 한다.

    - 반려(rejected)된 입원 이력을 자기 신고 상태로 되돌린다.
    - 실패(rejected)한 자동 진위확인 기록을 삭제해 문서확인번호 unique
      제약으로 재시도가 막히지 않게 한다.
    """
    admissions = AdmissionRepository(db)
    verifications = VerificationRepository(db)
    reset = await admissions.reset_rejected()
    if reset:
        logger.info("반려 이력 초기화: %d건", reset)
    removed = await verifications.delete_rejected()
    if removed:
        logger.info("실패 인증 기록 삭제: %d건", removed)
