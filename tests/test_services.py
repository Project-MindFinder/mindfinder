"""서비스 계층(VerificationService, MatchingService) 단위 테스트.

UI(슬래시 커맨드/패널)와 무관하게 핵심 도메인 로직을 검증한다.
실제 리포지토리를 임시 SQLite DB 위에 올리고, 봇 객체는 최소한의 가짜로 대체한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.config import Settings
from bot.db.database import Database
from bot.db.repositories import (
    AdmissionRepository,
    AuditRepository,
    ConsentRepository,
    HospitalRepository,
    KnowledgeVerificationRepository,
    MatchChannelRepository,
    MatchPreferenceRepository,
    MatchRequestRepository,
    UserRepository,
    VerificationRepository,
)
from bot.services.access_control import AccessControlService
from bot.services.matching import MatchingService
from bot.services.verification import VerificationService
from bot.utils.certificate import (
    AdmissionEntry,
    CertificateParseError,
    ParsedCertificate,
)
from bot.utils.periods import PeriodError
from bot.utils.snuh_truth import TruthResult, TruthStatus


# --------------------------------------------------------------------------- #
# 가짜 봇 / 디스코드 객체
# --------------------------------------------------------------------------- #
class FakeBot:
    """리포지토리와 설정만 가진 최소 봇 대역."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.user_repo = UserRepository(db)
        self.hospitals = HospitalRepository(db)
        self.admissions = AdmissionRepository(db)
        self.verifications = VerificationRepository(db)
        self.match_prefs = MatchPreferenceRepository(db)
        self.match_requests = MatchRequestRepository(db)
        self.match_channels = MatchChannelRepository(db)
        self.audit = AuditRepository(db)
        # 접근 제어/지식기반 인증/약관 동의 (매칭 게이팅 및 동의 확인용)
        self.knowledge = KnowledgeVerificationRepository(db)
        self.consents = ConsentRepository(db)
        self.access = AccessControlService(self)  # type: ignore[arg-type]


class FakeMember:
    """권한 오버라이트 키/멘션에 쓰이는 최소 멤버 대역."""

    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.bot = False
        self.display_name = f"user{user_id}"


