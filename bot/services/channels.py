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
  - 병원 카테고리 (병원마다 **개별 카테고리**)
      · 🏥-<병원명>     : 해당 병원 인증자만 (역할 기반)
        └ 기간 채널 (예: "2020년-3월") — 해당 월에 입원한 인증자만

권한은 역할 기반이라, 인증/병원 인증을 받으면 역할만 부여하면 된다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Sequence

import discord

from bot.services.access_control import (
    TIER1_ROLE_NAME,
    TIER2_ROLE_NAME,
    AccessControlService,
)
from bot.utils.hospitals import hospital_display_name
from bot.utils.periods import current_ym

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

# 카테고리 키/이름
VERIFY_CATEGORY_KEY = "cat:verify"
VERIFY_CATEGORY_NAME = "인증"
COMMUNITY_CATEGORY_KEY = "cat:community"
COMMUNITY_CATEGORY_NAME = "커뮤니티"
# (레거시) 단일 병원-커뮤니티 카테고리. 새 구조에서는 병원마다 개별 카테고리를 쓴다.
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
# 약관·개인정보처리방침 전문 게시 채널 (1단계 이상이면 열람 가능)
TERMS_CHANNEL_KEY = "ch:terms"
TERMS_CHANNEL_NAME = "📜-이용약관-개인정보처리방침"
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
        readonly: bool = False,
    ) -> dict:
        """채널 권한 오버라이트를 만든다.

        public=True 면 @everyone 이 볼 수 있다. 그 외에는 @everyone 을 차단하고
        view_roles 에게만 열어준다. deny_roles 는 명시적으로 차단한다.
        readonly=True 면 보기만 허용하고 **채팅(전송)은 막는다** (패널/안내 채널).
        """
        ow: dict = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=public,
                send_messages=public and not readonly,
                read_message_history=public,
            )
        }
        for role in view_roles:
            if role is not None:
                ow[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=not readonly,
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
        # 봇 자신은 항상 전송 가능해야 한다 (readonly 채널에 패널 게시).
        me = getattr(guild, "me", None)
        if me is not None:
            ow[me] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True,
            )
        return ow

    # ---------- 카테고리/채널 확보 ----------

    async def _ensure_category(
        self,
        guild: discord.Guild,
        key: str,
        name: str,
        overwrites: Optional[dict] = None,
    ) -> Optional[discord.CategoryChannel]:
        # 매칭은 **저장된 ID(discord_id)** 로만 한다 (이름 기준 매칭 금지).
        record = await self.bot.managed_channels.get(guild.id, key)
        if record is not None:
            channel = guild.get_channel(record.discord_id)
            if channel is not None:
                # 이름/권한이 바뀌었으면 최신 값으로 맞춘다.
                edit_kwargs: dict = {}
                if getattr(channel, "name", None) != name:
                    edit_kwargs["name"] = name
                if overwrites is not None:
                    edit_kwargs["overwrites"] = overwrites
                if edit_kwargs:
                    try:
                        await channel.edit(**edit_kwargs)
                    except (discord.Forbidden, discord.HTTPException):
                        logger.debug("카테고리 동기화 실패: %s", record.discord_id)
                return channel
        if not self.bot.settings.auto_create_channels:
            return None
        try:
            category = await guild.create_category(
                name=name,
                overwrites=overwrites,
                reason="MindFinder 채널 자동 구성",
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
        # 매칭은 **저장된 ID(discord_id)** 로만 한다 (이름 기준 매칭 금지).
        record = await self.bot.managed_channels.get(guild.id, key)
        channel: Optional[discord.TextChannel] = None
        if record is not None:
            channel = guild.get_channel(record.discord_id)  # type: ignore[assignment]
        if channel is not None:
            # 이름/카테고리가 바뀌었으면 최신 값으로 맞춘다 (ID 로 추적하므로 가능).
            edit_kwargs: dict = {}
            if getattr(channel, "name", None) != name:
                edit_kwargs["name"] = name
            if category is not None and getattr(channel, "category", None) != category:
                edit_kwargs["category"] = category
            if sync_permissions:
                edit_kwargs["overwrites"] = overwrites
            if edit_kwargs:
                try:
                    await channel.edit(**edit_kwargs)
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("채널 동기화 실패: channel=%s", channel.id)
            await self.bot.managed_channels.upsert(
                guild.id, "channel", key, channel.id, channel.name
            )
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

    # ---------- 병원 카테고리/기간 채널 ----------

    @staticmethod
    def hospital_category_key(hospital_id: int) -> str:
        """병원 개별 카테고리 키 (예: cat:hospital:3)."""
        return f"cat:hospital:{hospital_id}"

    @staticmethod
    def hospital_category_name(hospital_name: str) -> str:
        # 병동 정보가 있으면 함께 표시한다 (예: 서울대학교어린이병원 7A병동).
        return _clip(f"🏥-{hospital_display_name(hospital_name)}")

    @staticmethod
    def period_channel_key(hospital_id: int, ym_value: int) -> str:
        """기간(월) 채널 키 (예: ch:hospital:3:24302)."""
        return f"ch:hospital:{hospital_id}:{ym_value}"

    @staticmethod
    def period_channel_name(ym_value: int) -> str:
        """기간 채널 이름 (예: 2025년-3월)."""
        year = ym_value // 12
        month = ym_value % 12 + 1
        return _clip(f"{year}년-{month}월")

    def _hospital_overwrites(
        self, guild: discord.Guild, hospital_role: Optional[discord.Role]
    ) -> dict:
        """병원 카테고리/기간 채널 공통 권한.

        해당 **병원 역할**을 가진 사람만 볼 수 있다.
        @everyone 은 차단되고, tier2(서류 인증) 역할은 호출부에서 명시적으로 차단한다.
        """
        overwrites = self._overwrites(
            guild, public=False, view_roles=[hospital_role]
        )
        if hospital_role is not None:
            overwrites[hospital_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
        return overwrites

    async def ensure_hospital_category(
        self,
        guild: discord.Guild,
        hospital_id: int,
        hospital_name: str,
        *,
        with_role: bool = True,
    ) -> Optional[discord.CategoryChannel]:
        """병원 **개별 카테고리**(이름 = 병원명)를 확보한다.

        ``with_role=False`` 이면 **역할을 만들지 않고** 카테고리만 만든다.
        (병원 역할은 사용자가 인증(verified)을 받았을 때만 생성)
        """
        hospital_role = None
        if with_role:
            hospital_role = await self.bot.access.hospital_role(
                guild, hospital_id, hospital_name
            )
        tier2_role = await self.bot.access.tier_role(guild, 2)
        overwrites = self._hospital_overwrites(guild, hospital_role)
        # tier2 라도 해당 병원 역할이 없으면 못 보도록 명시적으로 차단
        if tier2_role is not None:
            overwrites[tier2_role] = discord.PermissionOverwrite(view_channel=False)
        return await self._ensure_category(
            guild,
            self.hospital_category_key(hospital_id),
            self.hospital_category_name(hospital_name),
            overwrites,
        )

    async def ensure_period_channel(
        self,
        guild: discord.Guild,
        hospital_id: int,
        hospital_name: str,
        ym_value: int,
        *,
        with_role: bool = True,
        members: Optional[Sequence[discord.Member]] = None,
    ) -> Optional[discord.TextChannel]:
        """병원 카테고리 안에 **기간(월) 비공개 채널**을 확보한다.

        이름은 ``{year}년-{month}월`` 이다.

        접근 방식:
        - ``members`` 가 주어지면 그 사용자들에게 **개별 오버라이트**로 접근을 준다.
          (역할 태그를 선택하지 않아도 채널은 보인다 — 인증만 하면 열림)
        - ``with_role`` 이면 병원 역할을 가진 사람도 볼 수 있게 한다(선택한 경우).

        주의: 기존 채널이 있으면 **오버라이트를 통째로 교체하지 않는다.**
        (다른 인증자의 개별 권한이 사라지기 때문) 기본 권한만 동기화하고,
        멤버 개별 권한은 ``set_permissions`` 로 **추가**한다.
        """
        category = await self.ensure_hospital_category(
            guild, hospital_id, hospital_name, with_role=with_role
        )
        hospital_role = None
        if with_role:
            hospital_role = await self.bot.access.hospital_role(
                guild, hospital_id, hospital_name
            )
        tier2_role = await self.bot.access.tier_role(guild, 2)
        overwrites = self._hospital_overwrites(guild, hospital_role)
        if tier2_role is not None:
            overwrites[tier2_role] = discord.PermissionOverwrite(view_channel=False)
        key = self.period_channel_key(hospital_id, ym_value)
        # sync_permissions=False 로 두어 **기존 오버라이트를 통째로 교체하지 않는다.**
        # (다른 인증자의 개별 권한이 사라지는 것을 막는다.)
        channel = await self._ensure_text(
            guild,
            key,
            self.period_channel_name(ym_value),
            category,
            overwrites,
            sync_permissions=False,
        )
        if channel is None:
            return None
        # 필요한 권한만 **개별 추가**한다 (set_permissions 는 해당 대상만 변경).
        for target, ow in overwrites.items():
            try:
                await channel.set_permissions(target, overwrite=ow, reason="MindFinder 기간 채널 권한")
            except (discord.Forbidden, discord.HTTPException):
                logger.debug(
                    "기간 채널 기본 권한 설정 실패: channel=%s",
                    getattr(channel, "id", None),
                )
        # 인증한 사용자 개개인에게 접근 권한을 **추가**한다 (기존 권한 유지).
        for member in members or ():
            if member is None:
                continue
            try:
                await channel.set_permissions(
                    member,
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    reason="MindFinder 기간 채널 접근 부여",
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.debug(
                    "기간 채널 개별 권한 부여 실패: channel=%s user=%s",
                    getattr(channel, "id", None),
                    getattr(member, "id", None),
                )
        return channel

    async def ensure_hospital_channel(
        self,
        guild: discord.Guild,
        hospital_id: int,
        hospital_name: str,
        *,
        with_role: bool = True,
    ) -> Optional[discord.CategoryChannel]:
        """(호환용) 병원 전용 공간을 확보한다.

        새 구조에서는 병원마다 **카테고리**를 만들고 그 안에 기간 채널을 둔다.
        기존 호출부가 깨지지 않도록 카테고리 확보로 위임한다.
        """
        return await self.ensure_hospital_category(
            guild, hospital_id, hospital_name, with_role=with_role
        )

    async def ensure_optin_hospital(
        self,
        guild: discord.Guild,
        user_id: int,
        hospital_id: int,
        hospital_name: str,
    ) -> Optional[discord.CategoryChannel]:
        """사용자가 특정 병원 역할을 '선택'했을 때 카테고리/역할을 확보하고 부여한다."""
        category = await self.ensure_hospital_category(
            guild, hospital_id, hospital_name, with_role=True
        )
        member = guild.get_member(user_id)
        if member is not None:
            await self.bot.access.grant_hospital_role(
                guild, member, hospital_id, hospital_name
            )
        return category

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

    async def ensure_community_access(
        self, guild: discord.Guild, user_id: int
    ) -> None:
        """사용자가 **선택한** 병원의 기간 채널 접근을 동기화한다.

        중요: 병원 역할(태그)은 **자동으로 부여하지 않는다.**
        역할은 사용자가 커뮤니티 패널에서 직접 '선택'했을 때만 부여하고,
        선택 상태는 DB(hospital_optins)로 사람별 관리한다.

        여기서는 '선택된 병원'에 대해서만 카테고리·기간 채널을 확보하고,
        선택을 해제한 병원의 역할은 회수한다.
        """
        member = guild.get_member(user_id)
        if member is None:
            return
        admissions = await self.bot.admissions.list_by_user(user_id)
        if not admissions:
            return
        # 사용자가 선택(opt-in)한 병원 id 집합
        try:
            opted_in = set(await self.bot.access.optedin_hospital_ids(user_id))
        except Exception:  # noqa: BLE001
            logger.exception("병원 선택 상태 조회 실패: user=%s", user_id)
            opted_in = set()

        # 인증(verified)되어 커뮤니티 대상인 이력의 병원 id
        eligible_ids = {
            adm.hospital_id
            for adm in admissions
            if adm.verification == "verified"
            and getattr(adm, "is_community_eligible", True)
        }

        for adm in admissions:
            if adm.verification != "verified":
                continue
            if not getattr(adm, "is_community_eligible", True):
                continue
            selected = adm.hospital_id in opted_in
            # 카테고리는 항상 확보하되, 역할(태그)은 **선택한 경우에만** 부여한다.
            try:
                await self.ensure_hospital_category(
                    guild, adm.hospital_id, adm.hospital.name, with_role=selected
                )
                if selected:
                    await self.bot.access.grant_hospital_role(
                        guild, member, adm.hospital_id, adm.hospital.name
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "병원 카테고리/역할 동기화 실패: hospital=%s", adm.hospital_id
                )
            # 기간 채널은 **인증만 하면 열린다** (역할 태그 선택과 무관).
            # 역할 태그를 안 골라도 채널은 사용자 개별 권한으로 보이게 한다.
            for ym_value in self.months_for_admission(adm):
                await self.ensure_period_channel(
                    guild,
                    adm.hospital_id,
                    adm.hospital.name,
                    ym_value,
                    with_role=selected,
                    members=[member],
                )

        # 선택을 해제한 병원의 역할은 회수한다 (태그 제거).
        for hospital_id in eligible_ids - opted_in:
            try:
                await self.bot.access.revoke_hospital_role(
                    guild, member, hospital_id
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "병원 역할 회수 실패: user=%s hospital=%s", user_id, hospital_id
                )

    # ---------- 전체 구성 ----------

    async def ensure_all(self, guild: discord.Guild) -> None:
        """용도별 채널/카테고리 전체를 확보한다."""
        tier1_role = await self.bot.access.tier_role(guild, 1)
        tier2_role = await self.bot.access.tier_role(guild, 2)

        # 1) 인증 카테고리 (아무나 접근)
        verify_cat = await self._ensure_category(
            guild, VERIFY_CATEGORY_KEY, VERIFY_CATEGORY_NAME
        )
        # 인증 채널: 서류(PDF) 업로드가 가능해야 하므로 일반 전송 허용
        public_ow = self._overwrites(guild, public=True)
        await self._ensure_text(
            guild, VERIFY_CHANNEL_KEY, VERIFY_CHANNEL_NAME, verify_cat, public_ow
        )
        # 패널(안내) 채널: 봇 안내 전용 → 사용자 채팅 차단(readonly)
        panel_ow = self._overwrites(guild, public=True, readonly=True)
        await self._ensure_text(
            guild, PANEL_CHANNEL_KEY, PANEL_CHANNEL_NAME, verify_cat, panel_ow
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
            guild, public=False, view_roles=[tier1_role, tier2_role], readonly=True
        )
        await self._ensure_text(
            guild, NOTICE_CHANNEL_KEY, NOTICE_CHANNEL_NAME, community_cat, notice_ow
        )
        # 약관·개인정보처리방침 전문 게시 채널.
        # 동의 임베드의 '전문 보기' 링크로 **누구나** 열람할 수 있도록 전체 공개한다.
        terms_ow = self._overwrites(guild, public=True, readonly=True)
        await self._ensure_text(
            guild, TERMS_CHANNEL_KEY, TERMS_CHANNEL_NAME, community_cat, terms_ow
        )

        # 3) 매칭 카테고리 (2단계 이상)
        match_cat = await self._ensure_category(
            guild, MATCH_CATEGORY_KEY, MATCH_CATEGORY_NAME
        )
        # 매칭 채널도 안내 패널 전용 → 채팅 차단
        match_ow = self._overwrites(
            guild, public=False, view_roles=[tier2_role], readonly=True
        )
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

        # 5) 병원별 카테고리 (이름 = 병원명)
        #    카테고리만 확보하고, **역할(태그)은 만들지 않는다.**
        #    역할은 사용자가 커뮤니티 패널에서 직접 선택할 때만 생성·부여하며,
        #    선택 상태는 DB(hospital_optins)로 사람별 관리한다.
        #    (기간 채널은 선택한 사용자에 대해 ensure_community_access 가 만든다.)
        await self._cleanup_legacy_hospital_channels(guild)
        for hospital in await self.bot.hospitals.list_all():
            await self.ensure_hospital_category(
                guild, hospital.id, hospital.name, with_role=False
            )

    async def _cleanup_legacy_hospital_channels(self, guild: discord.Guild) -> None:
        """옛 구조(단일 병원 카테고리 + 병원별 텍스트 채널)를 정리한다.

        - ``cat:hospital`` (단일 카테고리)
        - ``ch:hospital:{id}`` (병원별 텍스트 채널)
        기록(managed_channels)에서 찾아 실제 채널을 삭제하고 기록도 지운다.
        """
        try:
            records = await self.bot.managed_channels.list_for_guild(guild.id)
        except Exception:  # noqa: BLE001
            logger.exception("관리 채널 목록 조회 실패: guild=%s", guild.id)
            return
        # 현재 유효한 병원 id 집합 (DB에 남아 있는 병원만 유지 대상)
        valid_ids = {h.id for h in await self.bot.hospitals.list_all()}
        for record in records:
            key = record.key
            is_legacy = key == HOSPITAL_CATEGORY_KEY or (
                key.startswith("ch:hospital:") and key.count(":") == 2
            )
            # 병합으로 사라진 병원(id)의 카테고리도 중복이므로 정리한다.
            if not is_legacy and key.startswith("cat:hospital:"):
                try:
                    hid = int(key.split(":")[-1])
                except ValueError:
                    hid = -1
                if hid not in valid_ids:
                    is_legacy = True
            if not is_legacy:
                continue
            channel = guild.get_channel(record.discord_id)
            if channel is not None:
                try:
                    await channel.delete(  # type: ignore[attr-defined]
                        reason="MindFinder 레거시/중복 병원 카테고리 정리"
                    )
                    logger.info("레거시/중복 병원 채널 삭제: key=%s id=%s", key, record.discord_id)
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("레거시 병원 채널 삭제 실패: %s", record.discord_id)
            await self.bot.managed_channels.delete(guild.id, key)

    async def _verified_hospital_ids(self) -> set[int]:
        """인증(verified)된 이력이 있는 병원 id 집합 (역할 생성 대상)."""
        try:
            return set(await self.bot.admissions.list_verified_hospital_ids())
        except Exception:  # noqa: BLE001
            logger.exception("인증 병원 조회 실패")
            return set()

    async def migrate_role_names(self, guild: discord.Guild) -> None:
        """기존에 생성된 역할을 새 이름(임시인증/인증됨/병원 인증됨)으로 바꾼다."""
        records = await self.bot.managed_roles.list_for_guild(guild.id)
        for record in records:
            role = guild.get_role(record.discord_id)
            if role is None:
                continue
            if record.key == "tier1":
                target = TIER1_ROLE_NAME
            elif record.key == "tier2":
                target = TIER2_ROLE_NAME
            elif record.key.startswith("hospital:"):
                try:
                    hospital_id = int(record.key.split(":", 1)[1])
                except (IndexError, ValueError):
                    continue
                hospital = await self.bot.hospitals.get_by_id(hospital_id)
                if hospital is None:
                    continue
                target = AccessControlService.hospital_role_name(hospital.name)
            else:
                continue
            if role.name == target:
                continue
            try:
                await role.edit(name=target, reason="MindFinder 역할 이름 정리")
                await self.bot.managed_roles.upsert(
                    guild.id, record.key, role.id, target
                )
                logger.info("역할 이름 변경: %s → %s", role.name, target)
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("역할 이름 변경 실패: role=%s", role.id)

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
