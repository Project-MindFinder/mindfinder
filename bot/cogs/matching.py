"""매칭 코그: 매칭 희망 설정, 후보 찾기, 신청/수락/거절, 비공개 채널 연결.

중개자 원칙: 봇은 전화번호/SNS를 저장하거나 전달하지 않는다.
양쪽이 동의하면 **비공개 채널**을 만들어 참여자들을 초대하고,
그 안에서 직접 대화하도록 안내한다.

핵심 로직은 bot/services/matching.py 에 위임한다.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.services.matching import MatchingService

logger = logging.getLogger(__name__)


class MatchingCog(commands.Cog):
    """매칭 관련 명령."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot
        self.service = MatchingService(bot)

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
        enabled = 설정.value == "on"
        await self.service.set_matching(interaction.user.id, enabled)
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
        try:
            matched, total = await self.service.find_candidates(interaction.user.id)
        except ValueError:
            await interaction.response.send_message(
                "먼저 `/인증` 으로 입원 이력을 등록해주세요.", ephemeral=True
            )
            return

        if not matched:
            reason = (
                f"같은 병원·시기 동료 {total}명을 찾았지만, "
                "현재 매칭을 원하는 분이 없습니다."
                if total
                else "아직 같은 병원·시기로 등록된 동료가 없습니다."
            )
            await interaction.response.send_message(reason, ephemeral=True)
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
        for idx, cand in enumerate(matched, start=1):
            my_adm, other_adm = cand.pairs[0]
            display = self.service.mask_name(interaction.guild, cand.user_id)
            embed.add_field(
                name=f"후보 {idx} · {display}",
                value=(
                    f"병원: {other_adm.hospital.name}\n"
                    f"기간: {other_adm.period_text()}\n"
                    f"`/매칭신청 대상:{idx}`"
                ),
                inline=False,
            )
        # 후보 매핑을 임시 저장 (신청 시 참조)
        cache = getattr(self.bot, "_match_candidate_cache", {})
        cache[interaction.user.id] = matched
        self.bot._match_candidate_cache = cache
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        cand = cache[대상 - 1]
        my_adm, other_adm = cand.pairs[0]
        request_id, already = await self.service.request_match(
            requester_id=interaction.user.id,
            target_id=cand.user_id,
            admission_id=my_adm.id,
        )
        if already:
            await interaction.response.send_message(
                "이미 해당 동료에게 보낸 매칭 신청이 대기 중입니다.", ephemeral=True
            )
            return

        await self._notify_match_request(interaction, cand.user_id, request_id, other_adm)
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
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.service.accept_match(
            신청번호, interaction.user.id, interaction.guild
        )
        if result.error == "invalid":
            await interaction.followup.send(
                "유효한 매칭 신청이 아닙니다.", ephemeral=True
            )
            return
        if result.channel is None:
            await interaction.followup.send(
                "⚠️ 비공개 채널을 만들 권한이 없어 매칭 채널 생성에 실패했습니다. "
                "봇에게 '채널 관리' 권한이 있는지 확인해주세요.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ 매칭을 수락했습니다. {result.channel.mention} 에서 대화를 시작하세요.",
            ephemeral=True,
        )

    @app_commands.command(name="매칭거절", description="받은 매칭 신청을 거절합니다.")
    @app_commands.describe(신청번호="거절할 매칭 신청 번호")
    async def reject_match(
        self, interaction: discord.Interaction, 신청번호: int
    ) -> None:
        ok = await self.service.reject_match(신청번호, interaction.user.id)
        if not ok:
            await interaction.response.send_message(
                "유효한 매칭 신청이 아닙니다.", ephemeral=True
            )
            return
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

        channels = await self.service.my_open_channels(
            interaction.guild, interaction.user.id
        )
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

        target_channel = await self._pick_channel(interaction)
        if target_channel is None:
            return

        ok = await self.service.invite_to_channel(
            interaction.guild, interaction.user.id, 초대할사람, target_channel
        )
        if not ok:
            await interaction.response.send_message(
                "매칭 채널 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ {초대할사람.mention} 님을 {target_channel.mention} 에 초대했습니다.",
            ephemeral=True,
        )

    async def _pick_channel(
        self, interaction: discord.Interaction
    ) -> Optional[discord.TextChannel]:
        """초대 대상 매칭 채널을 고른다. 응답을 이미 보낸 경우 None."""
        # 현재 채널이 매칭 채널이고 내가 참여자라면 우선 사용
        if isinstance(interaction.channel, discord.TextChannel):
            record = await self.bot.match_channels.get_by_discord_id(
                interaction.channel.id
            )
            if record is not None and record.status == "open":
                members = await self.bot.match_channels.list_members(record.id)
                if interaction.user.id in members:
                    return interaction.channel

        my_channels = await self.service.my_open_channels(
            interaction.guild, interaction.user.id
        )
        if not my_channels:
            await interaction.response.send_message(
                "먼저 매칭을 성사시켜 비공개 채널에 참여하거나, "
                "매칭 채널 안에서 이 명령을 실행해주세요.",
                ephemeral=True,
            )
            return None
        if len(my_channels) == 1:
            return my_channels[0]
        listing = ", ".join(ch.mention for ch in my_channels)
        await interaction.response.send_message(
            f"참여 중인 매칭 채널이 여러 개입니다. 초대할 채널에서 "
            f"이 명령을 실행해주세요: {listing}",
            ephemeral=True,
        )
        return None

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

        ok = await self.service.close_channel(
            interaction.guild, interaction.user.id, target
        )
        if not ok:
            await interaction.response.send_message(
                "이 매칭 채널의 참여자만 닫을 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🔒 {target.mention} 매칭 채널을 닫았습니다.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MatchingCog(bot))
