"""대표 병원 및 별칭 시드 데이터.

봇 시작 시 DB 에 없으면 자동으로 등록한다.
**병동/자동검증 여부는 병원명 하드코딩이 아니라 DB 컬럼으로 저장한다.**
관리자가 자유롭게 확장할 수 있도록 별도 모듈로 분리했다.
"""

from __future__ import annotations

import logging

from bot.db.database import Database
from bot.db.repositories import AdmissionRepository, HospitalRepository
from bot.utils.hospitals import (
    get_default_hospitals,
    normalize_name,
    resolve_canonical,
    _build_default_hospitals,
)

logger = logging.getLogger(__name__)

# 대표 병원별 부가 정보(병동, 자동 진위확인 지원 여부)를 DB 에 저장한다.
# 병원명 문자열에 의존하지 않도록 여기(시드)에서만 정의한다.
_DEFAULT_HOSPITAL_META: dict[str, dict] = {
    "서울대학교어린이병원 7A병동": {"ward": "7A병동", "auto_verify": True},
}


async def seed_hospitals(db: Database) -> None:
    """기본 병원 목록과 별칭을 DB 에 등록하고, 기본 목록 외 병원을 정리한다.

    기본 목록(현재 서울대학교어린이병원 7A병동)에 없는 병원은
    **입원 이력이 없는 경우에만** DB 에서 제거한다.
    """
    repo = HospitalRepository(db)
    admissions = AdmissionRepository(db)
    defaults = _build_default_hospitals()
    default_canonicals = set(get_default_hospitals())

    for canonical in get_default_hospitals():
        normalized = normalize_name(canonical)
        meta = _DEFAULT_HOSPITAL_META.get(canonical, {})
        hospital = await repo.get_by_normalized(normalized)
        if hospital is None:
            hospital = await repo.create(
                canonical,
                normalized,
                ward=meta.get("ward"),
                auto_verify=bool(meta.get("auto_verify", False)),
            )
            logger.info("시드 병원 등록: %s", canonical)
        else:
            # 병동/자동검증 정보가 바뀌었으면 최신값으로 맞춘다.
            if hospital.ward != meta.get("ward"):
                await repo.set_ward(hospital.id, meta.get("ward"))
            if hospital.auto_verify != bool(meta.get("auto_verify", False)):
                await repo.set_auto_verify(
                    hospital.id, bool(meta.get("auto_verify", False))
                )

        # 별칭 등록
        for alias in defaults.get(canonical, []):
            alias_norm = normalize_name(alias)
            existing = await repo.get_by_alias(alias_norm)
            if existing is None:
                await repo.add_alias(hospital.id, alias_norm)

    # 기본 목록 외 병원은 모두 정리한다 (관련 이력도 함께 삭제).
    for hospital in await repo.list_all():
        if resolve_canonical(hospital.name) in default_canonicals:
            continue
        count = await admissions.count_by_hospital(hospital.id)
        if count:
            await admissions.delete_by_hospital(hospital.id)
            logger.info(
                "기본 목록 외 병원 이력 삭제(%d건): %s", count, hospital.name
            )
        await repo.delete(hospital.id)
        logger.info("기본 목록 외 병원 삭제: %s", hospital.name)
