"""위기 대응 코그: 위험 키워드 감지 시 상담 안내를 제공한다.

정신건강 커뮤니티 특성상 자해/자살 관련 표현이 감지되면
전문 상담 핫라인 안내를 조용히(비공개적) 제공한다.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

# 위험 키워드 (필요 시 확장)
CRISIS_KEYWORDS = [
    "자살", "자해", "죽고싶", "죽고 싶", "죽을래", "사라지고 싶",
    "살기싫", "살기 싫", "목숨", "극단적", "번개탄", "유서",
]

# 상담 안내 (한국 기준)
CRISIS_RESOURCES = (
    "**자살예방상담전화: 109** (24시간)\n"
    "**정신건강상담전화: 1577-0199** (24시간)\n"
    "**생명의전화: 1588-9191**\n"
    "**응급 상황 시: 119**\n\n"
    "혼자 감당하지 않아도 됩니다. 지금 바로 연락해보세요."
)


class CrisisCog(commands.Cog):
    """위기 대응 관련 명령 및 메시지 감지."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot

    @app_commands.command(name="도움", description="지금 힘들 때 연락할 수 있는 상담 안내를 봅니다.")
    async def crisis_help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="💙 도움이 필요하신가요",
            description=CRISIS_RESOURCES,
            color=discord.Color.from_rgb(90, 160, 220),
        )
        embed.set_footer(text="당신의 이야기를 들어줄 사람들이 있습니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """메시지에서 위험 키워드를 감지하면 안내를 남긴다."""
        # 봇/자기 메시지 무시
        if message.author.bot:
            return
        # DM 은 무시 (길드 채널에서만 동작)
        if message.guild is None:
            return

        content = message.content or ""
        if not content:
            return

        if any(keyword in content for keyword in CRISIS_KEYWORDS):
            logger.info(
                "위기 키워드 감지: user=%s guild=%s",
                message.author.id,
                message.guild.id,
            )
            embed = discord.Embed(
                title="💙 곁에 도움을 줄 사람들이 있습니다",
                description=CRISIS_RESOURCES,
                color=discord.Color.from_rgb(90, 160, 220),
            )
            try:
                await message.reply(embed=embed, mention_author=False)
            except discord.HTTPException:
                logger.warning("위기 안내 응답 실패: channel=%s", message.channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CrisisCog(bot))
