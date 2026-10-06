"""매칭 비공개 채널 리포지토리 테스트.

여러 명이 하나의 채널을 공유하는 흐름과 채널 재사용/멤버 보강 로직을 검증한다.
"""

from __future__ import annotations

import pytest

from bot.db.database import Database
from bot.db.repositories import HospitalRepository, MatchChannelRepository


@pytest.fixture
async def db(tmp_path):
    """임시 SQLite 파일로 초기화한 Database."""
    database = Database(tmp_path / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def repo(db: Database) -> MatchChannelRepository:
    """MatchChannelRepository."""
    return MatchChannelRepository(db)


@pytest.fixture
async def hospital_id(db: Database) -> int:
    """외래키 제약을 만족시키기 위한 실제 병원 id."""
    hospital = await HospitalRepository(db).create("테스트병원", "테스트병원")
    return hospital.id


async def test_create_registers_members(repo: MatchChannelRepository) -> None:
    channel_id = await repo.create(
        discord_channel_id=1000,
        guild_id=1,
        hospital_id=None,
        member_ids=[10, 20],
    )
    record = await repo.get_by_id(channel_id)
    assert record is not None
    assert record.discord_channel_id == 1000
    assert record.status == "open"
    members = await repo.list_members(channel_id)
    assert sorted(members) == [10, 20]


async def test_create_stores_name(repo: MatchChannelRepository) -> None:
    """매칭 채널 이름(고유번호)을 함께 저장할 수 있어야 한다."""
    channel_id = await repo.create(
        discord_channel_id=1100,
        guild_id=1,
        hospital_id=None,
        member_ids=[10, 20],
        name="매칭-12",
    )
    record = await repo.get_by_id(channel_id)
    assert record is not None
    assert record.name == "매칭-12"


async def test_set_name_updates_name(repo: MatchChannelRepository) -> None:
    channel_id = await repo.create(
        discord_channel_id=1101, guild_id=1, hospital_id=None, member_ids=[10]
    )
    await repo.set_name(channel_id, "매칭-7")
    record = await repo.get_by_id(channel_id)
    assert record is not None and record.name == "매칭-7"


async def test_set_discord_channel_updates_id_and_name(
    repo: MatchChannelRepository,
) -> None:
    channel_id = await repo.create(
        discord_channel_id=1102, guild_id=1, hospital_id=None, member_ids=[10]
    )
    await repo.set_discord_channel(channel_id, 7777, "매칭-9")
    record = await repo.get_by_id(channel_id)
    assert record is not None
    assert record.discord_channel_id == 7777
    assert record.name == "매칭-9"


async def test_create_deduplicates_members(repo: MatchChannelRepository) -> None:
    channel_id = await repo.create(
        discord_channel_id=1001,
        guild_id=1,
        hospital_id=None,
        member_ids=[10, 10, 20, 20],
    )
    members = await repo.list_members(channel_id)
    assert sorted(members) == [10, 20]


async def test_find_open_for_users_matches_shared_channel(
    repo: MatchChannelRepository, hospital_id: int
) -> None:
    await repo.create(
        discord_channel_id=2000,
        guild_id=1,
        hospital_id=hospital_id,
        member_ids=[10, 20],
    )
    found = await repo.find_open_for_users([20, 10], hospital_id=hospital_id)
    assert found is not None
    assert found.discord_channel_id == 2000


async def test_find_open_for_users_requires_all_members(
    repo: MatchChannelRepository, hospital_id: int
) -> None:
    await repo.create(
        discord_channel_id=2001,
        guild_id=1,
        hospital_id=hospital_id,
        member_ids=[10, 20],
    )
    # 30 은 채널에 없으므로 일치하는 채널이 없어야 한다.
    found = await repo.find_open_for_users([10, 30], hospital_id=hospital_id)
    assert found is None


async def test_add_member_is_idempotent(repo: MatchChannelRepository) -> None:
    channel_id = await repo.create(
        discord_channel_id=3000,
        guild_id=1,
        hospital_id=None,
        member_ids=[10],
    )
    await repo.add_member(channel_id, 30)
    await repo.add_member(channel_id, 30)  # 중복 추가는 무시
    members = await repo.list_members(channel_id)
    assert sorted(members) == [10, 30]


async def test_list_open_for_user(repo: MatchChannelRepository) -> None:
    await repo.create(
        discord_channel_id=4000, guild_id=1, hospital_id=None, member_ids=[10, 20]
    )
    await repo.create(
        discord_channel_id=4001, guild_id=1, hospital_id=None, member_ids=[20, 30]
    )
    channels = await repo.list_open_for_user(20)
    assert {c.discord_channel_id for c in channels} == {4000, 4001}


async def test_close_excludes_from_open_lists(repo: MatchChannelRepository) -> None:
    channel_id = await repo.create(
        discord_channel_id=5000, guild_id=1, hospital_id=None, member_ids=[10, 20]
    )
    await repo.close(channel_id)
    record = await repo.get_by_id(channel_id)
    assert record is not None
    assert record.status == "closed"
    assert record.closed_at is not None
    assert list(await repo.list_open_for_user(10)) == []
    assert await repo.find_open_for_users([10, 20]) is None


async def test_get_by_discord_id(repo: MatchChannelRepository) -> None:
    await repo.create(
        discord_channel_id=6000, guild_id=1, hospital_id=None, member_ids=[10]
    )
    record = await repo.get_by_discord_id(6000)
    assert record is not None
    assert record.discord_channel_id == 6000
    assert await repo.get_by_discord_id(9999) is None
