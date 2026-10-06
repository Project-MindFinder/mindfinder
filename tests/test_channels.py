"""채널 자동 구성 서비스(ChannelService) 테스트.

디스코드 길드를 최소한의 가짜 객체로 대체하고, 용도별 채널/카테고리/역할이
의도대로 생성·재사용되는지 검증한다.
"""

from __future__ import annotations

from typing import Optional

import pytest

from bot.config import Settings
from bot.db.database import Database
from bot.db.repositories import (
    AdmissionRepository,
    HospitalOptInRepository,
    HospitalRepository,
    KnowledgeVerificationRepository,
    ManagedChannelRepository,
    ManagedRoleRepository,
    UserRepository,
    ym,
)
from bot.services.access_control import AccessControlService
from bot.services.channels import (
    COMMUNITY_CHANNEL_NAME,
    NOTICE_CHANNEL_NAME,
    PANEL_CHANNEL_KEY,
    PANEL_CHANNEL_NAME,
    VERIFY_CHANNEL_NAME,
    VERIFY_CHANNEL_KEY,
    ChannelService,
)


# --------------------------------------------------------------------------- #
# 가짜 디스코드 객체
# --------------------------------------------------------------------------- #
class FakeRole:
    def __init__(self, role_id: int, name: str) -> None:
        self.id = role_id
        self.name = name


class FakeMember:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.roles: list = []

    async def add_roles(self, role, reason=None) -> None:  # noqa: ANN001
        if role not in self.roles:
            self.roles.append(role)

    async def remove_roles(self, role, reason=None) -> None:  # noqa: ANN001
        if role in self.roles:
            self.roles.remove(role)


class FakeThread:
    def __init__(self, name: str) -> None:
        self.id = id(self)
        self.name = name
        self.sent: list[str] = []
        self.members: list[int] = []

    async def send(self, content: str) -> None:
        self.sent.append(content)

    async def add_user(self, member) -> None:  # noqa: ANN001
        self.members.append(member.id)

    async def edit(self, **kwargs) -> None:  # noqa: ANN003
        self.edited = kwargs


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str, category=None) -> None:
        self.id = channel_id
        self.name = name
        self.category = category
        self.overwrites = None
        self.threads: list[FakeThread] = []

    async def edit(self, **kwargs) -> None:  # noqa: ANN003
        self.overwrites = kwargs.get("overwrites", self.overwrites)

    async def create_thread(  # noqa: ANN001, ANN003
        self, name: str, type=None, invitable=True, reason=None
    ) -> FakeThread:
        thread = FakeThread(name)
        thread.type = type
        self.threads.append(thread)
        return thread

    async def archived_threads(self, private: bool = False, limit: int = 50):  # noqa: ANN201
        """보관 스레드 조회 대역 (테스트에서는 비어 있음)."""
        for _ in ():
            yield _


class FakeCategory:
    def __init__(self, category_id: int, name: str) -> None:
        self.id = category_id
        self.name = name
        self.text_channels: list[FakeTextChannel] = []

    @property
    def channels(self) -> list[FakeTextChannel]:
        return self.text_channels


class FakeGuild:
    """카테고리/채널/역할 생성·조회를 지원하는 길드 대역."""

    def __init__(self, guild_id: int = 999) -> None:
        self.id = guild_id
        self.name = "테스트 서버"
        self.default_role = object()
        self.me = object()
        self.categories: list[FakeCategory] = []
        self.text_channels: list[FakeTextChannel] = []
        self.roles: list[FakeRole] = []
        self._counter = 1000

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    def get_channel(self, channel_id: int):
        for ch in self.text_channels:
            if ch.id == channel_id:
                return ch
        for cat in self.categories:
            if cat.id == channel_id:
                return cat
        return None

    def get_role(self, role_id: int) -> Optional[FakeRole]:
        for role in self.roles:
            if role.id == role_id:
                return role
        return None

    def get_member(self, user_id: int):
        return FakeMember(user_id)

    async def create_category(self, name: str, reason=None) -> FakeCategory:  # noqa: ANN001
        category = FakeCategory(self._next_id(), name)
        self.categories.append(category)
        return category

    async def create_text_channel(
        self, name: str, category=None, overwrites=None, topic=None, reason=None
    ) -> FakeTextChannel:  # noqa: ANN001
        channel = FakeTextChannel(self._next_id(), name, category=category)
        channel.overwrites = overwrites
        self.text_channels.append(channel)
        if category is not None:
            category.text_channels.append(channel)
        return channel

    async def create_role(self, name: str, mentionable=False, reason=None) -> FakeRole:  # noqa: ANN001
        role = FakeRole(self._next_id(), name)
        self.roles.append(role)
        return role