class FakeTextChannel:
    """매칭 비공개 채널 대역."""

    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.sent: list[dict] = []

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    async def send(self, **kwargs) -> None:  # noqa: ANN003
        self.sent.append(kwargs)

    async def set_permissions(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None


class FakeGuild:
    """채널 생성/조회만 지원하는 길드 대역."""

    def __init__(self, guild_id: int = 999) -> None:
        self.id = guild_id
        self.default_role = object()
        self.me = object()
        self._channels: dict[int, FakeTextChannel] = {}

    def get_member(self, user_id: int) -> FakeMember:
        return FakeMember(user_id)

    def get_channel(self, channel_id: int) -> Optional[FakeTextChannel]:
        return self._channels.get(channel_id)

    def get_role(self, role_id: int):
        return None

    async def create_text_channel(
        self, name: str, category=None, overwrites=None, topic=None, reason=None
    ) -> FakeTextChannel:
        channel = FakeTextChannel(channel_id=1000 + len(self._channels), name=name)
        self._channels[channel.id] = channel
        return channel


# --------------------------------------------------------------------------- #
# 픽스처
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def settings() -> Settings:
    # 매칭 게이팅(서류 인증자 한정) 테스트가 기존 흐름을 유지하도록 기본 허용한다.
    return Settings(token="test-token", allow_unverified_matching=True)


@pytest.fixture
def bot(db: Database, settings: Settings) -> FakeBot:
    return FakeBot(db, settings)


@pytest.fixture
def vservice(bot: FakeBot) -> VerificationService:
    return VerificationService(bot)  # type: ignore[arg-type]


@pytest.fixture
def mservice(bot: FakeBot) -> MatchingService:
    return MatchingService(bot)  # type: ignore[arg-type]


def _fake_cert(
    *,
    document_no: Optional[str] = "DOC-123",
    hospital_key: Optional[str] = "snuh",
    is_psychiatric: bool = True,
) -> ParsedCertificate:
    """정신건강의학과 입원 사실확인서 파싱 결과 대역."""
    return ParsedCertificate(
        document_no=document_no,
        issue_no="ISSUE-1",
        hospital_key=hospital_key,
        hospital_name="서울대학교병원",
        departments=["정신건강의학과"],
        is_psychiatric=is_psychiatric,
        psychiatric_departments=["정신건강의학과"] if is_psychiatric else [],
        years=[2020],
        month_days=[(3, 1), (5, 1)],
    )


# --------------------------------------------------------------------------- #
# VerificationService
# --------------------------------------------------------------------------- #
async def test_register_admission_creates_and_resolves_hospital(
    vservice: VerificationService, bot: FakeBot
) -> None:
    admission_id, period, hospital_name = await vservice.register_admission(
        user_id=1,
        hospital_name="서울대어린이병원",  # 별칭 → 대표 병원으로 정규화
        start_year=2020,
        start_month=3,
        end_year=2020,
        end_month=5,
        is_ongoing=False,
    )
    assert admission_id > 0
    assert period.text() == "2020년 3월 ~ 2020년 5월"
    assert hospital_name == "서울대학교어린이병원"

    rows = await bot.admissions.list_by_user(1)
    assert len(rows) == 1
    assert rows[0].verification == "self_reported"
    # 관계 즉시 로딩 확인 (세션 종료 후에도 접근 가능해야 함)
    assert rows[0].hospital.name == "서울대학교어린이병원"


async def test_register_admission_invalid_period_raises(
    vservice: VerificationService,
) -> None:
    with pytest.raises(PeriodError):
        await vservice.register_admission(
            user_id=1,
            hospital_name="국립정신건강센터",
            start_year=2020,
            start_month=13,  # 잘못된 월
            end_year=None,
            end_month=None,
            is_ongoing=False,
        )


async def test_auto_verify_disabled_returns_orange(
    vservice: VerificationService, settings: Settings
) -> None:
    object.__setattr__(settings, "snuh_truth_enabled", False)
    result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")
    assert result.approved is False
    assert result.color == "orange"
    assert result.title == "기능 비활성화"


async def test_auto_verify_rejects_non_pdf(vservice: VerificationService) -> None:
    result = await vservice.auto_verify_pdf(1, "cert.txt", b"hello")
    assert result.approved is False
    assert result.title == "형식 오류"


async def test_auto_verify_rejects_oversized(vservice: VerificationService) -> None:
    big = b"%PDF" + b"0" * (11 * 1024 * 1024)
    result = await vservice.auto_verify_pdf(1, "cert.pdf", big)
    assert result.approved is False
    assert result.title == "용량 초과"


async def test_auto_verify_parse_error(vservice: VerificationService) -> None:
    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        side_effect=CertificateParseError("해석 불가"),
    ):
        result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")
    assert result.approved is False
    assert result.title == "해석 실패"


async def test_auto_verify_unsupported_hospital(vservice: VerificationService) -> None:
    cert = _fake_cert(hospital_key="other", is_psychiatric=True)
    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        return_value=cert,
    ):
        result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")
    assert result.approved is False
    assert result.title == "지원하지 않는 병원"


async def test_auto_verify_non_psychiatric(vservice: VerificationService) -> None:
    cert = _fake_cert(is_psychiatric=False)
    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        return_value=cert,
    ):
        result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")
    assert result.approved is False
    assert result.title == "정신과 이력 아님"


async def test_auto_verify_missing_document_no(vservice: VerificationService) -> None:
    cert = _fake_cert(document_no=None)
    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        return_value=cert,
    ):
        result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")
    assert result.approved is False
    assert result.title == "문서번호 없음"


async def test_auto_verify_creates_multiple_admissions(
    vservice: VerificationService, bot: FakeBot
) -> None:
    """문서에 여러 건의 입원 이력이 있으면 모두 등록한다."""
    cert = _fake_cert()
    cert.admissions = [
        AdmissionEntry(
            admit_year=2020,
            admit_month=3,
            discharge_year=2020,
            discharge_month=5,
            department="정신건강의학과",
        ),
        AdmissionEntry(
            admit_year=2021,
            admit_month=1,
            discharge_year=2021,
            discharge_month=2,
            department="정신건강의학과",
        ),
    ]
    truth = TruthResult(
        status=TruthStatus.VALID, message="정상 발급 문서", document_no="DOC-123"
    )
    fake_client = MagicMock()
    fake_client.verify = AsyncMock(return_value=truth)

    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        return_value=cert,
    ), patch(
        "bot.services.verification.SnuhTruthClient", return_value=fake_client
    ):
        result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")

    assert result.approved is True
    rows = await bot.admissions.list_by_user(1)
    assert len(rows) == 2
    # 여러 건의 입원 이력이 각각 개별적으로 검증 완료 처리된다.
    verified = [r for r in rows if r.verification == "verified"]
    assert len(verified) == 2
    periods = sorted((r.admit_year, r.admit_month) for r in verified)
    assert periods == [(2020, 3), (2021, 1)]


