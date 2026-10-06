"""매칭 코그: 매칭 희망 설정, 후보 찾기, 신청/수락/거절, 비공개 채널 연결.

중개자 원칙: 봇은 전화번호/SNS를 저장하거나 전달하지 않는다.
양쪽이 동의하면 **비공개 채널**을 만들어 참여자들을 초대하고,
그 안에서 직접 대화하도록 안내한다.

1:1 뿐 아니라 여러 명이 하나의 채널을 공유할 수 있다.
- 이미 같은 병원으로 함께 있는 열린 채널이 있으면 새로 만들지 않고 재사용한다.
- `/매칭초대` 로 동료를 기존 채널에 추가할 수 있다.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot

logger = logging.getLogger(__name__)


class MatchingCog(commands.Cog):
    """매칭 관련 명령."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot

    # ---------- 매칭 희망 설정 ----------

    @app_commands.command(name="매칭", description="매칭 희망 여부를 설정합니다.")
    @app_commands.describe(설정="on 이면 매칭을 원함, off 이면 원하지 않음")
    @app_commands.choices(
        설정=[
            app_commands.Choice(name="on (매칭 원함)", value="on"),
            app_commands.Choice(name="off (매칭 안 함)", value="off"),
        ]
    )
    async def set_matching(
        self, interaction: discord.Interaction, 설정: app_commands.Choice[str]
    ) -> None:
        await self.bot.users.ensure_user(interaction.user.id)
        enabled = 설정.value == "on"
        await self.bot.match_prefs.set_enabled(interaction.user.id, enabled)
        await self.bot.audit.log(
            "match_preference.set",
            actor_id=interaction.user.id,
            detail="on" if enabled else "off",
        )
        msg = (
            "🟢 매칭을 **원함**으로 설정했습니다. `/매칭찾기` 로 동료를 찾아보세요."
            if enabled
            else "⚪ 매칭을 **원하지 않음**으로 설정했습니다."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    # ---------- 매칭 후보 찾기 ----------

    @app_commands.command(
        name="매칭찾기", description="같은 병원·시기에 입원했던 동료를 찾습니다."
    )
    async def find_matches(self, interaction: discord.Interaction) -> None:
        # 내 이력 확인
        my_admissions = await self.bot.admissions.list_by_user(interaction.user.id)
        if not my_admissions:
            await interaction.response.send_message(
                "먼저 `/인증` 으로 입원 이력을 등록해주세요.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        # 후보 수집 (병원 + 기간 겹침, 본인 제외)
        candidates: dict[int, list] = {}  # user_id -> [(my_adm, other_adm)]
        for my_adm in my_admissions:
            others = await self.bot.admissions.find_overlapping(
                hospital_id=my_adm.hospital_id,
                admit_ym_val=my_adm.admit_ym,
                discharge_ym_val=my_adm.discharge_ym,
                exclude_user_id=interaction.user.id,
            )
            for other in others:
                candidates.setdefault(other.user_id, []).append((my_adm, other))

        if not candidates:
            await interaction.followup.send(
                "아직 같은 병원·시기로 등록된 동료가 없습니다. "
                "커뮤니티 채널에서 기다리거나 나중에 다시 시도해보세요.",
                ephemeral=True,
            )
            return

        # 매칭을 원하는 상대만 필터링
        matched: list[tuple[int, list]] = []
        for user_id, pairs in candidates.items():
            if await self.bot.match_prefs.is_enabled(user_id):
                matched.append((user_id, pairs))

        if not matched:
            await interaction.followup.send(
                f"같은 병원·시기 동료 {len(candidates)}명을 찾았지만, "
                "현재 매칭을 원하는 분이 없습니다.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔍 매칭 후보",
            description=(
                f"같은 병원·시기에 입원했고 매칭을 원하는 동료 "
                f"**{len(matched)}명**을 찾았습니다.\n"
                "아래 명령으로 매칭을 신청할 수 있습니다."
            ),
            color=discord.Color.green(),
        )
        for idx, (user_id, pairs) in enumerate(matched, start=1):
            my_adm, other_adm = pairs[0]
            # 상대 개인정보 보호를 위해 부분 마스킹한 표시명만 노출
            display = self._mask_name(interaction, user_id)
            embed.add_field(
                name=f"후보 {idx} · {display}",
                value=(
                    f"병원: {other_adm.hospital.name}\n"
                    f"기간: {other_adm.period_text()}\n"
                    f"`/매칭신청 대상:후보 {idx}`"
                ),
                inline=False,
            )
        # 후보 매핑을 임시 저장 (신청 시 참조)
        self.bot._match_candidate_cache = getattr(self.bot, "_match_candidate_cache", {})
        self.bot._match_candidate_cache[interaction.user.id] = matched
        await interaction.followup.send(embed=embed, ephemeral=True)

    def _mask_name(self, interaction: discord.Interaction, user_id: int) -> str:
        """상대 표시명을 일부 마스킹한다."""
        if interaction.guild:
            member = interaction.guild.get_member(user_id)
            if member:
                name = member.display_name
                if len(name) <= 2:
                    return name[0] + "*"
                return name[0] + "*" * (len(name) - 2) + name[-1]
        return "익명 동료"

    # ---------- 매칭 신청 ----------

    @app_commands.command(name="매칭신청", description="매칭 후보에게 연락을 신청합니다.")
    @app_commands.describe(대상="'/매칭찾기' 결과에 표시된 후보 번호 (예: 1)")
    async def request_match(
        self, interaction: discord.Interaction, 대상: int
    ) -> None:
        cache = getattr(self.bot, "_match_candidate_cache", {}).get(
            interaction.user.id, []
        )
        if not cache:
            await interaction.response.send_message(
                "먼저 `/매칭찾기` 로 후보를 확인해주세요.", ephemeral=True
            )
            return
        if not (1 <= 대상 <= len(cache)):
            await interaction.response.send_message(
                f"후보 번호는 1 ~ {len(cache)} 사이여야 합니다.", ephemeral=True
            )
            return

        target_id, pairs = cache[대상 - 1]
        my_adm, other_adm = pairs[0]

        # 이미 대기 중인 신청이 있는지 확인
        existing = await self.bot.match_requests.find_pending_between(
            interaction.user.id, target_id
        )
        if existing is not None:
            await interaction.response.send_message(
                "이미 해당 동료에게 보낸 매칭 신청이 대기 중입니다.", ephemeral=True
            )
            return

        request_id = await self.bot.match_requests.create(
            requester_id=interaction.user.id,
            target_id=target_id,
            admission_id=my_adm.id,
        )
        await self.bot.audit.log(
            "match.request",
            actor_id=interaction.user.id,
            target=f"match:{request_id}",
            detail=f"target={target_id}",
        )

        # 상대에게 신청 도착 알림
        await self._notify_match_request(interaction, target_id, request_id, other_adm)
        await interaction.response.send_message(
            "✅ 매칭 신청을 보냈습니다. 상대가 수락하면 비공개 채널에서 대화할 수 있습니다.",
            ephemeral=True,
        )

    async def _notify_match_request(
        self, interaction: discord.Interaction, target_id: int,
        request_id: int, admission
    ) -> None:
        """상대방에게 매칭 신청 도착을 DM 으로 알린다.

        DM 이 차단되어 있어도 신청 자체는 유효하며, `/매칭요청함` 에서 확인할 수 있다.
        """
        try:
            target_user = await self.bot.fetch_user(target_id)
            embed = discord.Embed(
                title="💌 새 매칭 신청이 도착했습니다",
                description=(
                    f"같은 병원·시기에 입원했던 동료가 매칭을 신청했습니다.\n"
                    f"병원: {admission.hospital.name}\n"
                    f"기간: {admission.period_text()}\n\n"
                    f"`/매칭요청함` 에서 수락/거절할 수 있습니다."
                ),
                color=discord.Color.pink(),
            )
            await target_user.send(embed=embed)
        except discord.Forbidden:
            logger.info("DM 차단으로 매칭 알림 전송 실패: target=%s", target_id)

    # ---------- 받은 신청 처리 ----------

    @app_commands.command(name="매칭요청함", description="나에게 온 매칭 신청을 확인합니다.")
    async def incoming_requests(self, interaction: discord.Interaction) -> None:
        pending = await self.bot.match_requests.list_incoming_pending(
            interaction.user.id
        )
        if not pending:
            await interaction.response.send_message(
                "받은 매칭 신청이 없습니다.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📬 받은 매칭 신청 ({len(pending)}건)",
            description="`/매칭수락 신청번호:N` 또는 `/매칭거절 신청번호:N`",
            color=discord.Color.pink(),
        )
        for req in pending:
            embed.add_field(
                name=f"신청 #{req.id}",
                value=f"보낸 사람: <@{req.requester_id}>\n`/매칭수락 신청번호:{req.id}`",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="매칭수락", description="받은 매칭 신청을 수락하고 비공개 채널로 연결합니다."
    )
    @app_commands.describe(신청번호="수락할 매칭 신청 번호")
    async def accept_match(
        self, interaction: discord.Interaction, 신청번호: int
    ) -> None:
        req = await self.bot.match_requests.get_by_id(신청번호)
        if req is None or req.target_id != interaction.user.id or req.status != "pending":
            await interaction.response.send_message(
                "유효한 매칭 신청이 아닙니다.", ephemeral=True
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        await self.bot.match_requests.resolve(신청번호, "accepted")
        await self.bot.audit.log(
            "match.accept",
            actor_id=interaction.user.id,
            target=f"match:{신청번호}",
        )

        # 신청자의 입원 이력에서 병원 정보를 얻는다.
        hospital_id: Optional[int] = None
        hospital_name = "미상 병원"
        if req.admission_id is not None:
            admission = await self.bot.admissions.get_by_id(req.admission_id)
            if admission is not None:
                hospital_id = admission.hospital_id
                hospital_name = admission.hospital.name

        member_ids = [req.requester_id, req.target_id]

        # 이미 같은 병원으로 함께 있는 열린 채널이 있으면 재사용한다.
        channel = await self._ensure_match_channel(
            interaction.guild, member_ids, hospital_id, hospital_name
        )

        if channel is None:
            await interaction.followup.send(
                "⚠️ 비공개 채널을 만들 권한이 없어 매칭 채널 생성에 실패했습니다. "
                "봇에게 '채널 관리' 권한이 있는지 확인해주세요.",
                ephemeral=True,
            )
            return

        await self._announce_match(channel, member_ids, hospital_name)
        await interaction.followup.send(
            f"✅ 매칭을 수락했습니다. {channel.mention} 에서 대화를 시작하세요.",
            ephemeral=True,
        )

    @app_commands.command(name="매칭거절", description="받은 매칭 신청을 거절합니다.")
    @app_commands.describe(신청번호="거절할 매칭 신청 번호")
    async def reject_match(
        self, interaction: discord.Interaction, 신청번호: int
    ) -> None:
        req = await self.bot.match_requests.get_by_id(신청번호)
        if req is None or req.target_id != interaction.user.id or req.status != "pending":
            await interaction.response.send_message(
                "유효한 매칭 신청이 아닙니다.", ephemeral=True
            )
            return

        await self.bot.match_requests.resolve(신청번호, "rejected")
        await self.bot.audit.log(
            "match.reject",
            actor_id=interaction.user.id,
            target=f"match:{신청번호}",
        )
        await interaction.response.send_message(
            "매칭 신청을 거절했습니다.", ephemeral=True
        )

    # ---------- 매칭 채널 관리 ----------

    @app_commands.command(
        name="매칭채널", description="내가 참여 중인 매칭 비공개 채널 목록을 봅니다."
    )
    async def my_channels(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        channels = await self._my_open_channels(interaction.guild, interaction.user.id)
        if not channels:
            await interaction.response.send_message(
                "참여 중인 매칭 채널이 없습니다.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🔒 내 매칭 채널 ({len(channels)}개)",
            color=discord.Color.blurple(),
        )
        for ch in channels:
            embed.add_field(name=ch.name, value=ch.mention, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="매칭초대", description="같은 병원·시기 동료를 내 매칭 채널에 초대합니다."
    )
    @app_commands.describe(초대할사람="내 매칭 채널에 초대할 동료")
    async def invite_to_channel(
        self, interaction: discord.Interaction, 초대할사람: discord.Member
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        if 초대할사람.id == interaction.user.id or 초대할사람.bot:
            await interaction.response.send_message(
                "올바른 대상을 지정해주세요.", ephemeral=True
            )
            return

        # 초대자가 참여 중인 열린 매칭 채널을 고른다.
        # 명령을 매칭 채널 안에서 실행했다면 그 채널을 우선 사용한다.
        target_channel: Optional[discord.TextChannel] = None
        if isinstance(interaction.channel, discord.TextChannel):
            record = await self.bot.match_channels.get_by_discord_id(
                interaction.channel.id
            )
            if record is not None and record.status == "open":
                members = await self.bot.match_channels.list_members(record.id)
                if interaction.user.id in members:
                    target_channel = interaction.channel

        if target_channel is None:
            my_channels = await self._my_open_channels(
                interaction.guild, interaction.user.id
            )
            if not my_channels:
                await interaction.response.send_message(
                    "먼저 매칭을 성사시켜 비공개 채널에 참여하거나, "
                    "매칭 채널 안에서 이 명령을 실행해주세요.",
                    ephemeral=True,
                )
                return
            if len(my_channels) == 1:
                target_channel = my_channels[0]
            else:
                listing = ", ".join(ch.mention for ch in my_channels)
                await interaction.response.send_message(
                    f"참여 중인 매칭 채널이 여러 개입니다. 초대할 채널에서 "
                    f"이 명령을 실행해주세요: {listing}",
                    ephemeral=True,
                )
                return

        # 채널 접근 권한 부여 + DB 등록
        record = await self.bot.match_channels.get_by_discord_id(target_channel.id)
        if record is None:
            await interaction.response.send_message(
                "매칭 채널 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return

        await target_channel.set_permissions(
            초대할사람,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            reason=f"매칭 채널 초대 by {interaction.user}",
        )
        await self.bot.match_channels.add_member(record.id, 초대할사람.id)
        await self.bot.audit.log(
            "match.channel.invite",
            actor_id=interaction.user.id,
            target=f"match_channel:{record.id}",
            detail=f"invitee={초대할사람.id}",
        )

        await target_channel.send(
            f"👋 {초대할사람.mention} 님이 {interaction.user.mention} 님의 초대로 "
            "이 비공개 매칭 채널에 참여했습니다."
        )
        await interaction.response.send_message(
            f"✅ {초대할사람.mention} 님을 {target_channel.mention} 에 초대했습니다.",
            ephemeral=True,
        )

    @app_commands.command(name="매칭종료", description="매칭 채널을 닫고 보관합니다.")
    @app_commands.describe(채널="닫을 매칭 채널 (기본: 현재 채널)")
    async def close_channel(
        self,
        interaction: discord.Interaction,
        채널: Optional[discord.TextChannel] = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        target = 채널 or (
            interaction.channel
            if isinstance(interaction.channel, discord.TextChannel)
            else None
        )
        if target is None:
            await interaction.response.send_message(
                "닫을 매칭 채널을 지정해주세요.", ephemeral=True
            )
            return

        record = await self.bot.match_channels.get_by_discord_id(target.id)
        if record is None:
            await interaction.response.send_message(
                "해당 채널은 매칭 채널이 아닙니다.", ephemeral=True
            )
            return

        members = await self.bot.match_channels.list_members(record.id)
        if interaction.user.id not in members:
            await interaction.response.send_message(
                "이 매칭 채널의 참여자만 닫을 수 있습니다.", ephemeral=True
            )
            return

        await self.bot.match_channels.close(record.id)
        await self.bot.audit.log(
            "match.channel.close",
            actor_id=interaction.user.id,
            target=f"match_channel:{record.id}",
        )

        await interaction.response.send_message(
            f"🔒 {target.mention} 매칭 채널을 닫았습니다. "
            "필요하면 관리자가 다시 열 수 있습니다.",
            ephemeral=True,
        )
        # 닫힘 표시: 읽기 전용으로 전환 (기록은 보존)
        try:
            for member_id in members:
                member = interaction.guild.get_member(member_id)
                if member:
                    await target.set_permissions(member, send_messages=False)
            await target.send("🔒 이 매칭 채널이 닫혔습니다. 기록은 보존됩니다.")
        except discord.HTTPException:
            logger.exception("매칭 채널 닫기 중 권한 변경 실패: channel=%s", target.id)

    # ---------- 내부 헬퍼 ----------

    async def _my_open_channels(
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

    async def _ensure_match_channel(
        self,
        guild: discord.Guild,
        member_ids: Sequence[int],
        hospital_id: Optional[int],
        hospital_name: str,
    ) -> Optional[discord.TextChannel]:
        """두 사용자가 함께 쓸 매칭 채널을 확보한다.

        - 같은 병원으로 이미 함께 있는 열린 채널이 있으면 재사용한다.
        - 없으면 새 비공개 채널을 만들어 참여자들을 초대하고 DB에 등록한다.
        - 기존 채널을 재사용하는 경우, 누락된 참여자의 접근 권한을 보강한다.
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

        # 새 채널 생성
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

        name = self._channel_name(hospital_name)
        try:
            channel = await guild.create_text_channel(
                name=name,
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

        await self.bot.match_channels.create(
            discord_channel_id=channel.id,
            guild_id=guild.id,
            hospital_id=hospital_id,
            member_ids=list(member_ids),
        )
        return channel

    def _channel_name(self, hospital_name: str) -> str:
        """디스코드 채널 이름 규칙에 맞게 매칭 채널 이름을 만든다."""
        safe = "".join(
            ch if ch.isalnum() else "-" for ch in hospital_name
        ).strip("-")
        safe = safe or "동료"
        return f"매칭-{safe}"[:90]

    async def _announce_match(
        self, channel: discord.TextChannel, member_ids: Sequence[int], hospital_name: str
    ) -> None:
        """매칭 채널에 시작 안내 임베드를 남긴다."""
        mentions = " ".join(f"<@{uid}>" for uid in member_ids)
        embed = discord.Embed(
            title="🤝 매칭이 성사되었습니다",
            description=(
                f"같은 병원·시기에 입원했던 동료끼리 연결되었습니다.\n"
                f"병원: {hospital_name}\n\n"
                "이제 이 비공개 채널에서 자유롭게 대화하세요.\n"
                "다른 동료를 더 초대하려면 `/매칭초대` 를 사용하세요.\n"
                "⚠️ 전화번호/SNS 등 개인정보는 본인 판단으로 신중히 공유하세요."
            ),
            color=discord.Color.green(),
        )
        try:
            await channel.send(content=mentions, embed=embed)
        except discord.HTTPException:
            logger.exception("매칭 채널 안내 전송 실패: channel=%s", channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MatchingCog(bot))