class FakeBot:
    """리포지토리/설정/접근제어 서비스만 가진 최소 봇 대역."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.users = UserRepository(db)
        self.hospitals = HospitalRepository(db)
        self.admissions = AdmissionRepository(db)
        self.knowledge = KnowledgeVerificationRepository(db)
        self.managed_roles = ManagedRoleRepository(db)
        self.managed_channels = ManagedChannelRepository(db)
        self.hospital_optins = HospitalOptInRepository(db)
        self.access = AccessControlService(self)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 픽스처
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "channels.db")
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
def service(bot: FakeBot) -> ChannelService:
    return ChannelService(bot)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# ensure_all
# --------------------------------------------------------------------------- #
async def test_ensure_all_creates_categories_and_channels(
    service: ChannelService, bot: FakeBot
) -> None:
    guild = FakeGuild()
    await service.ensure_all(guild)  # type: ignore[arg-type]

    category_names = {c.name for c in guild.categories}
    assert {"인증", "커뮤니티"} <= category_names

    channel_names = {c.name for c in guild.text_channels}
    assert {
        VERIFY_CHANNEL_NAME,
        PANEL_CHANNEL_NAME,
        COMMUNITY_CHANNEL_NAME,
        NOTICE_CHANNEL_NAME,
    } <= channel_names

    # 관리 대상으로 기록되어 재시작 후 재사용 가능해야 한다.
    assert await bot.managed_channels.get(guild.id, VERIFY_CHANNEL_KEY) is not None
    assert await bot.managed_channels.get(guild.id, PANEL_CHANNEL_KEY) is not None


async def test_ensure_all_is_idempotent(service: ChannelService) -> None:
    guild = FakeGuild()
    await service.ensure_all(guild)  # type: ignore[arg-type]
    first = sorted(c.id for c in guild.text_channels)
    await service.ensure_all(guild)  # type: ignore[arg-type]
    second = sorted(c.id for c in guild.text_channels)
    assert first == second


async def test_ensure_all_creates_hospital_channel_without_role(
    service: ChannelService, bot: FakeBot
) -> None:
    """병원 채널은 만들되, 역할은 사용자가 선택(opt-in)할 때만 만든다."""
    hospital = await bot.hospitals.create("서울대학교병원", "서울대학교병원")
    guild = FakeGuild()
    await service.ensure_all(guild)  # type: ignore[arg-type]

    # 채널 이름에는 병동 정보가 함께 표시된다 (예: 서울대학교병원 62병동).
    assert any(c.name == f"🏥-{hospital.name} 62병동" for c in guild.text_channels)
    # 아무도 역할을 선택하지 않았으므로 병원 역할은 생성되지 않는다.
    assert not any(r.name.startswith("MF·") for r in guild.roles)
    assert not any(r.name.endswith("인증됨") and "서울대" in r.name for r in guild.roles)


async def test_auto_create_disabled_creates_nothing(
    db: Database, bot: FakeBot
) -> None:
    bot.settings = Settings(token="test-token", auto_create_channels=False)
    service = ChannelService(bot)  # type: ignore[arg-type]
    guild = FakeGuild()
    await service.ensure_all(guild)  # type: ignore[arg-type]
    assert guild.text_channels == []
    assert guild.categories == []


async def test_managed_channels_recorded_for_reuse(
    service: ChannelService, bot: FakeBot
) -> None:
    """인증/패널 채널이 관리 대상으로 기록되어 재시작 후 재사용된다."""
    guild = FakeGuild()
    await service.ensure_all(guild)  # type: ignore[arg-type]

    verify_record = await bot.managed_channels.get(guild.id, VERIFY_CHANNEL_KEY)
    panel_record = await bot.managed_channels.get(guild.id, PANEL_CHANNEL_KEY)
    assert verify_record is not None and verify_record.name == VERIFY_CHANNEL_NAME
    assert panel_record is not None and panel_record.name == PANEL_CHANNEL_NAME
    # 기록된 ID 로 실제 채널을 찾을 수 있어야 한다.
    assert guild.get_channel(verify_record.discord_id) is not None
    assert guild.get_channel(panel_record.discord_id) is not None


async def test_period_thread_created_and_reused(
    service: ChannelService, bot: FakeBot
) -> None:
    hospital = await bot.hospitals.create("서울대학교병원", "서울대학교병원")
    guild = FakeGuild()
    thread = await service.ensure_period_thread(
        guild, hospital.id, hospital.name, "2020년 3월 ~ 2020년 5월"  # type: ignore[arg-type]
    )
    assert thread is not None and thread.name == "2020년 3월 ~ 2020년 5월"

    again = await service.ensure_period_thread(
        guild, hospital.id, hospital.name, "2020년 3월 ~ 2020년 5월"  # type: ignore[arg-type]
    )
    assert again is thread


def test_months_for_admission_includes_both_ends() -> None:
    class _Adm:
        def __init__(self, start: int, end: Optional[int]) -> None:
            self.admit_ym = start
            self.discharge_ym = end

    months = ChannelService.months_for_admission(
        _Adm(ym(2020, 3), ym(2020, 5))
    )
    assert months == [ym(2020, 3), ym(2020, 4), ym(2020, 5)]

    # 진행 중(None)이면 시작월을 포함해야 한다.
    ongoing = ChannelService.months_for_admission(_Adm(ym(2020, 3), None))
    assert ongoing[0] == ym(2020, 3)
    assert len(ongoing) >= 1


async def test_ensure_community_access_only_verified_periods(
    service: ChannelService, bot: FakeBot
) -> None:
    """'병원 역할 선택' 없이, 인증(verified)된 이력만 기간 스레드가 열린다."""
    await bot.users.ensure_user(1)
    hospital = await bot.hospitals.create("서울대학교병원", "서울대학교병원")
    child = await bot.hospitals.create("서울대학교어린이병원", "서울대학교어린이병원")

    # 2020년 3월 한 달 입원(검증 완료) — opt-in 없이도 열려야 한다.
    adm_id = await bot.admissions.add(
        user_id=1,
        hospital_id=hospital.id,
        admit_year=2020,
        admit_month=3,
        discharge_year=2020,
        discharge_month=3,
        is_ongoing=False,
    )
    await bot.admissions.set_verification(adm_id, "verified")
    # 미인증(자기 신고) 이력 — 스레드가 생기면 안 된다.
    await bot.admissions.add(
        user_id=1,
        hospital_id=child.id,
        admit_year=2021,
        admit_month=1,
        discharge_year=2021,
        discharge_month=1,
        is_ongoing=False,
    )

    guild = FakeGuild()
    await service.ensure_community_access(guild, 1)  # type: ignore[arg-type]

    thread_names = [t.name for c in guild.text_channels for t in c.threads]
    assert "2020년 3월" in thread_names
    assert "2021년 1월" not in thread_names

    # 인증된 병원 스레드에는 사용자가 추가되어 있어야 한다.
    main_channel = next(
        c for c in guild.text_channels if "서울대학교병원" in c.name
    )
    assert main_channel.threads
    assert 1 in main_channel.threads[0].members

    # 미인증 병원(어린이병원)에는 기간 스레드가 만들어지지 않아야 한다.
    child_channel = next(
        (c for c in guild.text_channels if "서울대학교어린이병원" in c.name),
        None,
    )
    assert child_channel is None or child_channel.threads == []