async def test_auto_verify_approved_creates_verified_admission(
    vservice: VerificationService, bot: FakeBot
) -> None:
    cert = _fake_cert()
    truth = TruthResult(
        status=TruthStatus.VALID, message="정상 발급 문서", document_no="DOC-123"
    )
    fake_client = MagicMock()
    fake_client.verify = AsyncMock(return_value=truth)

    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        return_value=cert,
    ), patch(
        "bot.services.verification.SnuhTruthClient", return_value=fake_client
    ):
        result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")

    assert result.approved is True
    assert result.color == "green"
    assert result.document_no == "DOC-123"

    rows = await bot.admissions.list_by_user(1)
    assert len(rows) == 1
    assert rows[0].verification == "verified"

    # 동일 문서번호 재검증은 중복으로 차단
    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        return_value=cert,
    ):
        dup = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")
    assert dup.approved is False
    assert dup.title == "중복 문서"


async def test_auto_verify_invalid_marks_rejected(
    vservice: VerificationService, bot: FakeBot
) -> None:
    cert = _fake_cert()
    truth = TruthResult(
        status=TruthStatus.INVALID, message="인터넷 발급 문서가 아님", document_no="DOC-123"
    )
    fake_client = MagicMock()
    fake_client.verify = AsyncMock(return_value=truth)

    with patch(
        "bot.services.verification.certificate_utils.parse_certificate",
        return_value=cert,
    ), patch(
        "bot.services.verification.SnuhTruthClient", return_value=fake_client
    ):
        result = await vservice.auto_verify_pdf(1, "cert.pdf", b"%PDF-1.4")

    assert result.approved is False
    assert result.color == "red"
    # 실패한 시도는 재시도할 수 있도록 자기 신고 상태로 초기화된다.
    rows = await bot.admissions.list_by_user(1)
    assert rows[0].verification == "self_reported"
    # 실패한 자동 인증 기록은 삭제되어 재시도를 막지 않는다.
    assert await bot.verifications.find_by_document_no("DOC-123") is None


async def test_submit_and_approve_flow(
    vservice: VerificationService, bot: FakeBot
) -> None:
    admission_id, _, _ = await vservice.register_admission(
        user_id=1,
        hospital_name="국립정신건강센터",
        start_year=2019,
        start_month=1,
        end_year=2019,
        end_month=3,
        is_ongoing=False,
    )
    request_id = await vservice.submit_proof(1, admission_id)

    req = await vservice.approve(request_id, reviewer_id=99)
    assert req is not None
    rows = await bot.admissions.list_by_user(1)
    assert rows[0].verification == "verified"

    # 이미 처리된 심사는 재승인 불가
    assert await vservice.approve(request_id, reviewer_id=99) is None


async def test_submit_and_reject_flow(
    vservice: VerificationService, bot: FakeBot
) -> None:
    admission_id, _, _ = await vservice.register_admission(
        user_id=2,
        hospital_name="국립정신건강센터",
        start_year=2019,
        start_month=1,
        end_year=2019,
        end_month=3,
        is_ongoing=False,
    )
    request_id = await vservice.submit_proof(2, admission_id)
    req = await vservice.reject(request_id, reviewer_id=99, note="서류 불명확")
    assert req is not None
    rows = await bot.admissions.list_by_user(2)
    assert rows[0].verification == "rejected"


# --------------------------------------------------------------------------- #
# MatchingService
# --------------------------------------------------------------------------- #
async def _seed_pair(bot: FakeBot, mservice: MatchingService) -> None:
    """같은 병원·겹치는 기간의 두 사용자를 등록한다.

    매칭은 서류 인증(verified) 이력끼리만 허용되므로, 시드 이력을
    등록 직후 verified 로 표시한다.
    """
    for user_id in (1, 2):
        await mservice.set_matching(user_id, enabled=True)
    service = VerificationService(bot)  # type: ignore[arg-type]
    adm1, _period1, _name1 = await service.register_admission(
        user_id=1,
        hospital_name="서울대학교병원",
        start_year=2020,
        start_month=3,
        end_year=2020,
        end_month=6,
        is_ongoing=False,
    )
    adm2, _period2, _name2 = await service.register_admission(
        user_id=2,
        hospital_name="서울대학교병원",
        start_year=2020,
        start_month=4,
        end_year=2020,
        end_month=8,
        is_ongoing=False,
    )
    await bot.admissions.set_verification(adm1, "verified")
    await bot.admissions.set_verification(adm2, "verified")


