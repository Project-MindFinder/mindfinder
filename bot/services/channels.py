"""채널 자동 구성 서비스.

용도별로 채널을 분리하고, 인증 단계(역할)에 따라 접근 권한을 부여한다.

구성:
  - 인증 카테고리
      · 🔐-인증        : 아무나 접근 (자기 신고·서류 인증 패널)
      · 🧭-패널        : 아무나 접근 (안내·도움말 패널)
  - 커뮤니티 카테고리
      · 💬-전체-커뮤니티 : 인증(1단계) 이상
      · 📢-공지         : 인증(1단계) 이상
  - 매칭 카테고리
      · 🧩-매칭        : 인증(2단계·서류 인증) 이상
  - 병원-커뮤니티 카테고리
      · 🏥-<병원명>     : 해당 병원 인증자만 (역할 기반)
        └ 기간 스레드 (예: "2020년 3월 ~ 2020년 5월")

권한은 역할 기반이라, 인증/병원 인증을 받으면 역할만 부여하면 된다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Sequence

import discord

from bot.utils.periods import current_ym, format_ym

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

# 카테고리 키/이름
VERIFY_CATEGORY_KEY = "cat:verify"
VERIFY_CATEGORY_NAME = "인증"
COMMUNITY_CATEGORY_KEY = "cat:community"
COMMUNITY_CATEGORY_NAME = "커뮤니티"
HOSPITAL_CATEGORY_KEY = "cat:hospital"
HOSPITAL_CATEGORY_NAME = "병원-커뮤니티"

# 채널 키/이름
VERIFY_CHANNEL_KEY = "ch:verify"
VERIFY_CHANNEL_NAME = "🔐-인증"
PANEL_CHANNEL_KEY = "ch:panel"
PANEL_CHANNEL_NAME = "🧭-패널"
COMMUNITY_CHANNEL_KEY = "ch:community"
COMMUNITY_CHANNEL_NAME = "💬-전체-커뮤니티"
NOTICE_CHANNEL_KEY = "ch:notice"
NOTICE_CHANNEL_NAME = "📢-공지"
# 약관·개인정보 전문 게시 채널 (1단계 이상이면 열람 가능)
TERMS_CHANNEL_KEY = "ch:terms"
TERMS_CHANNEL_NAME = "📜-약관-개인정보"
MATCH_CATEGORY_KEY = "cat:match"
MATCH_CATEGORY_NAME = "매칭"
MATCH_CHANNEL_KEY = "ch:match"
MATCH_CHANNEL_NAME = "🧩-매칭"

# 관리자 전용 (병원 추가 요청 심사 등)
ADMIN_CATEGORY_KEY = "cat:admin"
ADMIN_CATEGORY_NAME = "관리자"
ADMIN_CHANNEL_KEY = "ch:admin"
ADMIN_CHANNEL_NAME = "🛡️-관리자"

_MAX_NAME = 100


def _clip(text: str) -> str:
    return text[:_MAX_NAME]


class ChannelService:
    """용도별 채널/카테고리 생성 및 권한 동기화."""

    def __init__(self, bot: "MindFinderBot") -> None:
        self.bot = bot

    # ---------- 권한 계산 ----------

    def _admin_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        role_id = self.bot.settings.admin_role_id
        if role_id:
            return guild.get_role(role_id)
        return None

    def _overwrites(
        self,
        guild: discord.Guild,
        *,
        public: bool = False,
        view_roles: Sequence[Optional[discord.Role]] = (),
        deny_roles: Sequence[Optional[discord.Role]] = (),
    ) -> dict:
        """채널 권한 오버라이트를 만든다.

        public=True 면 @everyone 이 볼 수 있다. 그 외에는 @everyone 을 차단하고
        view_roles 에게만 열어준다. deny_roles 는 명시적으로 차단한다.
        """
        ow: dict = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=public, send_messages=public, read_message_history=public
            )
        }
        for role in view_roles:
            if role is not None:
                ow[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )
        for role in deny_roles:
            if role is not None:
                ow[role] = discord.PermissionOverwrite(view_channel=False)
        admin = self._admin_role(guild)
        if admin is not None:
            ow[admin] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        return ow

    # ---------- 카테고리/채널 확보 ----------

    async def _ensure_category(
        self, guild: discord.Guild, key: str, name: str
    ) -> Optional[discord.CategoryChannel]:
        record = await self.bot.managed_channels.get(guild.id, key)
        if record is not None:
            channel = guild.get_channel(record.discord_id)
            if isinstance(channel, discord.CategoryChannel):
                return channel
        for channel in guild.categories:
            if channel.name == name:
                await self.bot.managed_channels.upsert(
                    guild.id, "category", key, channel.id, channel.name
                )
                return channel
        if not self.bot.settings.auto_create_channels:
            return None
        try:
            category = await guild.create_category(
                name=name, reason="MindFinder 채널 자동 구성"
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("카테고리 생성 실패: guild=%s key=%s", guild.id, key)
            return None
        await self.bot.managed_channels.upsert(
            guild.id, "category", key, category.id, category.name
        )
        logger.info("카테고리 생성: guild=%s key=%s", guild.id, key)
        return category

    async def _ensure_text(
        self,
        guild: discord.Guild,
        key: str,
        name: str,
        category: Optional[discord.CategoryChannel],
        overwrites: dict,
        *,
        sync_permissions: bool = True,
    ) -> Optional[discord.TextChannel]:
        record = await self.bot.managed_channels.get(guild.id, key)
        channel: Optional[discord.TextChannel] = None
        if record is not None:
            found = guild.get_channel(record.discord_id)
            if isinstance(found, discord.TextChannel):
                channel = found
        if channel is None and category is not None:
            for ch in category.text_channels:
                if ch.name == name:
                    channel = ch
                    break
        if channel is not None:
            await self.bot.managed_channels.upsert(
                guild.id, "channel", key, channel.id, channel.name
            )
            # 권한 동기화 (역할 변경 반영)
            if sync_permissions:
                try:
                    await channel.edit(overwrites=overwrites)
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("채널 권한 동기화 실패: channel=%s", channel.id)
            return channel
        if not self.bot.settings.auto_create_channels:
            return None
        try:
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                reason="MindFinder 채널 자동 구성",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("채널 생성 실패: guild=%s key=%s", guild.id, key)
            return None
        await self.bot.managed_channels.upsert(
            guild.id, "channel", key, channel.id, channel.name
        )
        logger.info("채널 생성: guild=%s key=%s", guild.id, key)
        return channel

    # ---------- 병원 채널 ----------

    @staticmethod
    def hospital_channel_key(hospital_id: int) -> str:
        return f"ch:hospital:{hospital_id}"

    @staticmethod
    def hospital_channel_name(hospital_name: str) -> str:
        return _clip(f"🏥-{hospital_name}")

    async def ensure_hospital_channel(
        self, guild: discord.Guild, hospital_id: int, hospital_name: str
    ) -> Optional[discord.TextChannel]:
        """병원 전용 채널을 확보한다 (해당 병원 인증자 역할만 접근)."""
        category = await self._ensure_category(
            guild, HOSPITAL_CATEGORY_KEY, HOSPITAL_CATEGORY_NAME
        )
        hospital_role = await self.bot.access.hospital_role(
            guild, hospital_id, hospital_name
        )
        tier2_role = await self.bot.access.tier_role(guild, 2)
        overwrites = self._overwrites(
            guild,
            public=False,
            view_roles=[hospital_role, tier2_role],
            deny_roles=[],
        )
        # tier2 라도 해당 병원 역할이 없으면 못 보도록 명시적으로 차단
        if tier2_role is not None:
            overwrites[tier2_role] = discord.PermissionOverwrite(view_channel=False)
        if hospital_role is not None:
            overwrites[hospital_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        return await self._ensure_text(
            guild,
            self.hospital_channel_key(hospital_id),
            self.hospital_channel_name(hospital_name),
            category,
            overwrites,
        )

    # ---------- 기간 스레드 ----------

    async def ensure_period_thread(
        self,
        guild: discord.Guild,
        hospital_id: int,
        hospital_name: str,
        period_text: str,
        *,
        private: bool = True,
    ) -> Optional[discord.Thread]:
        """병원 채널 안에 기간 단위 스레드를 만든다.

        ``private=True`` (기본) 이면 **비공개 스레드**로 만들어, 초대된
        (해당 기간에 입원했던) 인원만 볼 수 있다. 이미 있으면 재사용한다.
        """
        channel = await self.ensure_hospital_channel(
            guild, hospital_id, hospital_name
        )
        if channel is None:
            return None
        thread_name = _clip(period_text)
        for thread in channel.threads:
            if thread.name == thread_name:
                return thread
        thread_type = (
            discord.ChannelType.private_thread
            if private
            else discord.ChannelType.public_thread
        )
        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=thread_type,
                reason="MindFinder 기간 커뮤니티",
            )
            await thread.send(
                f"**{hospital_name} · {period_text}**\n"
                "같은 시기에 입원했던 분들과 이야기를 나눠보세요."
            )
            return thread
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("기간 스레드 생성 실패: hospital=%s", hospital_id)
            return None

    # ---------- 커뮤니티 접근 동기화 ----------

    @staticmethod
    def months_for_admission(admission) -> list[int]:
        """입원 이력이 걸치는 '월' 목록 (admit_ym ~ discharge_ym, 양끝 포함)."""
        start = admission.admit_ym
        end = admission.discharge_ym
        if end is None:
            end = current_ym()
        if end < start:
            end = start
        return list(range(start, end + 1))

    async def add_member_to_thread(
        self, thread: Optional[discord.Thread], member: discord.Member
    ) -> None:
        """비공개 스레드에 멤버를 추가한다 (이미 참여 중이면 무시)."""
        if thread is None or member is None:
            return
        try:
            await thread.add_user(member)
        except discord.HTTPException:
            logger.debug(
                "스레드 참여 추가 실패: thread=%s", getattr(thread, "id", None)
            )

    async def ensure_community_access(
        self, guild: discord.Guild, user_id: int
    ) -> None:
        """인증 사용자를 자신의 이력(병원·기간)에 맞는 비공개 스레드에 초대한다.

        - 서류/지식 인증을 받은 병원마다
        - 입원 기간에 걸치는 '월 단위' 비공개 스레드를 확보하고
        - 사용자를 그 스레드에 추가한다.
        """
        member = guild.get_member(user_id)
        if member is None:
            return
        admissions = await self.bot.admissions.list_by_user(user_id)
        if not admissions:
            return
        verified_ids = set(await self.bot.access.verified_hospital_ids(user_id))
        for adm in admissions:
            if adm.hospital_id not in verified_ids:
                continue
            for ym_value in self.months_for_admission(adm):
                thread = await self.ensure_period_thread(
                    guild,
                    adm.hospital_id,
                    adm.hospital.name,
                    format_ym(ym_value),
                    private=True,
                )
                await self.add_member_to_thread(thread, member)

    # ---------- 전체 구성 ----------

    async def ensure_all(self, guild: discord.Guild) -> None:
        """용도별 채널/카테고리 전체를 확보한다."""
        settings = self.bot.settings
        tier1_role = await self.bot.access.tier_role(guild, 1)
        tier2_role = await self.bot.access.tier_role(guild, 2)

        # 1) 인증 카테고리 (아무나 접근)
        verify_cat = await self._ensure_category(
            guild, VERIFY_CATEGORY_KEY, VERIFY_CATEGORY_NAME
        )
        public_ow = self._overwrites(guild, public=True)
        await self._ensure_text(
            guild, VERIFY_CHANNEL_KEY, VERIFY_CHANNEL_NAME, verify_cat, public_ow
        )
        await self._ensure_text(
            guild, PANEL_CHANNEL_KEY, PANEL_CHANNEL_NAME, verify_cat, public_ow
        )

        # 2) 커뮤니티 카테고리 (1단계 이상)
        community_cat = await self._ensure_category(
            guild, COMMUNITY_CATEGORY_KEY, COMMUNITY_CATEGORY_NAME
        )
        community_ow = self._overwrites(
            guild, public=False, view_roles=[tier1_role, tier2_role]
        )
        await self._ensure_text(
            guild,
            COMMUNITY_CHANNEL_KEY,
            COMMUNITY_CHANNEL_NAME,
            community_cat,
            community_ow,
        )
        notice_ow = self._overwrites(
            guild, public=False, view_roles=[tier1_role, tier2_role]
        )
        # 공지는 읽기 전용에 가깝게 (일반 사용자 전송 제한)
        for role in (tier1_role, tier2_role):
            if role is not None:
                notice_ow[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True
                )
        await self._ensure_text(
            guild, NOTICE_CHANNEL_KEY, NOTICE_CHANNEL_NAME, community_cat, notice_ow
        )
        # 약관·개인정보 전문 게시 채널 (1단계 이상 열람, 관리자만 작성)
        terms_ow = self._overwrites(
            guild, public=False, view_roles=[tier1_role, tier2_role]
        )
        for role in (tier1_role, tier2_role):
            if role is not None:
                terms_ow[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=False, read_message_history=True
                )
        await self._ensure_text(
            guild, TERMS_CHANNEL_KEY, TERMS_CHANNEL_NAME, community_cat, terms_ow
        )

        # 3) 매칭 카테고리 (2단계 이상)
        match_cat = await self._ensure_category(
            guild, MATCH_CATEGORY_KEY, MATCH_CATEGORY_NAME
        )
        match_ow = self._overwrites(guild, public=False, view_roles=[tier2_role])
        await self._ensure_text(
            guild, MATCH_CHANNEL_KEY, MATCH_CHANNEL_NAME, match_cat, match_ow
        )

        # 4) 관리자 카테고리/채널 (관리자 역할만 접근)
        admin_cat = await self._ensure_category(
            guild, ADMIN_CATEGORY_KEY, ADMIN_CATEGORY_NAME
        )
        admin_ow = self._overwrites(guild, public=False)
        await self._ensure_text(
            guild, ADMIN_CHANNEL_KEY, ADMIN_CHANNEL_NAME, admin_cat, admin_ow
        )

        # 5) 병원별 채널
        if settings.per_hospital_channels:
            for hospital in await self.bot.hospitals.list_all():
                await self.ensure_hospital_channel(guild, hospital.id, hospital.name)

    async def managed_text_channel(
        self, guild: discord.Guild, key: str
    ) -> Optional[discord.TextChannel]:
        """논리 키에 해당하는 관리 대상 텍스트 채널을 반환한다 (없으면 구성)."""
        record = await self.bot.managed_channels.get(guild.id, key)
        if record is not None:
            channel = guild.get_channel(record.discord_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        await self.ensure_all(guild)
        record = await self.bot.managed_channels.get(guild.id, key)
        if record is not None:
            channel = guild.get_channel(record.discord_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        return None

    async def panel_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        """상시 안내 패널을 게시할 채널을 반환한다 (없으면 구성)."""
        return await self.managed_text_channel(guild, PANEL_CHANNEL_KEY)

    async def verify_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        """인증(서류 제출) 채널을 반환한다 (없으면 구성)."""
        return await self.managed_text_channel(guild, VERIFY_CHANNEL_KEY)
