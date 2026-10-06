"""DB 시드 실행 유틸리티.

봇 시작 시 호출하여 기본 데이터를 채운다.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete

from bot.data.hospitals_seed import seed_hospitals
from bot.db.database import Database
from bot.db.models import (
    Admission,
    ConsentRecord,
    HospitalOptIn,
    HospitalRequest,
    KnowledgeVerification,
    MatchChannel,
    MatchChannelMember,
    MatchPreference,
    MatchRequest,
    UserDisplayPreference,
    VerificationRequest,
)
from bot.db.repositories import AdmissionRepository, VerificationRepository

logger = logging.getLogger(__name__)

# 초기화 대상 테이블 (데이터만 삭제, 스키마/병원 목록은 유지).
#   - 유지: users, hospitals, hospital_aliases, managed_roles,
#           managed_channels, audit_logs
#   - 삭제 순서는 외래키 의존성을 고려한다 (자식 → 부모).
_RESET_MODELS = (
    MatchChannelMember,
    MatchChannel,
    MatchRequest,
    MatchPreference,
    KnowledgeVerification,
    VerificationRequest,
    Admission,
    HospitalRequest,
    HospitalOptIn,
    UserDisplayPreference,
    ConsentRecord,
)


async def seed_all(db: Database) -> None:
    """모든 시드 데이터를 등록한다."""
    await seed_hospitals(db)
    await reset_failed_verifications(db)
    logger.info("시드 데이터 등록 완료")


async def reset_verifications(db: Database) -> dict[str, int]:
    """인증 관련 데이터를 전부 초기화한다 (테스트용).

    데이터만 삭제하고 스키마와 병원 목록(hospitals/hospital_aliases),
    users, managed_roles, managed_channels, audit_logs 는 유지한다.

    Returns: 테이블명 → 삭제한 행 수.
    """
    removed: dict[str, int] = {}
    async with db.session() as session:
        for model in _RESET_MODELS:
            result = await session.execute(delete(model))
            removed[model.__tablename__] = result.rowcount or 0
        await session.commit()
    total = sum(removed.values())
    logger.info(
        "인증정보 전면 초기화 완료: 총 %d건 %s",
        total,
        {name: count for name, count in removed.items() if count},
    )
    return removed


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
