"""커뮤니티 코그: 전체/병원/기간 단위 커뮤니티 채널 안내 및 동적 스레드 생성.

혼합 전략:
- 전체·대표 병원은 고정 채널로 운영 (관리자가 미리 생성)
- 세부 기간·개별 병원은 봇이 스레드/포럼으로 동적 처리
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot

logger = logging.getLogger(__name__)


class CommunityCog(commands.Cog):
    """커뮤니티 관련 명령."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot

    @app_commands.command(
        name="커뮤니티", description="병원·기간 단위 커뮤니티 스레드를 열거나 안내합니다."
    )
    @app_commands.describe(
        병원="커뮤니티를 열 병원 (내 이력 중 하나, 비우면 전체 안내)",
        입원연도="기간 기준 연도 (선택)",
    )
    async def community(
        self,
        interaction: discord.Interaction,
        병원: str | None = None,
        입원연도: int | None = None,
    ) -> None:
        # 병원 미지정: 전체 커뮤니티 안내
        if 병원 is None:
            embed = discord.Embed(
                title="🏥 MindFinder 커뮤니티 안내",
                description=(
                    "전체 커뮤니티 채널에서 자유롭게 이야기하고,\n"
                    "`/커뮤니티 병원:병원명` 으로 해당 병원 전용 스레드를 열 수 있습니다."
                ),
                color=discord.Color.teal(),
            )
            embed.add_field(
                name="사용 예",
                value=(
                    "`/커뮤니티 병원:국립정신건강센터` — 해당 병원 커뮤니티 스레드 열기\n"
                    "`/커뮤니티 병원:국립정신건강센터 입원연도:2020` — 2020년 기간 스레드"
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # 병원 정규화
        from bot.utils.hospitals import resolve_canonical

        canonical = resolve_canonical(병원)
        title = canonical if 입원연도 is None else f"{canonical} · {입원연도}년"

        # 스레드 생성 (현재 채널이 포럼이면 포럼 글, 아니면 스레드)
        channel = interaction.channel
        try:
            if isinstance(channel, discord.ForumChannel):
                thread_with_message = await channel.create_thread(
                    name=title,
                    content=f"{title} 커뮤니티입니다. 자유롭게 이야기 나눠주세요.",
                )
                thread = thread_with_message.thread
            elif isinstance(channel, discord.TextChannel):
                thread = await channel.create_thread(
                    name=title,
                    type=discord.ChannelType.public_thread,
                )
                await thread.send(f"{title} 커뮤니티입니다. 자유롭게 이야기 나눠주세요.")
            else:
                await interaction.response.send_message(
                    "⚠️ 이 채널에서는 스레드를 만들 수 없습니다. 포럼/텍스트 채널에서 사용해주세요.",
                    ephemeral=True,
                )
                return
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ 스레드를 만들 권한이 없습니다. 관리자에게 문의해주세요.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            logger.warning("스레드 생성 실패: %s", exc)
            await interaction.response.send_message(
                "⚠️ 스레드 생성에 실패했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        await self.bot.audit.log(
            "community.thread",
            actor_id=interaction.user.id,
            detail=title,
        )
        await interaction.response.send_message(
            f"✅ 커뮤니티 스레드를 열었습니다: {thread.mention}", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CommunityCog(bot))
