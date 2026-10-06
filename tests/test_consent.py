"""약관·개인정보 동의 서비스 및 리포지토리 테스트.

인증(입원 이력 등록/서류 제출) 전 필수 동의 절차가 올바르게 동작하는지
임시 SQLite DB 위에서 검증한다.
"""

from __future__ import annotations

import pytest

from bot.config import Settings
from bot.db.database import Database
from bot.db.repositories import ConsentRepository
from bot.services.consent import (
    DOC_PRIVACY,
    DOC_TERMS,
    ConsentService,
)


class FakeBot:
    """동의 리포지토리와 설정만 가진 최소 봇 대역."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.consents = ConsentRepository(db)


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "consent.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def settings() -> Settings:
    return Settings(token="test-token")


@pytest.fixture
def bot(db: Database, settings: Settings) -> FakeBot:
    return FakeBot(db, settings)


@pytest.fixture
def service(bot: FakeBot) -> ConsentService:
    return ConsentService(bot)  # type: ignore[arg-type]


async def test_missing_docs_initially_all(service: ConsentService) -> None:
    missing = await service.missing_docs(1)
    assert set(missing) == {DOC_PRIVACY, DOC_TERMS}
    assert await service.has_consented(1) is False


async def test_record_all_marks_consented(service: ConsentService) -> None:
    await service.record_all(1)
    assert await service.missing_docs(1) == []
    assert await service.has_consented(1) is True


async def test_record_single_doc(service: ConsentService) -> None:
    await service.record(1, [DOC_PRIVACY])
    assert await service.missing_docs(1) == [DOC_TERMS]
    assert await service.has_consented(1) is False


async def test_record_is_idempotent(service: ConsentService) -> None:
    await service.record_all(1)
    await service.record_all(1)  # 중복 기록은 무시되어야 한다
    rows = await service.bot.consents.list_for_user(1)
    assert len(rows) == 2


async def test_version_bump_requires_reconsent(
    bot: FakeBot, service: ConsentService
) -> None:
    await service.record_all(1)
    assert await service.has_consented(1) is True

    # 약관 버전을 올리면 다시 동의가 필요하다.
    object.__setattr__(bot.settings, "terms_of_service_version", "2027-01-01")
    assert await service.has_consented(1) is False
    assert await service.missing_docs(1) == [DOC_TERMS]


async def test_current_versions_from_settings(
    bot: FakeBot, service: ConsentService
) -> None:
    object.__setattr__(bot.settings, "privacy_policy_version", "v9")
    versions = service.current_versions()
    assert versions[DOC_PRIVACY] == "v9"
    assert versions[DOC_TERMS] == bot.settings.terms_of_service_version
