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

    # 1) 기본 목록 외 병원을 먼저 정리한다. 단, 대표명으로 '정규화(resolve)'되는
    #    병원은 같은 병원의 옛 이름일 수 있으므로 여기서 지우지 않고 아래에서 병합한다.
    #    (예: "서울대학교어린이병원" → "서울대학교어린이병원 7A병동" 으로 이름이 바뀐 경우)
    survivors: list = []
    for hospital in await repo.list_all():
        canonical = resolve_canonical(hospital.name)
        if canonical in default_canonicals:
            survivors.append((hospital, canonical))
            continue
        count = await admissions.count_by_hospital(hospital.id)
        if count:
            await admissions.delete_by_hospital(hospital.id)
            logger.info(
                "기본 목록 외 병원 이력 삭제(%d건): %s", count, hospital.name
            )
        await repo.delete(hospital.id)
        logger.info("기본 목록 외 병원 삭제: %s", hospital.name)

    # 2) 대표 병원을 확보하고(없으면 생성) 병동/자동검증 정보를 맞춘다.
    canonical_to_id: dict[str, int] = {}
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
        canonical_to_id[canonical] = hospital.id

        # 별칭 등록
        for alias in defaults.get(canonical, []):
            alias_norm = normalize_name(alias)
            existing = await repo.get_by_alias(alias_norm)
            if existing is None:
                await repo.add_alias(hospital.id, alias_norm)

    # 3) 같은 대표명으로 정규화되는 **중복 병원**을 대표 병원으로 병합한다.
    #    (옛 이름으로 남아 있던 병원을 지워 카테고리/채널이 중복 생성되지 않게 한다.)
    for hospital, canonical in survivors:
        target_id = canonical_to_id.get(canonical)
        if target_id is None or target_id == hospital.id:
            continue
        # 옛 병원에 딸린 이력/역할/채널/요청을 대표 병원으로 옮긴 뒤 삭제한다.
        await repo.merge_into(hospital.id, target_id)
        logger.info(
            "중복 병원 병합: %s(id=%s) → %s(id=%s)",
            hospital.name,
            hospital.id,
            canonical,
            target_id,
        )
