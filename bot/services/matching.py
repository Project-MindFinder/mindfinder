"""매칭 도메인 서비스.

중개자 원칙: 봇은 전화번호/SNS를 저장하거나 전달하지 않는다.
양쪽이 동의하면 **비공개 채널**을 만들어 참여자들을 초대하고,
그 안에서 직접 대화하도록 안내한다.

UI(슬래시 커맨드, 상시 패널)에 의존하지 않는 로직을 담되,
디스코드 채널 생성/권한 같은 길드 부수효과는 guild 객체를 받아 처리한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Sequence

import discord

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    """매칭 후보 한 명."""

    user_id: int
    pairs: list  # [(my_admission, other_admission), ...]


@dataclass
class AcceptResult:
    """매칭 수락 결과."""

    channel: Optional[discord.TextChannel]
    hospital_name: str
    error: Optional[str] = None


class MatchingService:
    """매칭 희망 설정, 후보 찾기, 신청/수락/거절, 비공개 채널 연결."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot

    # ---------- 매칭 희망 설정 ----------

    async def set_matching(self, user_id: int, enabled: bool) -> None:
        # 매칭은 서류 인증(2단계) 사용자에게만 허용한다 (설정으로 완화 가능).
        if enabled and not await self.bot.access.can_match(user_id):
            raise ValueError(
                "매칭은 서류 인증을 마친 사용자만 이용할 수 있습니다. "
                "먼저 서류 인증을 진행해주세요."
            )
        await self.bot.user_repo.ensure_user(user_id)
        await self.bot.match_prefs.set_enabled(user_id, enabled)
        await self.bot.audit.log(
            "match_preference.set",
            actor_id=user_id,
            detail="on" if enabled else "off",
        )

    # ---------- 매칭 후보 찾기 ----------

    async def find_candidates(
        self, user_id: int
    ) -> tuple[list[Candidate], int]:
        """매칭 후보를 찾는다.

        Returns:
            (매칭을 원하는 후보 목록, 병원·기간이 겹치는 전체 후보 수)

        Raises:
            ValueError: 등록된 이력이 없을 때.
        """
        my_admissions = await self.bot.admissions.list_by_user(user_id)
        if not my_admissions:
            raise ValueError("먼저 입원 이력을 등록해주세요.")

        # 매칭은 서류 인증(2단계) 사용자에게만 허용한다 (설정으로 완화 가능).
        if not await self.bot.access.can_match(user_id):
            raise ValueError(
                "매칭은 서류 인증을 마친 사용자만 이용할 수 있습니다. "
                "먼저 서류 인증을 진행해주세요."
            )

        candidates: dict[int, list] = {}
        for my_adm in my_admissions:
            # 서류 인증(verified)을 마친 이력끼리만 매칭한다.
            if my_adm.verification != "verified":
                continue
            others = await self.bot.admissions.find_overlapping(
                hospital_id=my_adm.hospital_id,
                admit_ym_val=my_adm.admit_ym,
                discharge_ym_val=my_adm.discharge_ym,
                exclude_user_id=user_id,
            )
            for other in others:
                if other.verification != "verified":
                    continue
                candidates.setdefault(other.user_id, []).append((my_adm, other))

        matched: list[Candidate] = []
        for other_id, pairs in candidates.items():
            if await self.bot.match_prefs.is_enabled(other_id):
                matched.append(Candidate(user_id=other_id, pairs=pairs))
        return matched, len(candidates)

    # ---------- 매칭 신청 ----------

    async def request_match(
        self, requester_id: int, target_id: int, admission_id: Optional[int]
    ) -> tuple[int, bool]:
        """매칭 신청을 생성한다.

        Returns:
            (request_id, already_pending)
            이미 대기 중인 신청이 있으면 request_id=0, already_pending=True.
        """
        existing = await self.bot.match_requests.find_pending_between(
            requester_id, target_id
        )
        if existing is not None:
            return 0, True

        request_id = await self.bot.match_requests.create(
            requester_id=requester_id,
            target_id=target_id,
            admission_id=admission_id,
        )
        await self.bot.audit.log(
            "match.request",
            actor_id=requester_id,
            target=f"match:{request_id}",
            detail=f"target={target_id}",
        )
        return request_id, False

    # ---------- 받은 신청 처리 ----------

    async def accept_match(
        self, request_id: int, target_id: int, guild: discord.Guild
    ) -> AcceptResult:
        """받은 매칭 신청을 수락하고 비공개 채널로 연결한다."""
        req = await self.bot.match_requests.get_by_id(request_id)
        if req is None or req.target_id != target_id or req.status != "pending":
            return AcceptResult(channel=None, hospital_name="", error="invalid")

        await self.bot.match_requests.resolve(request_id, "accepted")
        await self.bot.audit.log(
            "match.accept",
            actor_id=target_id,
            target=f"match:{request_id}",
        )

        hospital_id: Optional[int] = None
        hospital_name = "미상 병원"
        if req.admission_id is not None:
            admission = await self.bot.admissions.get_by_id(req.admission_id)
            if admission is not None:
                hospital_id = admission.hospital_id
                hospital_name = admission.hospital.name

        member_ids = [req.requester_id, req.target_id]
        channel = await self.ensure_match_channel(
            guild, member_ids, hospital_id, hospital_name
        )
        if channel is None:
            return AcceptResult(
                channel=None,
                hospital_name=hospital_name,
                error="forbidden",
            )

        await self.announce_match(channel, member_ids, hospital_name)
        return AcceptResult(channel=channel, hospital_name=hospital_name)

    async def reject_match(self, request_id: int, target_id: int) -> bool:
        req = await self.bot.match_requests.get_by_id(request_id)
        if req is None or req.target_id != target_id or req.status != "pending":
            return False
        await self.bot.match_requests.resolve(request_id, "rejected")
        await self.bot.audit.log(
            "match.reject",
            actor_id=target_id,
            target=f"match:{request_id}",
        )
        return True

    # ---------- 매칭 채널 관리 ----------

    async def my_open_channels(
        self, guild: discord.Guild, user_id: int
    ) -> list[discord.TextChannel]:
        """사용자가 참여 중인 열린 매칭 채널(디스코드 객체) 목록."""
        records = await self.bot.match_channels.list_open_for_user(user_id)
        channels: list[discord.TextChannel] = []
        for record in records:
            ch = guild.get_channel(record.discord_channel_id)
            if isinstance(ch, discord.TextChannel):
                channels.append(ch)
        return channels

    async def invite_to_channel(
        self, guild: discord.Guild, inviter_id: int, invitee: discord.Member,
        channel: discord.TextChannel,
    ) -> bool:
        """동료를 매칭 채널에 초대한다 (권한 부여 + DB 등록)."""
        record = await self.bot.match_channels.get_by_discord_id(channel.id)
        if record is None:
            return False
        await channel.set_permissions(
            invitee,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            reason=f"매칭 채널 초대 by {inviter_id}",
        )
        await self.bot.match_channels.add_member(record.id, invitee.id)
        await self.bot.audit.log(
            "match.channel.invite",
            actor_id=inviter_id,
            target=f"match_channel:{record.id}",
            detail=f"invitee={invitee.id}",
        )
        await channel.send(
            f"👋 {invitee.mention} 님이 <@{inviter_id}> 님의 초대로 "
            "이 비공개 매칭 채널에 참여했습니다."
        )
        return True

    async def close_channel(
        self, guild: discord.Guild, user_id: int, channel: discord.TextChannel
    ) -> bool:
        """매칭 채널을 닫는다 (읽기 전용 전환, 기록 보존)."""
        record = await self.bot.match_channels.get_by_discord_id(channel.id)
        if record is None:
            return False
        members = await self.bot.match_channels.list_members(record.id)
        if user_id not in members:
            return False

        await self.bot.match_channels.close(record.id)
        await self.bot.audit.log(
            "match.channel.close",
            actor_id=user_id,
            target=f"match_channel:{record.id}",
        )
        try:
            for member_id in members:
                member = guild.get_member(member_id)
                if member:
                    await channel.set_permissions(member, send_messages=False)
            await channel.send("🔒 이 매칭 채널이 닫혔습니다. 기록은 보존됩니다.")
        except discord.HTTPException:
            logger.exception("매칭 채널 닫기 중 권한 변경 실패: channel=%s", channel.id)
        return True

    # ---------- 내부 헬퍼 ----------

    async def ensure_match_channel(
        self,
        guild: discord.Guild,
        member_ids: Sequence[int],
        hospital_id: Optional[int],
        hospital_name: str,
    ) -> Optional[discord.TextChannel]:
        """두 사용자가 함께 쓸 매칭 채널을 확보한다.

        채널 이름은 병원명 대신 **고유번호** 로 짓고(예: 매칭-12), 병원 정보는
        채널 토픽과 DB(hospital_id)로 관리한다.
        """
        existing = await self.bot.match_channels.find_open_for_users(
            list(member_ids), hospital_id
        )
        if existing is not None:
            channel = guild.get_channel(existing.discord_channel_id)
            if isinstance(channel, discord.TextChannel):
                for user_id in member_ids:
                    member = guild.get_member(user_id)
                    if member is not None:
                        await channel.set_permissions(
                            member,
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            attach_files=True,
                            reason="매칭 채널 참여자 보강",
                        )
                    await self.bot.match_channels.add_member(existing.id, user_id)
                return channel

        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
            ),
        }
        for user_id in member_ids:
            member = guild.get_member(user_id)
            if member is not None:
                overwrites[member] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                )

        category = None
        if self.bot.settings.match_category_id:
            fetched = guild.get_channel(self.bot.settings.match_category_id)
            if isinstance(fetched, discord.CategoryChannel):
                category = fetched

        # 채널 이름은 병원명이 아니라 **고유번호** 로 관리한다.
        # 1) 임시 이름으로 채널을 먼저 만들고,
        # 2) DB 에 등록해 id 를 받은 뒤,
        # 3) 그 id 로 이름(예: 매칭-12)을 바꾼다.
        try:
            channel = await guild.create_text_channel(
                name="매칭-대기",
                category=category,
                overwrites=overwrites,
                topic=(
                    f"MindFinder 매칭 채널 · {hospital_name} · "
                    "참여자끼리 자유롭게 대화하세요."
                ),
                reason="MindFinder 매칭 채널 생성",
            )
        except discord.Forbidden:
            logger.warning("매칭 채널 생성 권한 없음: guild=%s", guild.id)
            return None
        except discord.HTTPException:
            logger.exception("매칭 채널 생성 실패: guild=%s", guild.id)
            return None

        record_id = await self.bot.match_channels.create(
            discord_channel_id=channel.id,
            guild_id=guild.id,
            hospital_id=hospital_id,
            member_ids=list(member_ids),
            name=None,
        )
        name = self.channel_name(record_id)
        try:
            await channel.edit(
                name=name,
                topic=(
                    f"MindFinder 매칭 채널 #{record_id} · {hospital_name} · "
                    "참여자끼리 자유롭게 대화하세요."
                ),
                reason="MindFinder 매칭 채널 이름(고유번호) 반영",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.debug("매칭 채널 이름 변경 실패: channel=%s", channel.id)
        await self.bot.match_channels.set_name(record_id, name)
        return channel

    def channel_name(self, record_id: int) -> str:
        """매칭 채널 이름을 **고유번호** 로 만든다 (예: 매칭-12)."""
        return f"매칭-{record_id}"

    async def announce_match(
        self, channel: discord.TextChannel, member_ids: Sequence[int],
        hospital_name: str
    ) -> None:
        """매칭 채널에 시작 안내 임베드를 남긴다."""
        mentions = " ".join(f"<@{uid}>" for uid in member_ids)
        embed = discord.Embed(
            title="🤝 매칭이 성사되었습니다",
            description=(
                f"같은 병원·시기에 입원했던 동료끼리 연결되었습니다.\n"
                f"병원: {hospital_name}\n\n"
                "이제 이 비공개 채널에서 자유롭게 대화하세요.\n"
                "다른 동료를 더 초대하려면 패널의 '동료 초대' 버튼을 사용하세요.\n"
                "⚠️ 전화번호/SNS 등 개인정보는 본인 판단으로 신중히 공유하세요."
            ),
            color=discord.Color.green(),
        )
        try:
            await channel.send(content=mentions, embed=embed)
        except discord.HTTPException:
            logger.exception("매칭 채널 안내 전송 실패: channel=%s", channel.id)

    @staticmethod
    def mask_name(guild: Optional[discord.Guild], user_id: int) -> str:
        """상대 표시명을 일부 마스킹한다 (기본값)."""
        if guild:
            member = guild.get_member(user_id)
            if member:
                name = member.display_name
                if len(name) <= 2:
                    return name[0] + "*"
                return name[0] + "*" * (len(name) - 2) + name[-1]
        return "익명 동료"

    async def display_name(
        self, guild: Optional[discord.Guild], user_id: int
    ) -> str:
        """매칭 시 상대에게 보여줄 표시명을 **사용자 설정**에 따라 만든다.

        설정(match_name_mode):
            - masked   : 기본 마스킹 (예: 홍*동)
            - real     : 실명(디스코드 표시명) 그대로
            - nickname : 병원에서 불린 닉네임 (직접 입력)
        """
        mode, nickname = "masked", None
        try:
            mode, nickname = await self.bot.display_prefs.get_match_name(user_id)
        except Exception:  # noqa: BLE001
            logger.exception("표시명 설정 조회 실패: user=%s", user_id)
        if mode == "nickname" and nickname:
            return nickname
        if mode == "real":
            if guild:
                member = guild.get_member(user_id)
                if member:
                    return member.display_name
            return "익명 동료"
        return self.mask_name(guild, user_id)
