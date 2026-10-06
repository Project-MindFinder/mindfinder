"""지식기반 인증 코그.

이미 서류 인증을 마친 사용자가, 자신이 입원했던 시기·병원에 실제로 있었던
사람만 알 수 있는 질문을 올리고, 응답자의 답변을 판정한 뒤 관리자가 최종
승인하면 응답자에게 해당 병원 인증을 부여한다.

- /지식인증        : (서류 인증자) 대상자에게 질문 보내기
- /지식인증목록    : 내가 받은 지식기반 인증 질문 확인
- /지식인증심사    : [관리자] 최종 승인 대기 목록
- /지식인증승인    : [관리자] 최종 승인
- /지식인증거절    : [관리자] 최종 거절
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.ui.knowledge import (
    AskQuestionView,
    PENDING_REVIEW_STATUS,
    register_persistent_views,
)

logger = logging.getLogger(__name__)

# 지식기반 인증 상태 표시
STATUS_LABEL = {
    "awaiting_answer": "⏳ 응답 대기",
    "awaiting_verdict": "🕵️ 판정 대기",
    "asker_confirmed": "🔔 관리자 승인 대기",
    "asker_rejected": "↩️ 오답 처리",
    "approved": "🟢 승인 완료",
    "rejected": "🔴 거절",
}


class KnowledgeCog(commands.Cog):
    """지식기반 인증 관련 명령."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot
        self._views_ready = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._views_ready:
            return
        self._views_ready = True
        try:
            await register_persistent_views(self.bot)
        except Exception:  # noqa: BLE001 - 영구 뷰 재등록 실패가 전체를 막지 않도록
            logger.exception("지식기반 인증 영구 뷰 재등록 실패")

    # ---------- asker: 질문 보내기 ----------

    @app_commands.command(
        name="지식인증",
        description="(서류 인증자) 같은 시기·병원에 있었는지 확인할 질문을 보냅니다.",
    )
    @app_commands.describe(대상="질문을 보낼 상대 (같은 시기·병원 경험자)")
    async def ask(
        self, interaction: discord.Interaction, 대상: discord.Member
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if 대상.bot or 대상.id == interaction.user.id:
            await interaction.followup.send(
                "⚠️ 자기 자신이나 봇에게는 질문할 수 없습니다.", ephemeral=True
            )
            return

        # 서류 인증(verified)을 마친 본인 이력만 질문 대상이 된다.
        admissions = [
            a
            for a in await self.bot.admissions.list_by_user(interaction.user.id)
            if a.verification == "verified"
        ]
        if not admissions:
            await interaction.followup.send(
                "⚠️ 서류 인증을 마친 이력이 있어야 질문할 수 있습니다. "
                "먼저 서류 인증을 진행해주세요.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "질문할 본인의 인증된 이력을 선택한 뒤, 질문을 작성해주세요.",
            view=AskQuestionView(self.bot, admissions, 대상.id),
            ephemeral=True,
        )

    # ---------- respondent: 받은 질문 ----------

    @app_commands.command(
        name="지식인증목록", description="내가 받은 지식기반 인증 질문을 확인합니다."
    )
    async def my_requests(self, interaction: discord.Interaction) -> None:
        from bot.ui.knowledge import AnswerRequestView

        pending = await self.bot.knowledge_service.list_for_respondent(
            interaction.user.id, "awaiting_answer"
        )
        if not pending:
            await interaction.response.send_message(
                "답변을 기다리는 지식기반 인증 질문이 없습니다.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"❓ 받은 지식기반 인증 질문 ({len(pending)}건)",
            description="아래 버튼으로 답변을 작성하세요.",
            color=discord.Color.blurple(),
        )
        for req in pending[:5]:
            hospital = await self.bot.knowledge_service.hospital_name(req.hospital_id)
            embed.add_field(
                name=f"#{req.id} · {hospital} {req.period_text}",
                value=f"> {req.question}"[:1000],
                inline=False,
            )
        await interaction.response.send_message(
            embed=embed,
            view=AnswerRequestView(self.bot, pending[0].id),
            ephemeral=True,
        )

    # ---------- 관리자 ----------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        settings = self.bot.settings
        if settings.admin_role_id and hasattr(interaction.user, "roles"):
            return any(r.id == settings.admin_role_id for r in interaction.user.roles)
        return False

    @app_commands.command(
        name="지식인증심사", description="[관리자] 최종 승인 대기 중인 지식기반 인증을 봅니다."
    )
    async def review_list(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        pending = await self.bot.knowledge_service.list_pending_review()
        if not pending:
            await interaction.response.send_message(
                "승인 대기 중인 지식기반 인증이 없습니다.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🗂 지식기반 인증 승인 대기 ({len(pending)}건)",
            color=discord.Color.gold(),
        )
        for req in pending:
            hospital = await self.bot.knowledge_service.hospital_name(req.hospital_id)
            embed.add_field(
                name=f"#{req.id} · {hospital} {req.period_text}",
                value=(
                    f"질문자: <@{req.asker_id}> · 응답자: <@{req.respondent_id}>\n"
                    f"`/지식인증승인 신청번호:{req.id}`"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="지식인증승인", description="[관리자] 지식기반 인증을 최종 승인합니다."
    )
    @app_commands.describe(신청번호="승인할 지식기반 인증 번호")
    async def approve(
        self, interaction: discord.Interaction, 신청번호: int
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        req = await self.bot.knowledge_service.admin_approve(
            신청번호, interaction.user.id
        )
        if req is None:
            await interaction.followup.send(
                "유효한 승인 대기 건이 아닙니다.", ephemeral=True
            )
            return

        await self._grant_respondent(req)
        await interaction.followup.send(
            f"✅ 신청 #{신청번호} 를 승인하고 응답자에게 인증을 부여했습니다.",
            ephemeral=True,
        )

    @app_commands.command(
        name="지식인증거절", description="[관리자] 지식기반 인증을 거절합니다."
    )
    @app_commands.describe(신청번호="거절할 지식기반 인증 번호", 사유="거절 사유 (선택)")
    async def reject(
        self,
        interaction: discord.Interaction,
        신청번호: int,
        사유: str | None = None,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        req = await self.bot.knowledge_service.admin_reject(
            신청번호, interaction.user.id, 사유
        )
        if req is None:
            await interaction.response.send_message(
                "유효한 승인 대기 건이 아닙니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"❌ 신청 #{신청번호} 를 거절했습니다.", ephemeral=True
        )

    async def _grant_respondent(self, req) -> None:
        hospital_name = await self.bot.knowledge_service.hospital_name(req.hospital_id)
        for guild in self.bot.guilds:
            member = guild.get_member(req.respondent_id)
            if member is None:
                continue
            try:
                # 병원 역할은 사용자가 커뮤니티 패널에서 선택할 때만 부여한다.
                await self.bot.access.grant_tier(guild, member, 1)
            except Exception:  # noqa: BLE001
                logger.exception("지식인증 역할 부여 실패: user=%s", req.respondent_id)
            try:
                await self.bot.channel_service.ensure_hospital_channel(
                    guild, req.hospital_id, hospital_name, with_role=False
                )
            except Exception:  # noqa: BLE001
                logger.exception("병원 채널 확보 실패: hospital=%s", req.hospital_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(KnowledgeCog(bot))