async def test_set_matching_toggles(bot: FakeBot, mservice: MatchingService) -> None:
    await mservice.set_matching(1, True)
    assert await bot.match_prefs.is_enabled(1) is True
    await mservice.set_matching(1, False)
    assert await bot.match_prefs.is_enabled(1) is False


async def test_find_candidates_requires_history(mservice: MatchingService) -> None:
    with pytest.raises(ValueError):
        await mservice.find_candidates(1)


async def test_find_candidates_matches_overlap(
    bot: FakeBot, mservice: MatchingService
) -> None:
    await _seed_pair(bot, mservice)
    matched, total = await mservice.find_candidates(1)
    assert total == 1
    assert len(matched) == 1
    assert matched[0].user_id == 2
    my_adm, other_adm = matched[0].pairs[0]
    assert my_adm.hospital.name == "서울대학교병원"
    assert other_adm.hospital.name == "서울대학교병원"


async def test_find_candidates_excludes_pref_off(
    bot: FakeBot, mservice: MatchingService
) -> None:
    await _seed_pair(bot, mservice)
    await mservice.set_matching(2, enabled=False)
    matched, total = await mservice.find_candidates(1)
    assert total == 1  # 겹치는 사람은 있지만
    assert matched == []  # 매칭을 원하지 않으므로 후보에서 제외


async def test_request_match_dedupes_pending(
    bot: FakeBot, mservice: MatchingService
) -> None:
    await _seed_pair(bot, mservice)
    matched, _ = await mservice.find_candidates(1)
    my_adm, _other = matched[0].pairs[0]

    request_id, already = await mservice.request_match(1, 2, my_adm.id)
    assert request_id > 0
    assert already is False

    again_id, already2 = await mservice.request_match(1, 2, my_adm.id)
    assert again_id == 0
    assert already2 is True


async def test_reject_match(bot: FakeBot, mservice: MatchingService) -> None:
    await _seed_pair(bot, mservice)
    matched, _ = await mservice.find_candidates(1)
    my_adm, _ = matched[0].pairs[0]
    request_id, _ = await mservice.request_match(1, 2, my_adm.id)

    assert await mservice.reject_match(request_id, target_id=2) is True
    # 이미 처리된 신청은 다시 거절 불가
    assert await mservice.reject_match(request_id, target_id=2) is False
    # 대상자가 아니면 거절 불가
    assert await mservice.reject_match(request_id, target_id=1) is False


async def test_accept_match_invalid_request(mservice: MatchingService) -> None:
    guild = FakeGuild()
    result = await mservice.accept_match(999, target_id=2, guild=guild)  # type: ignore[arg-type]
    assert result.error == "invalid"
    assert result.channel is None


async def test_accept_match_creates_channel(
    bot: FakeBot, mservice: MatchingService
) -> None:
    await _seed_pair(bot, mservice)
    matched, _ = await mservice.find_candidates(1)
    my_adm, _ = matched[0].pairs[0]
    request_id, _ = await mservice.request_match(1, 2, my_adm.id)

    guild = FakeGuild()
    result = await mservice.accept_match(request_id, target_id=2, guild=guild)  # type: ignore[arg-type]
    assert result.error is None
    assert result.channel is not None
    assert result.hospital_name == "서울대학교병원"

    # DB 에 채널/멤버가 등록되어야 한다.
    record = await bot.match_channels.get_by_discord_id(result.channel.id)
    assert record is not None
    members = await bot.match_channels.list_members(record.id)
    assert sorted(members) == [1, 2]

    # 내 채널 목록에 노출 (FakeTextChannel 이 isinstance 검사를 통과하도록 패치)
    with patch("bot.services.matching.discord.TextChannel", FakeTextChannel):
        channels = await mservice.my_open_channels(guild, 1)  # type: ignore[arg-type]
    assert [c.id for c in channels] == [result.channel.id]


async def test_mask_name_masks_middle(mservice: MatchingService) -> None:
    guild = FakeGuild()
    assert MatchingService.mask_name(guild, 123) == "u*****3"  # user123 마스킹
    assert MatchingService.mask_name(None, 1) == "익명 동료"
