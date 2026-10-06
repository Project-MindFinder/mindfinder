"""MindFinder 봇의 핵심 클래스.

discord.py 의 commands.Bot 을 확장하여
- 설정(Settings)과 데이터베이스, 리포지토리를 보유하고
- 코그(cog)를 로드하며
- 종료 시 자원을 정리한다.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot.config import Settings
from bot.db.database import Database
from bot.db.seed import reset_verifications, seed_all
from bot.db.repositories import (
    AdmissionRepository,
    AuditRepository,
    ConsentRepository,
    HospitalOptInRepository,
    HospitalRepository,
    HospitalRequestRepository,
    KnowledgeVerificationRepository,
    ManagedChannelRepository,
    ManagedRoleRepository,
    MatchChannelRepository,
    MatchPreferenceRepository,
    MatchRequestRepository,
    UserDisplayPreferenceRepository,
    UserRepository,
    VerificationRepository,
)
from bot.services.access_control import AccessControlService
from bot.services.channels import ChannelService
from bot.services.consent import ConsentService
from bot.services.knowledge import KnowledgeService
from bot.services.matching import MatchingService
from bot.services.terms import TermsService
from bot.services.verification import VerificationService

logger = logging.getLogger(__name__)


class MindFinderBot(commands.Bot):
    """MindFinder 디스코드 봇."""

    def __init__(self, settings: Settings) -> None:
        # 개인정보 보호를 위해 인텐트를 최소화한다.
        # - guilds: 서버 정보
        # - members: 역할 부여/조회 (인증 배지)
        # - message_content: 위기 대응 키워드 감지에 필요 (선택적 기능)
        # 특권 인텐트(members, message_content)는 디스코드 개발자 포털에서
        # 활성화해야 하므로, 미활성화 환경에서는 .env 로 끌 수 있게 한다.
        intents = discord.Intents.default()
        intents.members = settings.enable_members_intent
        intents.message_content = settings.enable_message_content_intent

        super().__init__(
            command_prefix="!",  # 슬래시 커맨드 중심이지만 접두사 명령도 허용
            intents=intents,
            help_command=None,   # 자체 도움말 명령 사용
        )

        self.settings = settings
        self.db = Database(settings.database_path)

        # 리포지토리 (DB 연결 후에도 재사용 가능한 객체)
        # 주의: discord.Client 는 users/guilds 등 읽기 전용 프로퍼티를 이미 정의하므로
        #       충돌을 피하기 위해 리포지토리 속성에는 user_repo 처럼 접미사를 붙인다.
        self.user_repo = UserRepository(self.db)
        self.hospitals = HospitalRepository(self.db)
        self.admissions = AdmissionRepository(self.db)
        self.verifications = VerificationRepository(self.db)
        self.match_prefs = MatchPreferenceRepository(self.db)
        self.match_requests = MatchRequestRepository(self.db)
        self.match_channels = MatchChannelRepository(self.db)
        self.audit = AuditRepository(self.db)
        # 접근 제어(역할/채널) 및 지식기반 인증
        self.managed_roles = ManagedRoleRepository(self.db)
        self.managed_channels = ManagedChannelRepository(self.db)
        self.knowledge = KnowledgeVerificationRepository(self.db)
        # 약관/개인정보 동의
        self.consents = ConsentRepository(self.db)
        # 사용자 '병원 추가' 요청
        self.hospital_requests = HospitalRequestRepository(self.db)
        # 사용자가 선택한 병원 역할(opt-in) 상태
        self.hospital_optins = HospitalOptInRepository(self.db)
        # 사용자가 선택한 입원 정보 노출 범위(역할태그/프로필)
        self.display_prefs = UserDisplayPreferenceRepository(self.db)

        # 도메인 서비스 (UI 비의존). 슬래시 커맨드와 상시 패널이 공유한다.
        self.verification_service = VerificationService(self)
        self.matching_service = MatchingService(self)
        self.access = AccessControlService(self)
        self.knowledge_service = KnowledgeService(self)
        self.channel_service = ChannelService(self)
        self.consent_service = ConsentService(self)
        self.terms_service = TermsService(self)

    async def setup_hook(self) -> None:
        """봇 로그인 직후 실행. DB 연결, 시드, 코그 로드."""
        await self.db.connect()
        await seed_all(self.db)
        # [테스트용] 인증정보 전면 초기화 (RESET_VERIFICATIONS_ON_START=true 일 때).
        # 운영에서는 반드시 false 로 두어야 한다.
        if self.settings.reset_verifications_on_start:
            removed = await reset_verifications(self.db)
            logger.warning(
                "인증정보 전면 초기화 실행(RESET_VERIFICATIONS_ON_START=true): %s",
                {k: v for k, v in removed.items() if v},
            )
        await self._load_cogs()
        await self._sync_commands()
        # 설정된 서버(GUILD_ID)에서만 동작한다.
        if self.settings.guild_id:
            logger.info(
                "운영 서버 제한: guild_id=%s 에서만 동작합니다.", self.settings.guild_id
            )
        else:
            logger.warning(
                "GUILD_ID 가 설정되지 않아 모든 서버에서 동작합니다. "
                "운영 시에는 .env 에 GUILD_ID 를 지정하세요."
            )

    def _is_allowed_guild(self, guild: discord.Guild | None) -> bool:
        """이 봇이 동작해야 하는 서버인지 확인한다 (GUILD_ID 기준)."""
        if guild is None:
            return False
        if not self.settings.guild_id:
            return True
        return guild.id == self.settings.guild_id

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        """허용되지 않은 서버에 초대되면 즉시 나간다."""
        if self._is_allowed_guild(guild):
            return
        logger.warning(
            "허용되지 않은 서버 초대 → 자동 퇴장: %s (ID: %s)", guild.name, guild.id
        )
        try:
            await guild.leave()
        except discord.HTTPException:
            logger.exception("서버 자동 퇴장 실패: guild=%s", guild.id)

    async def _load_cogs(self) -> None:
        """cogs 디렉터리의 확장 모듈을 로드한다."""
        for extension in (
            "bot.cogs.general",
            "bot.cogs.verification",
            "bot.cogs.matching",
            "bot.cogs.community",
            "bot.cogs.crisis",
            "bot.cogs.panel",
            "bot.cogs.knowledge",
        ):
            try:
                await self.load_extension(extension)
                logger.info("코그 로드 완료: %s", extension)
            except Exception:  # noqa: BLE001 - 개별 코그 실패가 전체를 막지 않도록
                logger.exception("코그 로드 실패: %s", extension)

    async def _sync_commands(self) -> None:
        """슬래시 커맨드를 동기화한다.

        GUILD_ID 가 설정되어 있으면 해당 길드에만 즉시 동기화(개발 편의),
        없으면 전역 동기화(반영에 시간이 걸릴 수 있음).
        """
        try:
            if self.settings.guild_id:
                guild = discord.Object(id=self.settings.guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("길드(%s) 슬래시 커맨드 %d개 동기화", self.settings.guild_id, len(synced))
            else:
                synced = await self.tree.sync()
                logger.info("전역 슬래시 커맨드 %d개 동기화", len(synced))
        except Exception:  # noqa: BLE001
            logger.exception("슬래시 커맨드 동기화 실패")

    async def on_ready(self) -> None:
        """봇이 준비되면 로그를 남긴다."""
        # 허용되지 않은 서버(재시작 전 초대된 곳 포함)에서 나간다.
        for guild in list(self.guilds):
            if not self._is_allowed_guild(guild):
                logger.warning(
                    "허용되지 않은 서버 → 자동 퇴장: %s (ID: %s)", guild.name, guild.id
                )
                try:
                    await guild.leave()
                except discord.HTTPException:
                    logger.exception("서버 자동 퇴장 실패: guild=%s", guild.id)
        logger.info(
            "봇 로그인 완료: %s (ID: %s) / 길드 %d개",
            self.user, getattr(self.user, "id", "?"), len(self.guilds),
        )
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="폐쇄병동 동료 찾기",
            )
        )

    async def close(self) -> None:
        """종료 시 DB 엔진을 정리하고 부모 close 를 호출한다."""
        await self.db.dispose()
        await super().close()
