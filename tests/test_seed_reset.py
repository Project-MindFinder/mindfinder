"""인증정보 전면 초기화(reset_verifications) 유틸 테스트.

- 인증 관련 테이블(admissions/verification_requests/.../consent_records)은 비운다.
- users/hospitals/managed_roles/managed_channels/audit_logs 는 유지한다.
"""

from __future__ import annotations

import pytest

from bot.db.database import Database
from bot.db.repositories import (
    AdmissionRepository,
    ConsentRepository,
    HospitalRepository,
    ManagedChannelRepository,
    UserRepository,
    VerificationRepository,
)
from bot.db.seed import reset_verifications


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "seed.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.dispose()


async def test_reset_verifications_clears_data_but_keeps_master(db: Database) -> None:
    users = UserRepository(db)
    hospitals = HospitalRepository(db)
    admissions = AdmissionRepository(db)
    verifications = VerificationRepository(db)
    consents = ConsentRepository(db)
    managed_channels = ManagedChannelRepository(db)

    # 기준 데이터(유지 대상)
    await users.ensure_user(1)
    hospital = await hospitals.create("서울대학교병원", "서울대학교병원")
    await managed_channels.upsert(999, "category", "cat:verify", 1234, "인증")

    # 초기화 대상 데이터
    adm_id = await admissions.add(
        user_id=1,
        hospital_id=hospital.id,
        admit_year=2020,
        admit_month=3,
        discharge_year=2020,
        discharge_month=3,
        is_ongoing=False,
    )
    await admissions.set_verification(adm_id, "verified")
    await verifications.create(user_id=1, admission_id=adm_id, method="document")
    await consents.record(1, "privacy", "2026-10-06")

    removed = await reset_verifications(db)

    # 삭제된 행이 보고되어야 한다.
    assert removed.get("admissions", 0) >= 1
    assert removed.get("verification_requests", 0) >= 1
    assert removed.get("consent_records", 0) >= 1

    # 초기화 대상은 비어 있어야 한다.
    assert list(await admissions.list_by_user(1)) == []
    assert list(await verifications.list_by_user(1)) == []
    assert list(await consents.list_for_user(1)) == []

    # 유지 대상(users/hospitals/managed_channels)은 그대로 남아 있어야 한다.
    assert await users.exists(1) is True
    assert await hospitals.get_by_id(hospital.id) is not None
    assert await managed_channels.get(999, "cat:verify") is not None
