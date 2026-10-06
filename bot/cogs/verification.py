"""인증 코그: 자기 신고 입원 이력 등록/조회 및 서류 심사.

- /인증      : 병원 + 입원 연월 + 퇴원 연월을 입력받아 자기 신고 이력 저장
- /내이력    : 내가 등록한 이력 조회
- /진위확인  : 서울대병원 입퇴원 사실확인서 PDF 를 자동 진위확인 후 자동 승인
- /서류제출  : 서류 심사 요청 생성 + 비공개 채널 안내
- /심사목록|승인|거절 : 관리자 심사 (ADMIN_ROLE_ID 필요)

핵심 로직은 bot/services/verification.py 에 위임한다.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.services.verification import VERIFICATION_LABEL, VerificationService
from bot.utils.periods import PeriodError

logger = logging.getLogger(__name__)


class VerificationCog(commands.Cog):
    """입원 이력 인증 관련 명령."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot
        self.service = VerificationService(bot)

    # ---------- 자기 신고 ----------

    @app_commands.command(
        name="인증", description="입원 이력을 등록하거나 서류 심사를 요청합니다."
    )
    @app_commands.describe(
        병원="입원했던 병원 이름 (예: 국립정신건강센터)",
        입원연도="입원한 연도 (예: 2020)",
        입원월="입원한 월 (1~12)",
        퇴원연도="퇴원한 연도 (현재 입원 중이면 비워두세요)",
        퇴원월="퇴원한 월 (1~12)",
        현재입원중="현재 입원 중이라면 True",
    )
    async def certify(
        self,
        interaction: discord.Interaction,
        병원: str,
        입원연도: int,
        입원월: int,
        퇴원연도: Optional[int] = None,
        퇴원월: Optional[int] = None,
        현재입원중: bool = False,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            _admission_id, period, hospital_name = (
                await self.service.register_admission(
                    user_id=interaction.user.id,
                    hospital_name=병원,
                    start_year=입원연도,
                    start_month=입원월,
                    end_year=퇴원연도,
                    end_month=퇴원월,
                    is_ongoing=현재입원중,
                )
            )
        except PeriodError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        embed = discord.Embed(
            title="✅ 입원 이력이 등록되었습니다",
            description=(
                f"**병원**: {hospital_name}\n"
                f"**기간**: {period.text()}\n"
                f"**상태**: {VERIFICATION_LABEL['self_reported']}"
            ),
            color=discord.Color.yellow(),
        )
        embed.set_footer(
            text="서류 심사를 통과하면 인증 배지(🟢)를 받을 수 있습니다."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="내이력", description="내가 등록한 입원 이력을 조회합니다.")
    async def my_history(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.admissions.list_by_user(interaction.user.id)
        if not rows:
            await interaction.response.send_message(
                "아직 등록된 입원 이력이 없습니다. `/인증` 으로 등록해주세요.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📋 내 입원 이력", color=discord.Color.blue()
        )
        for adm in rows:
            embed.add_field(
                name=f"#{adm.id} · {adm.hospital.name}",
                value=(
                    f"{adm.period_text()}\n"
                    f"상태: {VERIFICATION_LABEL.get(adm.verification, adm.verification)}"
                ),
                inline=False,
            )
        embed.set_footer(text="이력 삭제가 필요하면 관리자에게 문의하세요.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- 자동 진위확인 (서울대병원) ----------

    @app_commands.command(
        name="진위확인",
        description="서울대병원 입퇴원 사실확인서 PDF 를 자동 진위확인하여 인증받습니다.",
    )
    @app_commands.describe(서류="서울대병원에서 발급한 입퇴원 사실확인서 PDF 파일")
    async def verify_truth(
        self, interaction: discord.Interaction, 서류: discord.Attachment
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not 서류.filename.lower().endswith(".pdf"):
            await interaction.followup.send(
                "⚠️ PDF 파일만 제출할 수 있습니다.", ephemeral=True
            )
            return

        pdf_bytes = await 서류.read()
        result = await self.service.auto_verify_pdf(
            user_id=interaction.user.id,
            filename=서류.filename,
            pdf_bytes=pdf_bytes,
        )

        if result.approved:
            await self._grant_verified_role(interaction, interaction.user.id)

        color = {
            "green": discord.Color.green(),
            "red": discord.Color.red(),
            "orange": discord.Color.orange(),
        }.get(result.color, discord.Color.orange())
        embed = discord.Embed(
            title=("✅ " if result.approved else "⚠️ ") + result.title,
            description=result.detail,
            color=color,
        )
        if result.approved:
            embed.set_footer(text="정신건강의학과 입원 이력이 인증되었습니다.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- 서류 심사 ----------

    @app_commands.command(
        name="서류제출", description="입원 증빙 서류를 제출하여 인증 배지를 신청합니다."
    )
    @app_commands.describe(이력번호="서류로 증명할 입원 이력 번호 (/내이력 에서 확인)")
    async def submit_proof(
        self, interaction: discord.Interaction, 이력번호: int
    ) -> None:
        settings = self.bot.settings
        if not settings.verification_channel_id:
            await interaction.response.send_message(
                "⚠️ 서류 심사 채널이 아직 설정되지 않았습니다. 관리자에게 문의해주세요.",
                ephemeral=True,
            )
            return

        admission = await self.bot.admissions.get_by_id(이력번호)
        if admission is None or admission.user_id != interaction.user.id:
            await interaction.response.send_message(
                "⚠️ 본인의 입원 이력 번호를 입력해주세요. (`/내이력` 에서 확인)",
                ephemeral=True,
            )
            return

        request_id = await self.service.submit_proof(
            interaction.user.id, admission.id
        )

        embed = discord.Embed(
            title="📄 서류 심사 안내",
            description=(
                f"입력하신 이력(# {admission.id}, {admission.hospital.name} "
                f"{admission.period_text()})에 대한 증빙 서류를 제출해주세요.\n\n"
                f"**제출 채널**: <#{settings.verification_channel_id}>\n"
                f"`심사번호: {request_id}` 와 함께 서류 이미지/파일 첨부\n\n"
                "⚠️ 서류는 심사 완료 후 삭제되며, 관리자만 열람합니다."
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- 관리자 심사 ----------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        """관리자 권한 여부를 확인한다."""
        settings = self.bot.settings
        if interaction.user.guild_permissions.administrator:
            return True
        if settings.admin_role_id:
            return any(
                role.id == settings.admin_role_id for role in interaction.user.roles
            )
        return False

    @app_commands.command(name="심사목록", description="[관리자] 대기 중인 심사 목록을 봅니다.")
    async def review_list(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 사용할 수 있는 명령입니다.", ephemeral=True
            )
            return

        pending = await self.bot.verifications.list_pending()
        if not pending:
            await interaction.response.send_message(
                "대기 중인 심사가 없습니다.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🗂 대기 중인 심사 ({len(pending)}건)",
            color=discord.Color.orange(),
        )
        for req in pending:
            admission = (
                await self.bot.admissions.get_by_id(req.admission_id)
                if req.admission_id
                else None
            )
            detail = (
                f"{admission.hospital.name} {admission.period_text()}"
                if admission
                else "이력 없음"
            )
            embed.add_field(
                name=f"심사 #{req.id} · <@{req.user_id}>",
                value=f"{detail}\n`/심사승인 심사번호:{req.id}`",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="심사승인", description="[관리자] 심사를 승인하고 인증 배지를 부여합니다.")
    @app_commands.describe(심사번호="승인할 심사 번호")
    async def review_approve(
        self, interaction: discord.Interaction, 심사번호: int
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 사용할 수 있는 명령입니다.", ephemeral=True
            )
            return

        req = await self.service.approve(심사번호, interaction.user.id)
        if req is None:
            await interaction.response.send_message(
                "유효한 대기 중 심사가 아닙니다.", ephemeral=True
            )
            return

        await self._grant_verified_role(interaction, req.user_id)
        await self._delete_proof(req)
        await interaction.response.send_message(
            f"✅ 심사 #{심사번호} 를 승인했습니다.", ephemeral=True
        )

    @app_commands.command(name="심사거절", description="[관리자] 심사를 거절합니다.")
    @app_commands.describe(심사번호="거절할 심사 번호", 사유="거절 사유 (선택)")
    async def review_reject(
        self,
        interaction: discord.Interaction,
        심사번호: int,
        사유: Optional[str] = None,
    ) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 사용할 수 있는 명령입니다.", ephemeral=True
            )
            return

        req = await self.service.reject(심사번호, interaction.user.id, 사유)
        if req is None:
            await interaction.response.send_message(
                "유효한 대기 중 심사가 아닙니다.", ephemeral=True
            )
            return

        await self._delete_proof(req)
        await interaction.response.send_message(
            f"❌ 심사 #{심사번호} 를 거절했습니다.", ephemeral=True
        )

    async def _grant_verified_role(
        self, interaction: discord.Interaction, user_id: int
    ) -> None:
        """인증 역할을 부여한다."""
        role_id = self.bot.settings.verified_role_id
        if not role_id or not interaction.guild:
            return
        member = interaction.guild.get_member(user_id)
        role = interaction.guild.get_role(role_id)
        if member and role:
            try:
                await member.add_roles(role, reason="인증 배지 부여")
            except discord.Forbidden:
                logger.warning("인증 역할 부여 권한 없음: user=%s", user_id)

    async def _delete_proof(self, req) -> None:
        """심사 완료 후 증빙 메시지를 삭제한다 (개인정보 보호)."""
        if not req.proof_channel_id or not req.proof_message_id:
            return
        channel = self.bot.get_channel(req.proof_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                message = await channel.fetch_message(req.proof_message_id)
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                logger.warning("증빙 메시지 삭제 실패: req=%s", req.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VerificationCog(bot))
