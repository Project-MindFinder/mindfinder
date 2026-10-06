"""인증 코그: 자기 신고 입원 이력 등록/조회 및 서류 심사.

- /인증      : 병원(자동완성) + 입원 연월 + 퇴원 연월을 입력받아 자기 신고 이력 저장
- /내이력    : 내가 등록한 이력 조회
- /진위확인  : 서울대병원 입퇴원 사실확인서 PDF 를 자동 진위확인 후 자동 승인
- /서류인증  : 병원을 **목록에서 선택**해 서류 인증 신청
               · 서울대병원 → 자동 진위확인
               · 그 외 병원 → 관리자 수동 심사
- /서류제출  : 기존 이력에 대한 서류 심사 요청 생성 + 비공개 채널 안내
- /심사목록|승인|거절 : 관리자 심사 (ADMIN_ROLE_ID 필요)

핵심 로직은 bot/services/verification.py 에 위임한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.services.channels import VERIFY_CHANNEL_KEY
from bot.services.verification import VERIFICATION_LABEL, VerificationService
from bot.ui.consent import ensure_consent
from bot.ui.hospitals import HospitalSelectView
from bot.utils import certificate as certificate_utils
from bot.utils import document_fingerprint as fingerprint_utils
from bot.utils.periods import PeriodError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 수동 심사용 기간 입력 모달 (병원은 목록에서 이미 선택됨)
# --------------------------------------------------------------------------- #
class ManualProofModal(discord.ui.Modal, title="서류 심사 신청"):
    """선택한 병원에 대한 입원 기간을 입력받아 자기 신고 이력을 만들고 심사를 요청한다."""

    입원연도 = discord.ui.TextInput(label="입원 연도", placeholder="예: 2020", max_length=4)
    입원월 = discord.ui.TextInput(label="입원 월 (1~12)", placeholder="예: 3", max_length=2)
    퇴원연도 = discord.ui.TextInput(
        label="퇴원 연도 (입원 중이면 비워두세요)", required=False, max_length=4
    )
    퇴원월 = discord.ui.TextInput(label="퇴원 월 (1~12)", required=False, max_length=2)

    def __init__(
        self, bot: MindFinderBot, hospital_id: int, hospital_name: str
    ) -> None:
        super().__init__()
        self.bot = bot
        self.hospital_id = hospital_id
        self.hospital_name = hospital_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            start_year = int(str(self.입원연도.value).strip())
            start_month = int(str(self.입원월.value).strip())
            end_year = (
                int(str(self.퇴원연도.value).strip())
                if self.퇴원연도.value and str(self.퇴원연도.value).strip()
                else None
            )
            end_month = (
                int(str(self.퇴원월.value).strip())
                if self.퇴원월.value and str(self.퇴원월.value).strip()
                else None
            )
        except ValueError:
            await interaction.response.send_message(
                "⚠️ 연도/월은 숫자로 입력해주세요.", ephemeral=True
            )
            return

        is_ongoing = end_year is None and end_month is None
        try:
            admission_id, period, hospital_name = (
                await self.bot.verification_service.register_admission(
                    user_id=interaction.user.id,
                    hospital_name=self.hospital_name,
                    start_year=start_year,
                    start_month=start_month,
                    end_year=end_year,
                    end_month=end_month,
                    is_ongoing=is_ongoing,
                )
            )
        except PeriodError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        request_id = await self.bot.verification_service.submit_proof(
            interaction.user.id, admission_id
        )

        # 자기 신고 등록 → 1단계(전체 커뮤니티) 역할 부여 + 병원 채널 확보
        if interaction.guild is not None:
            member = interaction.guild.get_member(interaction.user.id)
            if member is not None:
                try:
                    await self.bot.access.grant_tier(interaction.guild, member, 1)
                except Exception:  # noqa: BLE001
                    logger.exception("1단계 역할 부여 실패: user=%s", interaction.user.id)
            try:
                await self.bot.channel_service.ensure_hospital_channel(
                    interaction.guild, self.hospital_id, hospital_name
                )
            except Exception:  # noqa: BLE001
                logger.exception("병원 채널 확보 실패: hospital=%s", self.hospital_id)

        settings = self.bot.settings
        channel_hint = (
            f"<#{settings.verification_channel_id}>"
            if settings.verification_channel_id
            else "관리자가 지정한 심사 채널"
        )
        embed = discord.Embed(
            title="📄 서류 심사가 접수되었습니다",
            description=(
                f"**병원**: {hospital_name}\n"
                f"**기간**: {period.text()}\n"
                f"**심사번호**: `{request_id}`\n\n"
                f"{channel_hint} 에 `심사번호: {request_id}` 와 함께 "
                "입퇴원 증빙 서류(이미지/PDF)를 첨부해주세요.\n"
                "관리자가 확인 후 수동으로 인증해드립니다."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(
            text="서류 원본은 저장하지 않으며, 심사 완료 후 삭제됩니다 (지문만 보관)."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class VerificationCog(commands.Cog):
    """입원 이력 인증 관련 명령."""

    # 본문에서 `심사번호: 12` / `심사 번호 12` 형태로 심사 번호를 찾는다.
    _REQUEST_ID_RE = re.compile(r"심사\s*번호\s*[:：]?\s*(\d+)")

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot
        self.service = VerificationService(bot)

    # ---------- 심사 채널 서류 지문 수집 (원본 미보관) ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """심사 채널에 올라온 증빙 서류의 '지문'만 수집한다.

        개인정보 보호를 위해 서류 원본(PDF)은 영구 보관하지 않는다. 대신
        정규화 텍스트 해시(지문)와 유사도 토큰만 DB 에 저장해, 동일/유사 서류의
        중복 제출을 탐지할 수 있게 한다.
        """
        if message.author.bot or message.guild is None:
            return
        verification_channel_id = await self._resolve_verification_channel_id(
            message.guild
        )
        if verification_channel_id is None:
            return
        if message.channel.id != verification_channel_id:
            return
        if not message.attachments:
            return

        request_id = self._parse_request_id(message.content)
        if request_id is None:
            return
        request = await self.bot.verifications.get_by_id(request_id)
        if request is None or request.user_id != message.author.id:
            return

        attachment = next(
            (
                a
                for a in message.attachments
                if a.filename.lower().endswith(".pdf")
            ),
            message.attachments[0],
        )
        try:
            data = await attachment.read()
        except discord.HTTPException:
            logger.warning("심사 서류 다운로드 실패: %s", attachment.filename)
            return

        fingerprint = await asyncio.to_thread(self._fingerprint_from_bytes, data)
        await self.bot.verifications.set_fingerprint(
            request_id,
            fingerprint.fingerprint,
            fingerprint_utils.encode_shingles(fingerprint.shingles),
        )
        await self.bot.verifications.set_proof_message(
            request_id, message.channel.id, message.id
        )

    async def _resolve_verification_channel_id(
        self, guild: discord.Guild
    ) -> Optional[int]:
        """증빙 서류를 받을 심사 채널 ID를 반환한다.

        설정(VERIFICATION_CHANNEL_ID)이 있으면 우선하고, 없으면 시작 시
        [`ChannelService.ensure_all()`] 가 자동 구성한 `🔐-인증` 채널을 사용한다.
        """
        if self.bot.settings.verification_channel_id:
            return self.bot.settings.verification_channel_id
        record = await self.bot.managed_channels.get(guild.id, VERIFY_CHANNEL_KEY)
        return record.discord_id if record is not None else None

    @classmethod
    def _parse_request_id(cls, content: str) -> Optional[int]:
        """메시지 본문에서 심사 번호를 파싱한다."""
        if not content:
            return None
        match = cls._REQUEST_ID_RE.search(content)
        if match is None:
            return None
        return int(match.group(1))

    @staticmethod
    def _fingerprint_from_bytes(data: bytes) -> "fingerprint_utils.DocumentFingerprint":
        """증빙 서류 바이트에서 지문을 계산한다 (원본 미보관)."""
        try:
            text = certificate_utils.extract_text(data)
        except Exception:  # noqa: BLE001 - 스캔 이미지/비PDF 등은 텍스트 추출 불가
            text = ""
        if not text:
            # 텍스트 추출 실패 시 원본 바이트 해시로 '완전 동일' 여부만 판정한다.
            return fingerprint_utils.DocumentFingerprint(
                fingerprint=hashlib.sha256(data).hexdigest(),
                shingles=frozenset(),
            )
        return fingerprint_utils.fingerprint_text(text)

    # ---------- 자기 신고 ----------

    @app_commands.command(
        name="인증", description="입원 이력을 등록하거나 서류 심사를 요청합니다."
    )
    @app_commands.describe(
        병원="입원했던 병원 이름 (목록에서 선택하거나 입력)",
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
        async def _proceed(inner: discord.Interaction) -> None:
            await self._do_certify(
                inner, 병원, 입원연도, 입원월, 퇴원연도, 퇴원월, 현재입원중
            )

        # 인증 전 약관/개인정보 필수 동의
        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _do_certify(
        self,
        interaction: discord.Interaction,
        병원: str,
        입원연도: int,
        입원월: int,
        퇴원연도: Optional[int],
        퇴원월: Optional[int],
        현재입원중: bool,
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

        # 자기 신고 등록 → 1단계(전체 커뮤니티) 역할 부여 + 병원 채널 확보
        if interaction.guild is not None:
            member = interaction.guild.get_member(interaction.user.id)
            if member is not None:
                try:
                    await self.bot.access.grant_tier(interaction.guild, member, 1)
                except Exception:  # noqa: BLE001
                    logger.exception("1단계 역할 부여 실패: user=%s", interaction.user.id)
            hospital = await self.bot.hospitals.get_by_name(hospital_name)
            if hospital is not None:
                try:
                    await self.bot.channel_service.ensure_hospital_channel(
                        interaction.guild, hospital.id, hospital_name
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("병원 채널 확보 실패: hospital=%s", hospital.id)

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
            text="서류 인증을 마치면 병원 커뮤니티·기간·매칭 채널을 이용할 수 있습니다."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @certify.autocomplete("병원")
    async def _certify_hospital_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        hospitals = await self.bot.hospitals.list_all()
        current_lower = current.lower()
        return [
            app_commands.Choice(name=h.name, value=h.name)
            for h in hospitals
            if current_lower in h.name.lower()
        ][:25]

    @app_commands.command(name="내이력", description="내가 등록한 입원 이력을 조회합니다.")
    async def my_history(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.admissions.list_by_user(interaction.user.id)
        if not rows:
            await interaction.response.send_message(
                "아직 등록된 입원 이력이 없습니다. `/인증` 으로 등록해주세요.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title="📋 내 입원 이력", color=discord.Color.blue())
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
        async def _proceed(inner: discord.Interaction) -> None:
            await self._do_verify_truth(inner, 서류)

        # 인증 전 약관/개인정보 필수 동의
        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _do_verify_truth(
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
            await self._grant_verified(interaction, interaction.user.id, result)

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

    # ---------- 병원 선택형 서류 인증 ----------

    @app_commands.command(
        name="서류인증",
        description="병원을 목록에서 선택해 서류 인증을 신청합니다. (서울대병원=자동, 그 외=수동)",
    )
    async def document_verify(self, interaction: discord.Interaction) -> None:
        async def _proceed(inner: discord.Interaction) -> None:
            await inner.response.defer(ephemeral=True, thinking=True)
            view = HospitalSelectView(
                self.bot,
                self._on_hospital_selected,
                placeholder="서류를 제출할 병원을 선택하세요",
            )
            await view.load()
            await inner.followup.send(
                "**병원을 선택**하세요.\n"
                "· 서울대학교병원 → 사실확인서 PDF 자동 진위확인\n"
                "· 그 외 병원 → 관리자 수동 심사\n"
                "목록에 없으면 **➕ 병원 추가** 버튼을 눌러주세요.",
                view=view,
                ephemeral=True,
            )

        # 인증 전 약관/개인정보 필수 동의
        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _on_hospital_selected(
        self, interaction: discord.Interaction, hospital_id: int
    ) -> None:
        hospital = await self.bot.hospitals.get_by_id(hospital_id)
        if hospital is None:
            await interaction.response.send_message(
                "병원 정보를 찾을 수 없습니다.", ephemeral=True
            )
            return

        if self.service.supports_auto_verify(hospital.name):
            # 서울대병원: 자동 진위확인
            await interaction.response.send_message(
                f"🏥 **{hospital.name}** 은(는) 자동 진위확인을 지원합니다.\n"
                "`/진위확인` 명령 또는 패널 채널에 **입퇴원 사실확인서 PDF** 를 올려주세요.\n"
                "자동으로 진위·정신과 여부를 확인해 인증해드립니다.",
                ephemeral=True,
            )
            return

        # 그 외 병원: 관리자 수동 심사 (기간 입력 → 심사 접수)
        await interaction.response.send_modal(
            ManualProofModal(self.bot, hospital.id, hospital.name)
        )

    # ---------- 서류 심사 ----------

    @app_commands.command(
        name="서류제출", description="입원 증빙 서류를 제출하여 인증 배지를 신청합니다."
    )
    @app_commands.describe(이력번호="서류로 증명할 입원 이력 번호 (/내이력 에서 확인)")
    async def submit_proof(
        self, interaction: discord.Interaction, 이력번호: int
    ) -> None:
        async def _proceed(inner: discord.Interaction) -> None:
            await self._do_submit_proof(inner, 이력번호)

        # 인증 전 약관/개인정보 필수 동의
        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _do_submit_proof(
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
                "⚠️ 서류 원본은 저장하지 않고 심사 완료 후 삭제됩니다. "
                "중복 제출 방지를 위해 개인정보를 제거한 '지문'만 보관합니다."
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

        await self._grant_verified(interaction, req.user_id)
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

    # ---------- 역할 부여 ----------

    async def _grant_verified(
        self,
        interaction: discord.Interaction,
        user_id: int,
        result=None,
    ) -> None:
        """2단계(서류 인증) 역할 + 해당 병원 역할을 부여한다."""
        guild = interaction.guild
        if guild is None:
            return
        member = guild.get_member(user_id)
        if member is None:
            return
        try:
            await self.bot.access.grant_tier(guild, member, 2)
        except Exception:  # noqa: BLE001
            logger.exception("2단계 역할 부여 실패: user=%s", user_id)

        # 병원 역할 + 병원 채널 확보
        hospital_name = getattr(result, "hospital_name", None)
        if hospital_name:
            hospital = await self.bot.hospitals.get_by_name(hospital_name)
            if hospital is None:
                hospital = await self.service.resolve_hospital(hospital_name)
            try:
                await self.bot.access.grant_hospital_role(
                    guild, member, hospital.id, hospital.name
                )
                await self.bot.channel_service.ensure_hospital_channel(
                    guild, hospital.id, hospital.name
                )
                # 입원 기간(월)에 맞는 비공개 기간 스레드에 초대한다.
                await self.bot.channel_service.ensure_community_access(
                    guild, user_id
                )
            except Exception:  # noqa: BLE001
                logger.exception("병원 역할/채널 처리 실패: hospital=%s", hospital.id)

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
