"""커뮤니티 코그: 전체/병원/기간 단위 커뮤니티(채널) 안내 및 동기화.

설계:
- **전체 커뮤니티**는 1단계(자기 신고) 이상이면 이용하는 고정 채널.
- **병원·기간 커뮤니티**는 병원 카테고리 안의 **월 단위 비공개 채널**로 운영한다.
  - 서류/지식 인증(2단계)을 받은 병원에 한해,
  - 내 입원 기간에 걸치는 월 채널이 열린다(다른 기간은 보이지 않음).
- 이 코그는 내가 접근 가능한 기간 채널을 확인/동기화하고 목록을 보여준다.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.services.channels import COMMUNITY_CHANNEL_KEY
from bot.utils.hospitals import hospital_display_name, resolve_canonical

logger = logging.getLogger(__name__)


class CommunityCog(commands.Cog):
    """커뮤니티 관련 명령."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="커뮤니티",
        description="내가 이용할 수 있는 병원·기간 커뮤니티(채널)를 확인합니다.",
    )
    @app_commands.describe(
        병원="확인할 병원 (내 인증 이력 중 하나, 비우면 전체 안내)",
    )
    async def community(
        self,
        interaction: discord.Interaction,
        병원: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 1) 내 기간 채널 접근을 먼저 동기화한다 (인증 이력 기반).
        try:
            await self.bot.channel_service.ensure_community_access(
                interaction.guild, interaction.user.id
            )
        except Exception:  # noqa: BLE001
            logger.exception("커뮤니티 접근 동기화 실패: user=%s", interaction.user.id)

        hospital_name = resolve_canonical(병원) if 병원 else None
        embed = await self._build_embed(interaction, hospital_name)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _build_embed(
        self, interaction: discord.Interaction, hospital_name: str | None
    ) -> discord.Embed:
        """내가 접근 가능한 기간 채널 목록을 담은 임베드를 만든다."""
        guild = interaction.guild
        admissions = await self.bot.admissions.list_by_user(interaction.user.id)

        embed = discord.Embed(
            title="🏥 내 병원·기간 커뮤니티",
            description=(
                "전체 커뮤니티는 1단계 인증이면 이용할 수 있고,\n"
                "**병원·기간 커뮤니티**는 서류/지식 인증(2단계)을 받은 병원의 "
                "내 입원 기간에 해당하는 **비공개 월 채널**로 자동으로 열립니다.\n"
                "(같은 기간에 입원했던 인증자에게만 보입니다.)"
            ),
            color=discord.Color.teal(),
        )

        # 전체 커뮤니티 채널 링크
        community_ch = await self.bot.channel_service.managed_text_channel(
            guild, COMMUNITY_CHANNEL_KEY
        )
        if community_ch is not None:
            embed.add_field(
                name="전체 커뮤니티",
                value=community_ch.mention,
                inline=False,
            )

        if not admissions:
            embed.add_field(
                name="내 이력 없음",
                value="먼저 ‘입원 이력 등록’으로 이력을 등록해주세요.",
                inline=False,
            )
            return embed

        found_any = False
        for adm in admissions:
            if hospital_name is not None and adm.hospital.name != hospital_name:
                continue
            # 인증(verified)을 받은 이력만 병원·기간 커뮤니티가 열린다.
            if adm.verification != "verified":
                continue
            months = self.bot.channel_service.months_for_admission(adm)
            labels: list[str] = []
            for ym_value in months:
                channel = await self.bot.channel_service.ensure_period_channel(
                    guild,
                    adm.hospital_id,
                    adm.hospital.name,
                    ym_value,
                    with_role=True,
                )
                if channel is None:
                    continue
                labels.append(channel.mention)
            if labels:
                found_any = True
                embed.add_field(
                    name=hospital_display_name(adm.hospital.name),
                    value=" · ".join(labels[:25]),
                    inline=False,
                )

        if not found_any:
            embed.add_field(
                name="표시할 기간 채널 없음",
                value=(
                    "먼저 **서류 인증 또는 지식기반 인증**을 완료하세요. "
                    "인증이 완료되면 해당 병원·기간 채널이 자동으로 열립니다."
                ),
                inline=False,
            )
        return embed

    @community.autocomplete("병원")
    async def _hospital_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        hospitals = await self.bot.hospitals.list_all()
        current = (current or "").strip()
        choices: list[app_commands.Choice[str]] = []
        for hospital in hospitals:
            name = hospital.name
            if current and current not in name:
                continue
            choices.append(app_commands.Choice(name=name, value=name))
            if len(choices) >= 25:
                break
        return choices


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommunityCog(bot))
