"""접근 제어 서비스: 인증 단계별 역할·채널 권한 관리.

디스코드 역할/채널은 서버마다 다르므로, 봇이 필요로 하는 역할을
"이름"으로 자동 생성하고 생성된 ID 를 DB(managed_roles)에 기록해 재사용한다.

인증 단계(tier):
  - tier 0 : 아무 이력 없음 → 인증(안내) 채널만 접근
  - tier 1 : 자기 신고 이력 등록 → 전체 커뮤니티 열람
  - tier 2 : 서류/진위확인 통과 → 병원·기간·매칭 채널 이용

지식기반 인증으로 병원별 인증을 받은 사용자는 해당 병원 채널만 추가로 볼 수 있다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Sequence

import discord

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

# 인증 단계 키
TIER1_KEY = "tier1"
TIER2_KEY = "tier2"

# 봇이 자동 생성할 역할 이름
TIER1_ROLE_NAME = "MindFinder 인증됨"
TIER2_ROLE_NAME = "MindFinder 서류인증"

# 역할 이름 최대 길이 (디스코드 제한)
_MAX_ROLE_NAME = 100


class AccessControlService:
    """인증 단계별 역할 부여/회수 및 채널 권한 계산."""

    def __init__(self, bot: "MindFinderBot") -> None:
        self.bot = bot

    # ---------- 역할 이름/키 ----------

    @staticmethod
    def hospital_role_key(hospital_id: int) -> str:
        return f"hospital:{hospital_id}"

    @staticmethod
    def hospital_role_name(hospital_name: str) -> str:
        name = f"MF·{hospital_name}"
        return name[:_MAX_ROLE_NAME]

    # ---------- 역할 확보 ----------

    async def _config_role_id(self, key: str) -> Optional[int]:
        """설정(.env)에 명시된 역할 ID 를 반환한다."""
        settings = self.bot.settings
        if key == TIER1_KEY:
            return settings.verified_community_role_id
        if key == TIER2_KEY:
            # DOC_VERIFIED_ROLE_ID 우선, 없으면 기존 VERIFIED_ROLE_ID 로 폴백
            return settings.doc_verified_role_id or settings.verified_role_id
        return None

    async def resolve_role(
        self,
        guild: discord.Guild,
        key: str,
        name: str,
        *,
        create: bool = True,
    ) -> Optional[discord.Role]:
        """논리 키에 해당하는 역할을 확보한다.

        1) 설정에 ID 가 있으면 그 역할을 사용
        2) DB(managed_roles)에 기록된 역할 ID 가 유효하면 재사용
        3) 같은 이름의 역할이 이미 있으면 재사용
        4) 없으면 새로 생성 (create=True 이고 자동 생성이 허용된 경우)
        """
        # 1) 설정값
        config_id = await self._config_role_id(key)
        if config_id:
            role = guild.get_role(config_id)
            if role is not None:
                await self.bot.managed_roles.upsert(guild.id, key, role.id, role.name)
                return role
            logger.warning("설정된 역할 ID(%s)를 서버에서 찾을 수 없습니다.", config_id)

        # 2) DB 기록
        record = await self.bot.managed_roles.get(guild.id, key)
        if record is not None:
            role = guild.get_role(record.discord_id)
            if role is not None:
                return role

        # 3) 이름으로 검색
        for role in guild.roles:
            if role.name == name:
                await self.bot.managed_roles.upsert(guild.id, key, role.id, role.name)
                return role

        # 4) 생성
        if not create or not self.bot.settings.auto_create_channels:
            return None
        try:
            role = await guild.create_role(
                name=name,
                mentionable=False,
                reason="MindFinder 접근 제어 역할 자동 생성",
            )
        except discord.Forbidden:
            logger.warning("역할 생성 권한 없음: guild=%s key=%s", guild.id, key)
            return None
        except discord.HTTPException:
            logger.exception("역할 생성 실패: guild=%s key=%s", guild.id, key)
            return None
        await self.bot.managed_roles.upsert(guild.id, key, role.id, role.name)
        logger.info("역할 자동 생성: guild=%s key=%s role=%s", guild.id, key, role.id)
        return role

    async def tier_role(
        self, guild: discord.Guild, tier: int
    ) -> Optional[discord.Role]:
        """인증 단계에 해당하는 역할을 반환한다."""
        if tier >= 2:
            return await self.resolve_role(guild, TIER2_KEY, TIER2_ROLE_NAME)
        if tier == 1:
            return await self.resolve_role(guild, TIER1_KEY, TIER1_ROLE_NAME)
        return None

    async def hospital_role(
        self, guild: discord.Guild, hospital_id: int, hospital_name: str
    ) -> Optional[discord.Role]:
        key = self.hospital_role_key(hospital_id)
        return await self.resolve_role(
            guild, key, self.hospital_role_name(hospital_name)
        )

    # ---------- 역할 부여/회수 ----------

    async def _add_role(
        self, member: discord.Member, role: Optional[discord.Role]
    ) -> None:
        if role is None or role in member.roles:
            return
        try:
            await member.add_roles(role, reason="MindFinder 인증 처리")
        except discord.Forbidden:
            logger.warning("역할 부여 권한 없음: member=%s role=%s", member.id, role.id)
        except discord.HTTPException:
            logger.exception("역할 부여 실패: member=%s role=%s", member.id, role.id)

    async def grant_tier(
        self, guild: discord.Guild, member: discord.Member, tier: int
    ) -> None:
        """단계별 역할을 부여한다 (1단계는 2단계도 포함)."""
        if tier >= 1:
            await self._add_role(member, await self.tier_role(guild, 1))
        if tier >= 2:
            await self._add_role(member, await self.tier_role(guild, 2))

    async def grant_hospital_role(
        self,
        guild: discord.Guild,
        member: discord.Member,
        hospital_id: int,
        hospital_name: str,
    ) -> None:
        role = await self.hospital_role(guild, hospital_id, hospital_name)
        await self._add_role(member, role)

    # ---------- 조회 ----------

    async def user_tier(self, user_id: int) -> int:
        """사용자의 인증 단계를 계산한다.

        서류 인증(verified) 이력 또는 관리자가 승인한 지식기반 인증이 있으면
        2단계로 본다.
        """
        admissions = await self.bot.admissions.list_by_user(user_id)
        if any(a.verification == "verified" for a in admissions):
            return 2
        approved = await self.bot.knowledge.list_for_respondent(user_id, "approved")
        if approved:
            return 2
        if admissions:
            return 1
        return 0

    async def verified_hospital_ids(self, user_id: int) -> Sequence[int]:
        """사용자가 인증(서류 또는 지식기반)을 받은 병원 id 목록."""
        admissions = await self.bot.admissions.list_by_user(user_id)
        seen: list[int] = []
        for a in admissions:
            if a.verification == "verified" and a.hospital_id not in seen:
                seen.append(a.hospital_id)
        for req in await self.bot.knowledge.list_for_respondent(user_id, "approved"):
            if req.hospital_id not in seen:
                seen.append(req.hospital_id)
        return seen

    async def can_match(self, user_id: int) -> bool:
        """매칭 기능 사용 가능 여부 (기본: 서류 인증자만)."""
        if self.bot.settings.allow_unverified_matching:
            return True
        return await self.user_tier(user_id) >= 2
