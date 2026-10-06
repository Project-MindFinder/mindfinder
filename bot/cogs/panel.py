"""상시 패널 코그: 용도별 채널에 고정 패널을 게시하고 버튼/모달로 기능을 제공한다.

슬래시 커맨드 대신, **기능별 전용 채널**에 항상 떠 있는 안내 패널을 게시하고
버튼을 눌러 기능을 사용하는 방식을 제공한다.
(슬래시 커맨드도 그대로 유지되어 둘 다 사용할 수 있다.)

- 채널이 없으면 봇이 자동으로 만든다(ChannelService.ensure_all).
- 패널 메시지는 재시작해도 유지되도록 영구 뷰(persistent view)를 사용한다.
- 서울대병원 사실확인서 PDF 는 패널 버튼 → **창(모달) 업로드** 로 제출한다.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.services.channels import (
    ADMIN_CHANNEL_KEY,
    ADMIN_CHANNEL_NAME,
    COMMUNITY_CHANNEL_KEY,
    COMMUNITY_CHANNEL_NAME,
    MATCH_CHANNEL_KEY,
    MATCH_CHANNEL_NAME,
    PANEL_CHANNEL_KEY,
    PANEL_CHANNEL_NAME as MANAGED_PANEL_CHANNEL_NAME,
    TERMS_CHANNEL_KEY,
    TERMS_CHANNEL_NAME,
    VERIFY_CHANNEL_KEY,
    VERIFY_CHANNEL_NAME,
)
from bot.services.matching import Candidate, MatchingService
from bot.services.verification import VERIFICATION_LABEL, VerificationService
from bot.ui.consent import build_consent_embed, ensure_consent
from bot.ui.knowledge import AnswerRequestView, VerdictView
from bot.utils.hospitals import hospital_display_name, normalize_name
from bot.utils.periods import PeriodError, format_ym, parse_period_input

logger = logging.getLogger(__name__)

# 패널 메시지를 식별하는 푸터 마커 (중복 게시 방지)
PANEL_MARKER = "MindFinder 안내 패널"
# 자동 생성할 패널 채널 이름 (레거시 단일 패널용)
PANEL_CHANNEL_NAME = "mindfinder-패널"
# 후보/신청 목록에 한 번에 표시할 최대 항목 수
MAX_ACTIONS = 5


# --------------------------------------------------------------------------- #
# 모달: 자기 신고 입원 이력 등록
# --------------------------------------------------------------------------- #
class AdmissionModal(discord.ui.Modal, title="입원 이력 등록"):
    """병원 + 입퇴원 연·월을 입력받아 자기 신고 이력을 등록한다."""

    병원 = discord.ui.TextInput(
        label="병원 이름",
        placeholder="예: 국립정신건강센터",
        max_length=50,
    )
    입원연도 = discord.ui.TextInput(label="입원 연도", placeholder="예: 2020", max_length=4)
    입원월 = discord.ui.TextInput(label="입원 월 (1~12)", placeholder="예: 3", max_length=2)
    퇴원연도 = discord.ui.TextInput(
        label="퇴원 연도 (입원 중이면 비워두세요)", required=False, max_length=4
    )
    퇴원월 = discord.ui.TextInput(
        label="퇴원 월 (1~12)", required=False, max_length=2
    )

    def __init__(self, bot: MindFinderBot, service: VerificationService) -> None:
        super().__init__()
        self.bot = bot
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async def _proceed(inner: discord.Interaction) -> None:
            await self._register(inner)

        # 인증 전 약관/개인정보 필수 동의
        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _register(self, interaction: discord.Interaction) -> None:
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
            _id, period, hospital_name = await self.service.register_admission(
                user_id=interaction.user.id,
                hospital_name=str(self.병원.value),
                start_year=start_year,
                start_month=start_month,
                end_year=end_year,
                end_month=end_month,
                is_ongoing=is_ongoing,
            )
        except PeriodError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        # 자기 신고 등록 → 1단계 역할 부여 (전체 커뮤니티 열람)
        if interaction.guild is not None:
            member = interaction.guild.get_member(interaction.user.id)
            if member is not None:
                try:
                    await self.bot.access.grant_tier(interaction.guild, member, 1)
                except Exception:  # noqa: BLE001
                    logger.exception("1단계 역할 부여 실패: user=%s", interaction.user.id)

        embed = discord.Embed(
            title="✅ 입원 이력이 등록되었습니다",
            description=(
                f"**병원**: {hospital_name}\n"
                f"**기간**: {period.text()}\n"
                f"**상태**: {VERIFICATION_LABEL['self_reported']}"
            ),
            color=discord.Color.yellow(),
        )
        embed.set_footer(text="서류 인증을 받으면 병원·기간 커뮤니티와 매칭을 이용할 수 있습니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# 모달: 통합 서류 인증 (병원 선택 + 기간 입력 + PDF 업로드)
# --------------------------------------------------------------------------- #
class UnifiedVerifyModal(discord.ui.Modal, title="서류 인증"):
    """병원을 고르고(창 안 드롭다운) 기간을 입력한 뒤, 서울대병원은 PDF 로 인증한다.

    · 서울대학교병원(62병동)·서울대학교어린이병원(7A병동)
      → 입퇴원 사실확인서 PDF 자동 진위확인 (PDF 필수)
    · 그 외 병원 → 기간을 입력해 관리자 수동 심사 요청

    디스코드 모달은 드롭다운(Select)과 파일 업로드(FileUpload)를 함께 담을 수 있다.
    """

    기간 = discord.ui.TextInput(
        label="입원 기간 (예: 2020-03 ~ 2020-06)",
        placeholder="2020-03 ~ 2020-06 · 입원 중이면 시작만 입력",
        required=False,
        max_length=40,
    )

    def __init__(self, bot: MindFinderBot, service: VerificationService) -> None:
        super().__init__()
        self.bot = bot
        self.service = service
        # 병원 드롭다운 (load() 에서 실제 목록으로 채운다)
        self.선택병원 = discord.ui.Select(
            placeholder="병원을 선택하세요",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="불러오는 중…", value="0")],
            custom_id="mf:verify:hospital",
        )
        self.add_item(self.선택병원)
        # PDF 업로드 (서울대병원은 필수, 그 외는 선택)
        self.서류 = discord.ui.FileUpload(
            custom_id="mf:verify:pdf", required=False, max_values=1
        )
        self.add_item(
            discord.ui.Label(
                text="입퇴원 사실확인서 PDF (서울대병원만)",
                description="서울대병원은 PDF 필수 · 그 외 병원은 비워두세요.",
                component=self.서류,
            )
        )

    async def load(self) -> "UnifiedVerifyModal":
        """드롭다운 옵션을 등록된 병원 목록으로 채운다."""
        hospitals = list(await self.bot.hospitals.list_all())
        options = [
            discord.SelectOption(
                label=hospital_display_name(h.name)[:100], value=str(h.id)
            )
            for h in hospitals[:25]
        ]
        if not options:
            options = [discord.SelectOption(label="등록된 병원이 없습니다", value="0")]
        self.선택병원.options = options
        return self

    async def on_submit(self, interaction: discord.Interaction) -> None:
        hospital_id = int(self.선택병원.values[0]) if self.선택병원.values else 0
        hospital = (
            await self.bot.hospitals.get_by_id(hospital_id) if hospital_id else None
        )
        if hospital is None:
            await interaction.response.send_message(
                "⚠️ 병원을 선택해주세요.", ephemeral=True
            )
            return

        attachments = list(getattr(self.서류, "values", []) or [])
        attachment = attachments[0] if attachments else None
        if attachment is not None and not attachment.filename.lower().endswith(".pdf"):
            await interaction.response.send_message(
                "⚠️ PDF 파일만 제출할 수 있습니다.", ephemeral=True
            )
            return

        if self.service.supports_auto_verify(hospital.name):
            await self._submit_auto(interaction, hospital, attachment)
            return
        await self._submit_manual(interaction, hospital)

    async def _submit_auto(
        self,
        interaction: discord.Interaction,
        hospital,
        attachment,
    ) -> None:
        """서울대병원 계열: 사실확인서 PDF 자동 진위확인."""
        if attachment is None:
            await interaction.response.send_message(
                f"⚠️ **{hospital_display_name(hospital.name)}** 은(는) "
                "입퇴원 사실확인서 **PDF** 가 필요합니다.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            pdf_bytes = await attachment.read()
        except discord.HTTPException:
            await interaction.followup.send(
                "⚠️ 파일을 읽지 못했습니다. 다시 시도해주세요.", ephemeral=True
            )
            return

        result = await self.service.auto_verify_pdf(
            user_id=interaction.user.id,
            filename=attachment.filename,
            pdf_bytes=pdf_bytes,
        )
        if result.approved and interaction.guild is not None:
            await grant_verified_access(
                self.bot, interaction.guild, interaction.user.id, result
            )

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

    async def _submit_manual(self, interaction: discord.Interaction, hospital) -> None:
        """그 외 병원: 기간을 입력받아 자기 신고 등록 후 관리자 수동 심사 요청."""
        try:
            start_year, start_month, end_year, end_month = parse_period_input(
                str(self.기간.value or "")
            )
        except PeriodError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        is_ongoing = end_year is None and end_month is None
        try:
            admission_id, period, hospital_name = await self.service.register_admission(
                user_id=interaction.user.id,
                hospital_name=hospital.name,
                start_year=start_year,
                start_month=start_month,
                end_year=end_year,
                end_month=end_month,
                is_ongoing=is_ongoing,
            )
        except PeriodError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        request_id = await self.service.submit_proof(interaction.user.id, admission_id)

        # 자기 신고 등록 → 1단계 역할 + 병원 채널 확보
        if interaction.guild is not None:
            member = interaction.guild.get_member(interaction.user.id)
            if member is not None:
                try:
                    await self.bot.access.grant_tier(interaction.guild, member, 1)
                except Exception:  # noqa: BLE001
                    logger.exception("1단계 역할 부여 실패: user=%s", interaction.user.id)
            try:
                await self.bot.channel_service.ensure_hospital_channel(
                    interaction.guild, hospital.id, hospital_name
                )
            except Exception:  # noqa: BLE001
                logger.exception("병원 채널 확보 실패: hospital=%s", hospital.id)

        settings = self.bot.settings
        channel_hint = (
            f"<#{settings.verification_channel_id}>"
            if settings.verification_channel_id
            else "관리자가 지정한 심사 채널"
        )
        embed = discord.Embed(
            title="📄 서류 심사가 접수되었습니다",
            description=(
                f"**병원**: {hospital_display_name(hospital_name)}\n"
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


# --------------------------------------------------------------------------- #
# 모달: 기타 방법 인증 (서류·지식 외 · 관리자 수동 승인)
# --------------------------------------------------------------------------- #
class OtherVerifyModal(discord.ui.Modal, title="기타 방법 인증 신청"):
    """서류·지식기반 외의 방법으로 인증을 신청한다 (관리자 수동 승인).

    예: 진료기록 사본, 병원 발급 서류의 사진, 보호자 확인 등. 서류 원본을
    봇에 보관하지 않기 위해, 사유(경위)만 적어 제출하고 실제 확인은 관리자가
    대화로 진행한다.
    """

    병원 = discord.ui.TextInput(
        label="병원명",
        placeholder="예: 국립정신건강센터",
        required=True,
        max_length=100,
    )
    기간 = discord.ui.TextInput(
        label="입원 기간 (예: 2020-03 ~ 2020-06)",
        placeholder="2020-03 ~ 2020-06 · 입원 중이면 시작만 입력",
        required=True,
        max_length=40,
    )
    사유 = discord.ui.TextInput(
        label="인증이 필요한 이유 · 확인 방법",
        placeholder="서류를 낼 수 없는 사정과, 어떻게 확인받을 수 있는지 적어주세요.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(self, bot: MindFinderBot, service: VerificationService) -> None:
        super().__init__()
        self.bot = bot
        self.service = service

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            start_year, start_month, end_year, end_month = parse_period_input(
                str(self.기간.value or "")
            )
        except PeriodError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        is_ongoing = end_year is None and end_month is None
        try:
            admission_id, period, hospital_name = await self.service.register_admission(
                user_id=interaction.user.id,
                hospital_name=str(self.병원.value or ""),
                start_year=start_year,
                start_month=start_month,
                end_year=end_year,
                end_month=end_month,
                is_ongoing=is_ongoing,
            )
        except PeriodError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return

        reason = str(self.사유.value or "").strip()
        request_id = await self.service.submit_other_proof(
            interaction.user.id, admission_id
        )

        # 자기 신고 등록 → 1단계 역할 + 병원 채널 확보
        if interaction.guild is not None:
            member = interaction.guild.get_member(interaction.user.id)
            if member is not None:
                try:
                    await self.bot.access.grant_tier(interaction.guild, member, 1)
                except Exception:  # noqa: BLE001
                    logger.exception("1단계 역할 부여 실패: user=%s", interaction.user.id)
            try:
                hospital = await self.bot.hospitals.get_by_name(hospital_name)
                if hospital is not None:
                    await self.bot.channel_service.ensure_hospital_channel(
                        interaction.guild, hospital.id, hospital.name
                    )
            except Exception:  # noqa: BLE001
                logger.exception("병원 채널 확보 실패: hospital=%s", hospital_name)

        await notify_admins_of_other_request(
            self.bot,
            request_id=request_id,
            user_id=interaction.user.id,
            hospital_name=hospital_name,
            period_text=period.text(),
            reason=reason,
        )

        embed = discord.Embed(
            title="🟠 기타 방법 인증이 접수되었습니다",
            description=(
                f"**병원**: {hospital_display_name(hospital_name)}\n"
                f"**기간**: {period.text()}\n"
                f"**심사번호**: `{request_id}`\n\n"
                "관리자가 확인 후 승인 여부를 알려드립니다.\n"
                "추가 확인이 필요하면 관리자가 DM 으로 연락드릴 수 있습니다."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="서류·지식기반 인증이 어려울 때 사용하는 대체 경로입니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def notify_admins_of_other_request(
    bot: MindFinderBot,
    *,
    request_id: int,
    user_id: int,
    hospital_name: str,
    period_text: str,
    reason: str,
) -> None:
    """관리자 패널 채널에 기타 방법 인증 신청을 게시한다."""
    for guild in bot.guilds:
        channel = await bot.channel_service.managed_text_channel(
            guild, ADMIN_CHANNEL_KEY
        )
        if channel is None:
            continue
        view = OtherVerifyReviewView(bot, request_id)
        try:
            await channel.send(
                f"🟠 **기타 방법 인증 신청** (`{request_id}`)\n"
                f"· 신청자: <@{user_id}>\n"
                f"· 병원: **{hospital_display_name(hospital_name)}**\n"
                f"· 기간: {period_text}\n"
                f"· 사유: {reason[:300]}\n"
                "확인 후 승인하면 2단계 인증이 부여됩니다.",
                view=view,
            )
        except discord.HTTPException:
            logger.exception("기타 인증 알림 실패: request=%s", request_id)
        return


# --------------------------------------------------------------------------- #
# 모달: 기타 병원 직접 입력 (관리자 승인 요청)
# --------------------------------------------------------------------------- #
class HospitalRequestModal(discord.ui.Modal, title="기타 병원 추가 요청"):
    """목록에 없는 병원을 직접 입력해 등록을 요청한다 (관리자 승인 후 추가)."""

    병원 = discord.ui.TextInput(
        label="병원 이름", placeholder="예: 국립정신건강센터", max_length=100
    )
    병동 = discord.ui.TextInput(
        label="병동 (선택)", placeholder="예: 62병동", required=False, max_length=50
    )
    사유 = discord.ui.TextInput(
        label="추가 사유 (선택)",
        placeholder="이 병원에서 입원한 경험이 있습니다.",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=300,
    )

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.병원.value or "").strip()
        if len(name) < 2:
            await interaction.response.send_message(
                "⚠️ 병원 이름을 2자 이상 입력해주세요.", ephemeral=True
            )
            return
        # 이미 등록된 병원이면 바로 안내한다.
        existing = await self.bot.hospitals.get_by_name(name)
        if existing is None:
            existing = await self.bot.hospitals.get_by_normalized(
                normalize_name(name)
            )
        if existing is not None:
            await interaction.response.send_message(
                f"ℹ️ 이미 등록된 병원입니다: **{hospital_display_name(existing.name)}**\n"
                "‘서류 인증 (2단계)’ 버튼에서 선택해 진행해주세요.",
                ephemeral=True,
            )
            return

        request_id = await self.bot.hospital_requests.create(
            user_id=interaction.user.id,
            name=name,
            ward=str(self.병동.value or "").strip() or None,
            reason=str(self.사유.value or "").strip() or None,
        )
        await notify_admins_of_hospital_request(
            self.bot, request_id, interaction.user.id, name
        )
        await interaction.response.send_message(
            f"✅ 병원 추가 요청이 접수되었습니다 (요청번호 `{request_id}`).\n"
            "관리자 승인 후 목록에 추가되면 서류 인증을 진행할 수 있습니다.",
            ephemeral=True,
        )


async def notify_admins_of_hospital_request(
    bot: MindFinderBot, request_id: int, user_id: int, name: str
) -> None:
    """관리자 패널 채널에 병원 추가 요청을 게시한다 (없으면 DM/로그)."""
    for guild in bot.guilds:
        channel = await bot.channel_service.managed_text_channel(
            guild, ADMIN_CHANNEL_KEY
        )
        if channel is None:
            continue
        view = HospitalRequestReviewView(bot, request_id)
        try:
            await channel.send(
                f"🏥 **병원 추가 요청** (`{request_id}`)\n"
                f"· 요청자: <@{user_id}>\n"
                f"· 병원명: **{name}**\n"
                "승인하면 병원 목록에 추가됩니다.",
                view=view,
            )
        except discord.HTTPException:
            logger.exception("병원 추가 요청 알림 실패: request=%s", request_id)
        return


class HospitalRequestReviewView(discord.ui.View):
    """관리자가 병원 추가 요청을 승인/거절하는 영구 뷰."""

    def __init__(self, bot: MindFinderBot, request_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id
        approve = discord.ui.Button(
            label="승인", style=discord.ButtonStyle.success,
            custom_id=f"mf:hreq:approve:{request_id}",
        )
        reject = discord.ui.Button(
            label="거절", style=discord.ButtonStyle.danger,
            custom_id=f"mf:hreq:reject:{request_id}",
        )
        approve.callback = self._approve
        reject.callback = self._reject
        self.add_item(approve)
        self.add_item(reject)

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def _approve(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 처리할 수 있습니다.", ephemeral=True
            )
            return
        req = await self.bot.hospital_requests.get_by_id(self.request_id)
        if req is None or req.status != "pending":
            await interaction.response.send_message(
                "이미 처리된 요청입니다.", ephemeral=True
            )
            return
        # 병원 등록 (대표 명칭 + 별칭)
        hospital = await self.bot.verification_service.resolve_hospital(req.name)
        await self.bot.hospital_requests.set_status(
            self.request_id, "approved", reviewer_id=interaction.user.id
        )
        if interaction.guild is not None:
            try:
                await self.bot.channel_service.ensure_hospital_channel(
                    interaction.guild, hospital.id, hospital.name
                )
            except Exception:  # noqa: BLE001
                logger.exception("병원 채널 확보 실패: hospital=%s", hospital.id)
        await interaction.response.send_message(
            f"✅ **{hospital_display_name(hospital.name)}** 을(를) 승인했습니다.",
            ephemeral=True,
        )
        try:
            user = await self.bot.fetch_user(req.user_id)
            await user.send(
                f"✅ 요청하신 병원 **{req.name}** 이(가) 등록되었습니다. "
                "이제 서류 인증을 진행할 수 있습니다."
            )
        except Exception:  # noqa: BLE001
            logger.debug("병원 추가 승인 DM 실패: user=%s", req.user_id)

    async def _reject(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 처리할 수 있습니다.", ephemeral=True
            )
            return
        req = await self.bot.hospital_requests.get_by_id(self.request_id)
        if req is None or req.status != "pending":
            await interaction.response.send_message(
                "이미 처리된 요청입니다.", ephemeral=True
            )
            return
        await self.bot.hospital_requests.set_status(
            self.request_id, "rejected", reviewer_id=interaction.user.id
        )
        await interaction.response.send_message("요청을 거절했습니다.", ephemeral=True)


class OtherVerifyReviewView(discord.ui.View):
    """관리자가 '기타 방법' 인증 신청을 승인/거절하는 영구 뷰."""

    def __init__(self, bot: MindFinderBot, request_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id
        approve = discord.ui.Button(
            label="승인", style=discord.ButtonStyle.success,
            custom_id=f"mf:ovr:approve:{request_id}",
        )
        reject = discord.ui.Button(
            label="거절", style=discord.ButtonStyle.danger,
            custom_id=f"mf:ovr:reject:{request_id}",
        )
        approve.callback = self._approve
        reject.callback = self._reject
        self.add_item(approve)
        self.add_item(reject)

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def _approve(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 처리할 수 있습니다.", ephemeral=True
            )
            return
        req = await self.bot.verifications.get_by_id(self.request_id)
        if req is None or req.status != "pending":
            await interaction.response.send_message(
                "이미 처리된 요청입니다.", ephemeral=True
            )
            return

        # 2단계 인증 승인: 검증 요청/이력 상태 갱신
        await self.bot.verifications.resolve(
            self.request_id, "approved", interaction.user.id
        )
        hospital_name: Optional[str] = None
        if req.admission_id:
            await self.bot.admissions.set_verification(req.admission_id, "verified")
            adm = await self.bot.admissions.get_by_id(req.admission_id)
            if adm is not None and adm.hospital is not None:
                hospital_name = adm.hospital.name

        # 2단계 역할 + 병원 역할/채널 + 기간 스레드 부여
        if interaction.guild is not None:
            shim = _ResultShim(hospital_name) if hospital_name else None
            await grant_verified_access(
                self.bot, interaction.guild, req.user_id, shim
            )

        await interaction.response.send_message(
            "✅ 기타 방법 인증을 승인했습니다.", ephemeral=True
        )
        try:
            user = await self.bot.fetch_user(req.user_id)
            await user.send(
                "✅ 기타 방법 인증이 **승인**되었습니다. "
                "이제 병원·기간 커뮤니티와 매칭을 이용할 수 있습니다."
            )
        except Exception:  # noqa: BLE001
            logger.debug("기타 인증 승인 DM 실패: user=%s", req.user_id)

    async def _reject(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 처리할 수 있습니다.", ephemeral=True
            )
            return
        req = await self.bot.verifications.get_by_id(self.request_id)
        if req is None or req.status != "pending":
            await interaction.response.send_message(
                "이미 처리된 요청입니다.", ephemeral=True
            )
            return
        await self.bot.verifications.resolve(
            self.request_id, "rejected", interaction.user.id
        )
        if req.admission_id:
            await self.bot.admissions.set_verification(req.admission_id, "rejected")
        await interaction.response.send_message("기타 인증을 거절했습니다.", ephemeral=True)
        try:
            user = await self.bot.fetch_user(req.user_id)
            await user.send(
                "🔴 기타 방법 인증이 **거절**되었습니다. "
                "서류 인증 또는 지식기반 인증을 시도해보세요."
            )
        except Exception:  # noqa: BLE001
            logger.debug("기타 인증 거절 DM 실패: user=%s", req.user_id)


class _ResultShim:
    """grant_verified_access 가 기대하는 hospital_name 속성만 가진 경량 객체."""

    def __init__(self, hospital_name: str) -> None:
        self.hospital_name = hospital_name


# --------------------------------------------------------------------------- #
# 후보 목록 뷰 (동적, 비영구)
# --------------------------------------------------------------------------- #
class CandidateView(discord.ui.View):
    """매칭 후보 목록을 버튼으로 보여준다."""

    def __init__(
        self,
        bot: MindFinderBot,
        service: MatchingService,
        requester_id: int,
        candidates: list[Candidate],
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.service = service
        self.requester_id = requester_id
        for idx, cand in enumerate(candidates[:MAX_ACTIONS], start=1):
            my_adm, _other = cand.pairs[0]
            button = discord.ui.Button(
                label=f"후보 {idx}",
                style=discord.ButtonStyle.primary,
                custom_id=f"mf:cand:{idx}",
            )
            button.callback = self._make_callback(cand, my_adm.id)
            self.add_item(button)

    def _make_callback(self, cand: Candidate, admission_id: int):
        async def _callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message(
                    "본인의 후보 목록에서만 신청할 수 있습니다.", ephemeral=True
                )
                return
            request_id, already = await self.service.request_match(
                requester_id=interaction.user.id,
                target_id=cand.user_id,
                admission_id=admission_id,
            )
            if already:
                await interaction.response.send_message(
                    "이미 해당 동료에게 보낸 매칭 신청이 대기 중입니다.", ephemeral=True
                )
                return
            # 상대에게 DM 알림 (실패해도 신청은 유효)
            try:
                target_user = await self.bot.fetch_user(cand.user_id)
                _, other_adm = cand.pairs[0]
                await target_user.send(
                    embed=discord.Embed(
                        title="💌 새 매칭 신청이 도착했습니다",
                        description=(
                            f"같은 병원·시기에 입원했던 동료가 매칭을 신청했습니다.\n"
                            f"병원: {other_adm.hospital.name}\n"
                            f"기간: {other_adm.period_text()}\n\n"
                            "매칭 패널의 '받은 신청' 버튼에서 수락/거절할 수 있습니다."
                        ),
                        color=discord.Color.pink(),
                    )
                )
            except discord.Forbidden:
                logger.info("DM 차단으로 매칭 알림 전송 실패: target=%s", cand.user_id)
            await interaction.response.send_message(
                "✅ 매칭 신청을 보냈습니다. 상대가 수락하면 비공개 채널에서 대화할 수 있습니다.",
                ephemeral=True,
            )

        return _callback


# --------------------------------------------------------------------------- #
# 받은 신청 뷰 (동적, 비영구)
# --------------------------------------------------------------------------- #
class IncomingRequestView(discord.ui.View):
    """받은 매칭 신청을 수락/거절 버튼으로 처리한다."""

    def __init__(
        self,
        bot: MindFinderBot,
        service: MatchingService,
        target_id: int,
        requests: list,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.service = service
        self.target_id = target_id
        for req in requests[:MAX_ACTIONS]:
            accept = discord.ui.Button(
                label=f"#{req.id} 수락",
                style=discord.ButtonStyle.success,
                custom_id=f"mf:acc:{req.id}",
            )
            accept.callback = self._make_callback(req.id, accept=True)
            self.add_item(accept)
            reject = discord.ui.Button(
                label=f"#{req.id} 거절",
                style=discord.ButtonStyle.danger,
                custom_id=f"mf:rej:{req.id}",
            )
            reject.callback = self._make_callback(req.id, accept=False)
            self.add_item(reject)

    def _make_callback(self, request_id: int, accept: bool):
        async def _callback(interaction: discord.Interaction) -> None:
            if interaction.user.id != self.target_id:
                await interaction.response.send_message(
                    "본인의 신청만 처리할 수 있습니다.", ephemeral=True
                )
                return
            if interaction.guild is None:
                await interaction.response.send_message(
                    "서버에서만 처리할 수 있습니다.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            if accept:
                result = await self.service.accept_match(
                    request_id, interaction.user.id, interaction.guild
                )
                if result.error == "invalid":
                    await interaction.followup.send(
                        "이미 처리되었거나 유효하지 않은 신청입니다.", ephemeral=True
                    )
                elif result.channel is None:
                    await interaction.followup.send(
                        "⚠️ 비공개 채널 생성에 실패했습니다. 봇 권한을 확인해주세요.",
                        ephemeral=True,
                    )
                else:
                    await interaction.followup.send(
                        f"✅ 매칭을 수락했습니다. {result.channel.mention} 에서 대화하세요.",
                        ephemeral=True,
                    )
            else:
                ok = await self.service.reject_match(
                    request_id, interaction.user.id
                )
                msg = "매칭 신청을 거절했습니다." if ok else "이미 처리된 신청입니다."
                await interaction.followup.send(msg, ephemeral=True)

        return _callback


# --------------------------------------------------------------------------- #
# 지식기반 인증 (패널 버튼용)
# --------------------------------------------------------------------------- #
class KnowledgeAskModal(discord.ui.Modal, title="지식기반 인증 질문 보내기"):
    """서류 인증을 마친 사용자가 동료에게 병동 경험 질문을 보낸다."""

    질문 = discord.ui.TextInput(
        label="질문",
        style=discord.TextStyle.paragraph,
        placeholder="예: 3월에 입원했을 때 담당 간호사실 위치가 어디였나요?",
        max_length=500,
    )

    def __init__(
        self, bot: MindFinderBot, admission_id: int, respondent_id: int
    ) -> None:
        super().__init__()
        self.bot = bot
        self.admission_id = admission_id
        self.respondent_id = respondent_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        request_id, message = await self.bot.knowledge_service.ask(
            asker_id=interaction.user.id,
            admission_id=self.admission_id,
            respondent_id=self.respondent_id,
            question=str(self.질문.value),
        )
        if request_id is None:
            await interaction.response.send_message(f"⚠️ {message}", ephemeral=True)
            return

        embed = discord.Embed(
            title="❓ 지식기반 인증 질문이 도착했습니다",
            description=(
                f"**대상**: {message}\n\n"
                f"> {str(self.질문.value)}\n\n"
                "이 시기·병원에 실제로 있었다면 답변해주세요. "
                "정답으로 확인되면 관리자 승인을 거쳐 인증이 부여됩니다."
            ),
            color=discord.Color.blurple(),
        )
        try:
            user = await self.bot.fetch_user(self.respondent_id)
            dm = await user.send(
                embed=embed, view=AnswerRequestView(self.bot, request_id)
            )
            await self.bot.knowledge.set_message(request_id, dm.channel.id, dm.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.info("응답자 DM 전송 실패: user=%s", self.respondent_id)
            await interaction.response.send_message(
                "✅ 질문을 등록했습니다. 다만 상대방이 DM 을 받을 수 없어 전달되지 "
                "않았습니다. 상대에게 DM 을 열어달라고 안내해주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "✅ 질문을 보냈습니다. 상대가 답변하면 '지식 인증 현황'에서 판정할 수 있습니다.",
            ephemeral=True,
        )


class KnowledgeAskView(discord.ui.View):
    """질문할 본인 이력(서류 인증 완료)을 고르는 뷰."""

    def __init__(
        self, bot: MindFinderBot, admissions: list, respondent_id: int
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.respondent_id = respondent_id
        options = [
            discord.SelectOption(
                label=f"{adm.hospital.name} {adm.period_text()}"[:100],
                value=str(adm.id),
            )
            for adm in admissions[:25]
        ]
        select = discord.ui.Select(
            placeholder="질문할 본인 이력을 선택하세요", options=options
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        admission_id = int(interaction.data["values"][0])
        await interaction.response.send_modal(
            KnowledgeAskModal(self.bot, admission_id, self.respondent_id)
        )


class KnowledgeUserSelectView(discord.ui.View):
    """질문을 보낼 동료를 고르는 뷰 (같은 병원·시기 경험자)."""

    def __init__(
        self,
        bot: MindFinderBot,
        admissions: list,
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.admissions = admissions
        select = discord.ui.Select(
            placeholder="질문을 보낼 동료를 선택하세요", options=options[:25]
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        respondent_id = int(interaction.data["values"][0])
        await interaction.response.send_message(
            "질문할 본인의 인증된 이력을 선택한 뒤, 질문을 작성해주세요.",
            view=KnowledgeAskView(self.bot, self.admissions, respondent_id),
            ephemeral=True,
        )


# --------------------------------------------------------------------------- #
# 인증 패널 (영구 뷰) — 🔐-인증 채널
# --------------------------------------------------------------------------- #
class VerifyPanelView(discord.ui.View):
    """인증 전용 패널: 입원 이력 등록 / 서류 인증 / 내 이력."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.vservice = VerificationService(bot)

    @discord.ui.button(
        label="입원 이력 등록", style=discord.ButtonStyle.primary,
        custom_id="mf:v_admission", row=0,
    )
    async def admission(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        async def _proceed(inner: discord.Interaction) -> None:
            await inner.response.send_modal(AdmissionModal(self.bot, self.vservice))

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    @discord.ui.button(
        label="서류 인증 (2단계)", style=discord.ButtonStyle.success,
        custom_id="mf:v_verify", row=0,
    )
    async def verify(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """창(모달)에서 병원을 고르고 기간/PDF 를 입력해 인증한다."""

        async def _proceed(inner: discord.Interaction) -> None:
            modal = await UnifiedVerifyModal(self.bot, self.vservice).load()
            await inner.response.send_modal(modal)

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    @discord.ui.button(
        label="기타 방법 인증", style=discord.ButtonStyle.secondary,
        custom_id="mf:v_other", row=1,
    )
    async def other_method(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """서류·지식기반 외의 방법으로 인증을 신청한다 (관리자 수동 승인)."""

        async def _proceed(inner: discord.Interaction) -> None:
            await inner.response.send_modal(OtherVerifyModal(self.bot, self.vservice))

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    @discord.ui.button(
        label="지식기반 질문 보내기", style=discord.ButtonStyle.secondary,
        custom_id="mf:v_ask", row=1,
    )
    async def ask_knowledge(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """서류 인증자로서 동료에게 병동 경험 질문을 보낸다 (지식기반 인증)."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        admissions = [
            a
            for a in await self.bot.admissions.list_by_user(interaction.user.id)
            if a.verification == "verified"
        ]
        if not admissions:
            await interaction.response.send_message(
                "⚠️ 서류 인증을 마친 이력이 있어야 질문할 수 있습니다.",
                ephemeral=True,
            )
            return

        # 같은 병원·시기 경험자(자기 자신 제외)를 후보로 모은다.
        seen: set[int] = set()
        options: list[discord.SelectOption] = []
        for adm in admissions:
            others = await self.bot.admissions.find_overlapping(
                hospital_id=adm.hospital_id,
                admit_ym_val=adm.admit_ym,
                discharge_ym_val=adm.discharge_ym,
                exclude_user_id=interaction.user.id,
            )
            for other in others:
                if other.user_id in seen:
                    continue
                seen.add(other.user_id)
                name = MatchingService.mask_name(interaction.guild, other.user_id)
                options.append(
                    discord.SelectOption(
                        label=(
                            f"{name} · {other.hospital.name} {other.period_text()}"
                        )[:100],
                        value=str(other.user_id),
                    )
                )
        if not options:
            await interaction.response.send_message(
                "같은 병원·시기에 등록된 동료가 아직 없습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "질문을 보낼 동료를 선택하세요.",
            view=KnowledgeUserSelectView(self.bot, admissions, options),
            ephemeral=True,
        )

    @discord.ui.button(
        label="지식 인증 현황", style=discord.ButtonStyle.secondary,
        custom_id="mf:v_kstatus", row=1,
    )
    async def knowledge_status(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """내가 받은 질문(답변)과 내가 보낸 질문(판정)을 한 번에 보여준다."""
        to_answer = await self.bot.knowledge_service.list_for_respondent(
            interaction.user.id, "awaiting_answer"
        )
        to_verdict = await self.bot.knowledge_service.list_for_respondent(
            interaction.user.id, "awaiting_verdict"
        )
        embed = discord.Embed(
            title="🧠 지식기반 인증 현황", color=discord.Color.blurple()
        )
        if to_answer:
            embed.add_field(
                name=f"답변할 질문 ({len(to_answer)}건)",
                value="\n".join(f"#{r.id} · {r.period_text}" for r in to_answer[:5]),
                inline=False,
            )
        if to_verdict:
            embed.add_field(
                name=f"판정할 질문 ({len(to_verdict)}건)",
                value="\n".join(f"#{r.id} · {r.period_text}" for r in to_verdict[:5]),
                inline=False,
            )
        if not to_answer and not to_verdict:
            embed.description = "진행 중인 지식기반 인증이 없습니다."
        await interaction.response.send_message(embed=embed, ephemeral=True)
        if to_answer:
            await interaction.followup.send(
                "받은 질문에 답변하세요.",
                view=AnswerRequestView(self.bot, to_answer[0].id),
                ephemeral=True,
            )
        if to_verdict:
            await interaction.followup.send(
                "보낸 질문의 응답을 판정하세요.",
                view=VerdictView(self.bot, to_verdict[0].id),
                ephemeral=True,
            )

    @discord.ui.button(
        label="기타 병원 직접 입력", style=discord.ButtonStyle.secondary,
        custom_id="mf:v_request", row=0,
    )
    async def request_hospital(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """목록에 없는 병원을 직접 입력해 등록을 요청한다 (관리자 승인 후 추가)."""

        async def _proceed(inner: discord.Interaction) -> None:
            await inner.response.send_modal(HospitalRequestModal(self.bot))

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    @discord.ui.button(
        label="내 이력", style=discord.ButtonStyle.secondary,
        custom_id="mf:v_history", row=0,
    )
    async def history(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        rows = await self.bot.admissions.list_by_user(interaction.user.id)
        if not rows:
            await interaction.response.send_message(
                "아직 등록된 이력이 없습니다. '입원 이력 등록' 버튼을 눌러주세요.",
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
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# 매칭 패널 (영구 뷰) — 🧩-매칭 채널
# --------------------------------------------------------------------------- #
class MatchPanelView(discord.ui.View):
    """매칭 전용 패널: 매칭 희망 설정 / 동료 찾기 / 받은 신청 / 내 채널."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.mservice = MatchingService(bot)

    @discord.ui.button(
        label="매칭 희망 켜기", style=discord.ButtonStyle.success,
        custom_id="mf:m_on", row=0,
    )
    async def match_on(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        try:
            await self.mservice.set_matching(interaction.user.id, True)
        except ValueError as exc:
            await interaction.response.send_message(
                f"⚠️ {exc}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "🟢 매칭을 **원함**으로 설정했습니다.", ephemeral=True
        )

    @discord.ui.button(
        label="매칭 희망 끄기", style=discord.ButtonStyle.secondary,
        custom_id="mf:m_off", row=0,
    )
    async def match_off(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.mservice.set_matching(interaction.user.id, False)
        await interaction.response.send_message(
            "⚪ 매칭을 **원하지 않음**으로 설정했습니다.", ephemeral=True
        )

    @discord.ui.button(
        label="동료 찾기", style=discord.ButtonStyle.primary,
        custom_id="mf:m_find", row=1,
    )
    async def find(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        try:
            matched, total = await self.mservice.find_candidates(interaction.user.id)
        except ValueError:
            await interaction.response.send_message(
                "먼저 '입원 이력 등록'으로 이력을 등록해주세요.", ephemeral=True
            )
            return
        if not matched:
            reason = (
                f"같은 병원·시기 동료 {total}명을 찾았지만, 매칭을 원하는 분이 없습니다."
                if total
                else "아직 같은 병원·시기로 등록된 동료가 없습니다."
            )
            await interaction.response.send_message(reason, ephemeral=True)
            return
        embed = discord.Embed(
            title="🔍 매칭 후보",
            description=(
                f"매칭을 원하는 동료 **{len(matched)}명**을 찾았습니다. "
                "아래 버튼으로 신청하세요."
            ),
            color=discord.Color.green(),
        )
        for idx, cand in enumerate(matched[:MAX_ACTIONS], start=1):
            _my, other = cand.pairs[0]
            display = self.mservice.mask_name(interaction.guild, cand.user_id)
            embed.add_field(
                name=f"후보 {idx} · {display}",
                value=f"{other.hospital.name} {other.period_text()}",
                inline=False,
            )
        await interaction.response.send_message(
            embed=embed,
            view=CandidateView(self.bot, self.mservice, interaction.user.id, matched),
            ephemeral=True,
        )

    @discord.ui.button(
        label="받은 신청", style=discord.ButtonStyle.success,
        custom_id="mf:m_incoming", row=2,
    )
    async def incoming(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
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
            description="아래 버튼으로 수락/거절하세요.",
            color=discord.Color.pink(),
        )
        for req in pending[:MAX_ACTIONS]:
            embed.add_field(
                name=f"신청 #{req.id}",
                value=f"보낸 사람: <@{req.requester_id}>",
                inline=False,
            )
        await interaction.response.send_message(
            embed=embed,
            view=IncomingRequestView(
                self.bot, self.mservice, interaction.user.id, pending
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="내 매칭 채널", style=discord.ButtonStyle.secondary,
        custom_id="mf:m_channels", row=2,
    )
    async def channels(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        chans = await self.mservice.my_open_channels(
            interaction.guild, interaction.user.id
        )
        if not chans:
            await interaction.response.send_message(
                "참여 중인 매칭 채널이 없습니다.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"🔒 내 매칭 채널 ({len(chans)}개)",
            color=discord.Color.blurple(),
        )
        for ch in chans:
            embed.add_field(name=ch.name, value=ch.mention, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# 커뮤니티 패널 (영구 뷰) — 💬-전체-커뮤니티 채널
# --------------------------------------------------------------------------- #
class CommunityPanelView(discord.ui.View):
    """커뮤니티 안내 패널: 전체 커뮤니티 / 병원·기간 스레드."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="내 기간 스레드", style=discord.ButtonStyle.primary,
        custom_id="mf:c_threads", row=0,
    )
    async def my_threads(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """내 인증 이력(병원·기간)에 맞는 비공개 월 스레드를 확보/표시한다."""
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.channel_service.ensure_community_access(
                interaction.guild, interaction.user.id
            )
        except Exception:  # noqa: BLE001
            logger.exception("커뮤니티 접근 동기화 실패: user=%s", interaction.user.id)

        admissions = await self.bot.admissions.list_by_user(interaction.user.id)
        verified_ids = set(
            await self.bot.access.verified_hospital_ids(interaction.user.id)
        )
        member = interaction.guild.get_member(interaction.user.id)
        embed = discord.Embed(
            title="🏥 내 병원·기간 커뮤니티",
            description=(
                "서류 인증(2단계)을 마친 병원의, 내 입원 기간에 해당하는 "
                "**비공개 월 스레드**만 표시됩니다."
            ),
            color=discord.Color.teal(),
        )
        found = False
        for adm in admissions:
            if adm.hospital_id not in verified_ids:
                continue
            labels: list[str] = []
            for ym_value in self.bot.channel_service.months_for_admission(adm):
                thread = await self.bot.channel_service.ensure_period_thread(
                    interaction.guild,
                    adm.hospital_id,
                    adm.hospital.name,
                    format_ym(ym_value),
                    private=True,
                )
                if thread is None:
                    continue
                if member is not None:
                    await self.bot.channel_service.add_member_to_thread(thread, member)
                labels.append(thread.mention)
            if labels:
                found = True
                embed.add_field(
                    name=hospital_display_name(adm.hospital.name),
                    value=" · ".join(labels[:25]),
                    inline=False,
                )
        if not found:
            embed.add_field(
                name="표시할 기간 스레드 없음",
                value=(
                    "‘서류 인증 (2단계)’를 완료한 병원의 이력이 있어야 "
                    "병원·기간 커뮤니티가 열립니다."
                ),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="병원별 안내", style=discord.ButtonStyle.secondary,
        custom_id="mf:c_hospital", row=0,
    )
    async def hospital_guide(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        hospitals = await self.bot.hospitals.list_all()
        embed = discord.Embed(
            title="🏥 병원별 커뮤니티 안내",
            description=(
                "서류 인증을 받으면 **내가 인증받은 병원**의 전용 채널이 열립니다.\n"
                "그 안에 **월 단위 비공개 스레드**가 생기고, "
                "내 입원 기간에 해당하는 스레드에만 참여됩니다."
            ),
            color=discord.Color.teal(),
        )
        names = (
            ", ".join(h.name for h in hospitals[:20])
            or "아직 등록된 병원이 없습니다."
        )
        embed.add_field(name="등록된 병원", value=names, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="매칭 안내", style=discord.ButtonStyle.secondary,
        custom_id="mf:c_match", row=0,
    )
    async def match_guide(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "🧩 매칭은 **서류 인증자(2단계)** 만 이용할 수 있습니다.\n"
            "매칭 패널에서 '매칭 희망 켜기' → '동료 찾기' 로 신청하세요.\n"
            "연락처는 봇이 저장하지 않으며, 매칭 성사 시 비공개 채널에서 직접 대화합니다.",
            ephemeral=True,
        )


# --------------------------------------------------------------------------- #
# 안내 패널 (영구 뷰) — 🧭-패널 채널
# --------------------------------------------------------------------------- #
class HelpPanelView(discord.ui.View):
    """안내·도움말 패널: 사용법, 약관·개인정보."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="사용법", style=discord.ButtonStyle.primary,
        custom_id="mf:h_help", row=0,
    )
    async def help_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        embed = discord.Embed(
            title="🧭 MindFinder 사용법",
            description=(
                "각 기능은 **전용 채널**의 패널 버튼으로 사용합니다.\n"
                "슬래시 명령어도 그대로 사용할 수 있습니다."
            ),
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="🔐 인증",
            value=(
                "**입원 이력 등록** — 병원·기간 입력 (자기 신고)\n"
                "**서울대병원 서류 인증** — 사실확인서 PDF 자동 진위확인\n"
                "**다른 병원 서류 인증** — 관리자 수동 심사\n"
                "**내 이력** — 등록 이력 확인"
            ),
            inline=False,
        )
        embed.add_field(
            name="🧩 매칭",
            value=(
                "**매칭 희망 켜기/끄기** — 매칭 참여 여부\n"
                "**동료 찾기** — 같은 병원·시기 동료에게 신청\n"
                "**받은 신청** — 온 신청 수락/거절\n"
                "**내 매칭 채널** — 비공개 채널 목록"
            ),
            inline=False,
        )
        embed.set_footer(
            text="전화번호/SNS는 봇이 저장·전달하지 않습니다. 비공개 채널에서 직접 대화하세요."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="약관·개인정보", style=discord.ButtonStyle.secondary,
        custom_id="mf:h_consent", row=0,
    )
    async def consent_info(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            embed=build_consent_embed(self.bot), ephemeral=True
        )

    @discord.ui.button(
        label="위기 상담 안내", style=discord.ButtonStyle.danger,
        custom_id="mf:h_crisis", row=0,
    )
    async def crisis_help(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "지금 많이 힘드시다면 아래에 연락해 주세요.\n"
            "· 자살예방 상담전화 **109** (24시간)\n"
            "· 정신건강 상담전화 **1577-0199** (24시간)\n"
            "· 생명의전화 **1588-9191**\n"
            "당신의 이야기를 들어줄 사람이 있습니다.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="내 이용 현황", style=discord.ButtonStyle.success,
        custom_id="mf:h_status", row=0,
    )
    async def my_status(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """내 인증 단계·입원 이력·매칭 상태를 한 번에 보여준다 (실제 조회 기능)."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        bot = self.bot
        tier = await bot.access.user_tier(interaction.user.id)
        tier_label = {
            0: "미인증 — 먼저 입원 이력을 등록하세요",
            1: "1단계 (자기 신고) — 전체 커뮤니티 이용 가능",
            2: "2단계 (서류·지식 인증) — 병원 커뮤니티·매칭 이용 가능",
        }.get(tier, f"{tier}단계")
        embed = discord.Embed(title="🧭 내 이용 현황", color=discord.Color.teal())
        embed.add_field(name="인증 단계", value=tier_label, inline=False)

        admissions = await bot.admissions.list_by_user(interaction.user.id)
        if admissions:
            lines = [
                f"· #{a.id} {a.hospital.name} · {a.period_text()} "
                f"— {VERIFICATION_LABEL.get(a.verification, a.verification)}"
                for a in admissions
            ]
            embed.add_field(
                name="입원 이력", value="\n".join(lines)[:1024], inline=False
            )
        else:
            embed.add_field(
                name="입원 이력",
                value="등록된 이력이 없습니다. **🔐 인증** 채널에서 등록하세요.",
                inline=False,
            )

        pref = await bot.match_prefs.is_enabled(interaction.user.id)
        can_match = await bot.access.can_match(interaction.user.id)
        match_lines = [f"희망: {'켜짐' if pref else '꺼짐'}"]
        match_lines.append("사용 가능" if can_match else "2단계 인증 후 사용 가능")
        if interaction.guild is not None:
            try:
                chans = await bot.matching_service.my_open_channels(
                    interaction.guild, interaction.user.id
                )
            except Exception:  # noqa: BLE001
                chans = []
            if chans:
                match_lines.append("내 채널: " + ", ".join(c.mention for c in chans))
        embed.add_field(name="매칭", value="\n".join(match_lines)[:1024], inline=False)
        embed.set_footer(text="각 기능은 해당 채널의 패널 버튼에서 사용하세요.")
        await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# 관리자 패널 (영구 뷰) — 🛠-관리자 채널
# --------------------------------------------------------------------------- #
class AdminPanelView(discord.ui.View):
    """관리자 전용 패널: 병원 추가 요청 검토 / 약관 재게시 / 채널 재구성."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "관리자만 사용할 수 있습니다.", ephemeral=True
        )

    @discord.ui.button(
        label="병원 추가 요청 검토", style=discord.ButtonStyle.primary,
        custom_id="mf:a_hospitals", row=0,
    )
    async def hospital_requests(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not self._is_admin(interaction):
            await self._deny(interaction)
            return
        pending = await self.bot.hospital_requests.list_by_status("pending")
        if not pending:
            await interaction.response.send_message(
                "대기 중인 병원 추가 요청이 없습니다.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=f"🏥 병원 추가 요청 ({len(pending)}건)",
            description=(
                "아래 요청을 검토해 **승인/거절**하세요. "
                "승인하면 병원이 등록되고 전용 채널이 만들어집니다."
            ),
            color=discord.Color.orange(),
        )
        for req in pending[:MAX_ACTIONS]:
            ward = f" · {req.ward}" if getattr(req, "ward", None) else ""
            reason = getattr(req, "reason", None) or "(사유 없음)"
            embed.add_field(
                name=f"요청 `{req.id}` · {req.name}{ward}",
                value=f"요청자: <@{req.user_id}>\n사유: {reason}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        # 각 요청에 대한 승인/거절 버튼을 관리자 채널에 게시한다.
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            return
        for req in pending[:MAX_ACTIONS]:
            ward = f" · {req.ward}" if getattr(req, "ward", None) else ""
            try:
                await channel.send(
                    f"🏥 **병원 추가 요청** (`{req.id}`)\n"
                    f"· 요청자: <@{req.user_id}>\n"
                    f"· 병원명: **{req.name}**{ward}",
                    view=HospitalRequestReviewView(self.bot, req.id),
                )
            except discord.HTTPException:
                logger.exception("병원 요청 검토 게시 실패: request=%s", req.id)

    @discord.ui.button(
        label="약관 재게시", style=discord.ButtonStyle.secondary,
        custom_id="mf:a_terms", row=0,
    )
    async def repost_terms(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not self._is_admin(interaction):
            await self._deny(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.terms_service.post_all(interaction.guild)
        except Exception:  # noqa: BLE001
            logger.exception("약관 재게시 실패: guild=%s", interaction.guild.id)
            await interaction.followup.send(
                "⚠️ 약관 재게시 중 오류가 발생했습니다.", ephemeral=True
            )
            return
        await interaction.followup.send(
            "✅ 약관·개인정보 전문을 다시 게시했습니다.", ephemeral=True
        )

    @discord.ui.button(
        label="채널 재구성", style=discord.ButtonStyle.secondary,
        custom_id="mf:a_channels", row=0,
    )
    async def rebuild_channels(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not self._is_admin(interaction):
            await self._deny(interaction)
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.channel_service.ensure_all(interaction.guild)
            cog = interaction.client.get_cog("PanelCog")
            if cog is not None:
                await cog.ensure_panels(interaction.guild)
        except Exception:  # noqa: BLE001
            logger.exception("채널 재구성 실패: guild=%s", interaction.guild.id)
            await interaction.followup.send(
                "⚠️ 채널 재구성 중 오류가 발생했습니다. "
                "봇에 '채널 관리' 권한이 있는지 확인해주세요.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "✅ 용도별 채널/패널 구성을 완료했습니다.", ephemeral=True
        )


# --------------------------------------------------------------------------- #
# 공용 헬퍼: 진위확인 통과자 접근 권한 부여
# --------------------------------------------------------------------------- #
async def grant_verified_access(
    bot: MindFinderBot, guild: discord.Guild, user_id: int, result=None
) -> None:
    """자동 진위확인 통과자에게 2단계 역할 + 병원 역할/채널을 부여한다."""
    member = guild.get_member(user_id)
    if member is None:
        return
    # 2단계(서류 인증) 역할 (설정된 VERIFIED_ROLE_ID 가 있으면 함께 부여)
    try:
        await bot.access.grant_tier(guild, member, 2)
    except Exception:  # noqa: BLE001
        logger.exception("2단계 역할 부여 실패: user=%s", user_id)
    role_id = bot.settings.verified_role_id
    if role_id:
        role = guild.get_role(role_id)
        if role is not None:
            try:
                await member.add_roles(role, reason="자동 진위확인 인증")
            except discord.Forbidden:
                logger.warning("인증 역할 부여 권한 없음: user=%s", user_id)

    # 병원 역할 + 병원 채널 확보
    hospital_name = getattr(result, "hospital_name", None)
    if not hospital_name:
        return
    hospital = await bot.hospitals.get_by_name(hospital_name)
    if hospital is None:
        hospital = await VerificationService(bot).resolve_hospital(hospital_name)
    try:
        await bot.access.grant_hospital_role(
            guild, member, hospital.id, hospital.name
        )
        await bot.channel_service.ensure_hospital_channel(
            guild, hospital.id, hospital.name
        )
        # 입원 기간(월)에 맞는 비공개 기간 스레드에 초대한다.
        await bot.channel_service.ensure_community_access(guild, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("병원 역할/채널 처리 실패: hospital=%s", hospital.id)


# --------------------------------------------------------------------------- #
# 코그: 패널 게시 및 패널 채널 PDF 자동 진위확인
# --------------------------------------------------------------------------- #
class PanelCog(commands.Cog):
    """용도별 채널에 상시 패널을 게시하고, 인증 채널의 PDF 를 자동 진위확인한다."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot
        self.vservice = VerificationService(bot)
        # 영구 뷰 등록 (재시작 후에도 버튼이 동작하도록)
        bot.add_view(VerifyPanelView(bot))
        bot.add_view(MatchPanelView(bot))
        bot.add_view(CommunityPanelView(bot))
        bot.add_view(AdminPanelView(bot))
        bot.add_view(HelpPanelView(bot))
        self._panels_ready = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._panels_ready:
            return
        self._panels_ready = True
        # 재시작 전 접수된 '병원 추가 요청' 검토 버튼을 다시 살린다.
        try:
            pending = await self.bot.hospital_requests.list_by_status("pending")
        except Exception:  # noqa: BLE001
            pending = []
            logger.exception("병원 추가 요청 조회 실패")
        for req in pending:
            try:
                self.bot.add_view(HospitalRequestReviewView(self.bot, req.id))
            except Exception:  # noqa: BLE001
                logger.exception("병원 요청 뷰 등록 실패: request=%s", req.id)

        # 재시작 전 접수된 '기타 방법 인증' 검토 버튼을 다시 살린다.
        try:
            other_pending = await self.bot.verifications.list_pending_by_method("other")
        except Exception:  # noqa: BLE001
            other_pending = []
            logger.exception("기타 인증 요청 조회 실패")
        for req in other_pending:
            try:
                self.bot.add_view(OtherVerifyReviewView(self.bot, req.id))
            except Exception:  # noqa: BLE001
                logger.exception("기타 인증 뷰 등록 실패: request=%s", req.id)

        for guild in self.bot.guilds:
            # 1) 용도별 채널/카테고리 전체 구성 (인증/커뮤니티/매칭/관리자/약관/병원별)
            try:
                await self.bot.channel_service.ensure_all(guild)
            except Exception:  # noqa: BLE001 - 한 서버 실패가 전체를 막지 않도록
                logger.exception("채널 자동 구성 실패: guild=%s", guild.id)
            # 2) 약관·개인정보 전문 게시 (전용 채널)
            try:
                await self.bot.terms_service.post_all(guild)
            except Exception:  # noqa: BLE001
                logger.exception("약관 게시 실패: guild=%s", guild.id)
            # 3) 기능별 패널 게시
            await self.ensure_panels(guild)

    # ---------- 패널 게시 ----------

    async def ensure_panels(self, guild: discord.Guild) -> None:
        """기능별 채널에 상시 패널을 게시/갱신한다."""
        specs = [
            (VERIFY_CHANNEL_KEY, PANEL_MARKER_VERIFY, self._build_verify_embed, VerifyPanelView),
            (MATCH_CHANNEL_KEY, PANEL_MARKER_MATCH, self._build_match_embed, MatchPanelView),
            (
                COMMUNITY_CHANNEL_KEY,
                PANEL_MARKER_COMMUNITY,
                self._build_community_embed,
                CommunityPanelView,
            ),
            (ADMIN_CHANNEL_KEY, PANEL_MARKER_ADMIN, self._build_admin_embed, AdminPanelView),
            (PANEL_CHANNEL_KEY, PANEL_MARKER_HELP, self._build_help_embed, HelpPanelView),
        ]
        for key, marker, embed_builder, view_cls in specs:
            channel = await self._managed_channel(guild, key)
            if channel is None:
                continue
            await self._post_panel(channel, marker, embed_builder(), view_cls(self.bot))

    async def _managed_channel(
        self, guild: discord.Guild, key: str
    ) -> Optional[discord.TextChannel]:
        """관리 대상 채널을 반환한다 (설정값 우선, 없으면 managed_channels)."""
        settings = self.bot.settings
        if key == PANEL_CHANNEL_KEY and settings.panel_channel_id:
            ch = guild.get_channel(settings.panel_channel_id)
            if isinstance(ch, discord.TextChannel):
                return ch
        if key == VERIFY_CHANNEL_KEY and settings.verification_channel_id:
            ch = guild.get_channel(settings.verification_channel_id)
            if isinstance(ch, discord.TextChannel):
                return ch
        return await self.bot.channel_service.managed_text_channel(guild, key)

    @staticmethod
    def _embed_signature(embed: discord.Embed) -> str:
        """임베드 내용을 비교 가능한 문자열로 만든다 (변경 감지용)."""
        parts = [embed.title or "", embed.description or "", str(embed.color)]
        for field in embed.fields:
            parts.append(f"{field.name}\x1f{field.value}\x1f{field.inline}")
        parts.append(str(getattr(embed.footer, "text", "") or ""))
        return "\x1e".join(parts)

    @staticmethod
    def _items_signature(items) -> str:  # noqa: ANN001
        """뷰 구성요소(버튼/셀렉트)를 비교 가능한 문자열로 만든다."""
        parts: list[str] = []
        for item in items:
            parts.append(
                "\x1f".join(
                    [
                        str(getattr(item, "custom_id", "") or ""),
                        str(getattr(item, "label", "") or ""),
                        str(getattr(item, "style", "") or ""),
                        str(getattr(item, "disabled", "") or ""),
                    ]
                )
            )
        return "\x1e".join(parts)

    @staticmethod
    def _message_items(message: discord.Message) -> list:
        """기존 메시지의 구성요소(버튼/셀렉트)를 평탄화해 반환한다."""
        items: list = []
        for row in message.components:
            items.extend(getattr(row, "children", []))
        return items

    async def _post_panel(
        self,
        channel: discord.TextChannel,
        marker: str,
        embed: discord.Embed,
        view: discord.ui.View,
    ) -> None:
        """채널에 패널 메시지를 게시한다.

        내용이 이전과 같으면 그대로 두고, **변경사항이 있으면 기존 고정
        메시지를 지우고 새로 만들어 다시 고정**한다. (중복 패널도 정리)
        """
        matches: list[discord.Message] = []
        try:
            async for msg in channel.history(limit=50):
                if (
                    msg.author.id == self.bot.user.id
                    and msg.embeds
                    and msg.embeds[0].footer
                    and msg.embeds[0].footer.text == marker
                ):
                    matches.append(msg)
        except discord.HTTPException:
            logger.exception("패널 메시지 조회 실패: channel=%s", channel.id)

        unchanged = bool(matches) and (
            self._embed_signature(matches[0].embeds[0]) == self._embed_signature(embed)
            and self._items_signature(self._message_items(matches[0]))
            == self._items_signature(view.children)
        )
        if unchanged:
            # 내용이 같으면 유지하고, 혹시 남은 중복 패널만 정리한다.
            for extra in matches[1:]:
                try:
                    await extra.delete()
                except discord.HTTPException:
                    logger.exception("중복 패널 삭제 실패: channel=%s", channel.id)
            return

        # 변경사항이 있으면 기존 패널(고정 포함)을 지우고 새로 만든다.
        for msg in matches:
            try:
                await msg.delete()
            except discord.HTTPException:
                logger.exception("패널 삭제 실패: channel=%s", channel.id)
        try:
            sent = await channel.send(embed=embed, view=view)
            logger.info("패널 게시: channel=%s marker=%s", channel.id, marker)
            try:
                await sent.pin(reason="MindFinder 상시 패널 고정")
            except discord.HTTPException:
                logger.exception("패널 고정 실패: channel=%s", channel.id)
        except discord.HTTPException:
            logger.exception("패널 전송 실패: channel=%s", channel.id)

    # ---------- 패널 임베드 ----------

    def _build_verify_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔐 인증",
            description=(
                "이곳에서 **입원 이력**을 등록하고 **서류 인증**을 받습니다.\n"
                "아래 버튼을 누르면 입력 창이 열립니다."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="① 입원 이력 등록 (자기 신고)",
            value="병원·기간을 입력하면 1단계 인증 → **전체 커뮤니티** 이용",
            inline=False,
        )
        embed.add_field(
            name="② 서류 인증 (2단계)",
            value=(
                "‘서류 인증’ 버튼 → **창(모달)에서 병원 선택** 후 진행\n"
                "**서울대병원 계열** 사실확인서 PDF → 자동 진위확인\n"
                "**그 외 병원** → 입원 기간 입력 후 관리자 수동 심사\n"
                "목록에 없는 병원은 ‘기타 병원 직접 입력’으로 요청\n"
                "2단계 인증 → **병원·기간 커뮤니티 + 매칭** 이용"
            ),
            inline=False,
        )
        embed.add_field(
            name="③ 지식기반 인증 / 기타 방법",
            value=(
                "이미 서류 인증을 마친 사람이라면 **‘지식기반 질문 보내기’** 로 "
                "동료에게 병동 경험 질문을 보낼 수 있습니다(‘지식 인증 현황’에서 "
                "답변·판정).\n"
                "서류·지식 외의 방법은 **‘기타 방법 인증’** 으로 사유를 적어 "
                "관리자 수동 승인을 요청하세요."
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_MARKER_VERIFY)
        return embed

    def _build_match_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🧩 매칭",
            description=(
                "같은 병원·시기에 입원했던 동료와 연결됩니다. "
                "(서류 인증자 전용)"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="이용 순서",
            value=(
                "1) **매칭 희망 켜기**\n"
                "2) **동료 찾기** → 후보에게 신청\n"
                "3) 상대가 **수락**하면 비공개 채널 개설"
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_MARKER_MATCH)
        return embed

    def _build_community_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="💬 전체 커뮤니티",
            description=(
                "인증(1단계) 이상이면 이용할 수 있는 전체 커뮤니티입니다.\n"
                "서류 인증을 받으면 **병원별·기간별** 채널도 열립니다."
            ),
            color=discord.Color.teal(),
        )
        embed.set_footer(text=PANEL_MARKER_COMMUNITY)
        return embed

    def _build_help_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🧭 MindFinder 안내",
            description=(
                "폐쇄병동 입원 경험자들을 위한 커뮤니티입니다.\n"
                "기능별 채널의 패널 버튼으로 이용하세요."
            ),
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="채널 안내",
            value=(
                "🔐 **인증** — 이력 등록·서류 인증\n"
                "🧩 **매칭** — 동료 찾기 (서류 인증자)\n"
                "💬 **전체 커뮤니티** — 인증자 커뮤니티\n"
                "🏥 **병원-커뮤니티** — 병원·기간별"
            ),
            inline=False,
        )
        embed.add_field(
            name="내 이용 현황",
            value=(
                "아래 **‘내 이용 현황’** 버튼으로 내 **인증 단계·입원 이력·"
                "매칭 상태·매칭 채널**을 한 번에 확인할 수 있습니다."
            ),
            inline=False,
        )
        embed.add_field(
            name="개인정보 보호",
            value=(
                "연락처는 봇이 저장·전달하지 않습니다. "
                "매칭 성사 시 비공개 채널에서 직접 대화하세요."
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_MARKER_HELP)
        return embed

    def _build_admin_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🛠 관리자 패널",
            description=(
                "관리자 전용 기능입니다. (관리자 권한 보유자만 동작)\n"
                "병원 추가 요청 검토, 약관 재게시, 채널 재구성을 할 수 있습니다."
            ),
            color=discord.Color.dark_red(),
        )
        embed.add_field(
            name="병원 추가 요청 검토",
            value=(
                "사용자가 ‘기타 병원 직접 입력’으로 요청한 병원을 **승인/거절**합니다.\n"
                "승인 시 병원이 등록되고 전용 채널이 생성됩니다."
            ),
            inline=False,
        )
        embed.add_field(
            name="약관 재게시 / 채널 재구성",
            value=(
                "약관·개인정보 전문을 다시 게시하거나, "
                "용도별 채널·패널 구성을 다시 실행합니다."
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_MARKER_ADMIN)
        return embed

    # ---------- 인증 채널의 PDF 자동 진위확인 (메시지 업로드 지원) ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """인증 채널에 올라온 서울대병원 PDF 를 자동 진위확인한다."""
        if message.author.bot or message.guild is None:
            return
        if not message.attachments:
            return

        verify_channel_id = await self._managed_channel_id(
            message.guild, VERIFY_CHANNEL_KEY
        )
        if verify_channel_id is None or message.channel.id != verify_channel_id:
            return

        pdfs = [
            a for a in message.attachments if a.filename.lower().endswith(".pdf")
        ]
        if not pdfs:
            return
        if not self.bot.settings.snuh_truth_enabled:
            return

        attachment = pdfs[0]
        try:
            pdf_bytes = await attachment.read()
        except discord.HTTPException:
            logger.warning("PDF 다운로드 실패: %s", attachment.filename)
            return

        result = await self.vservice.auto_verify_pdf(
            user_id=message.author.id,
            filename=attachment.filename,
            pdf_bytes=pdf_bytes,
        )
        if result.approved:
            await grant_verified_access(
                self.bot, message.guild, message.author.id, result
            )

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
        try:
            await message.reply(embed=embed, mention_author=True)
        except discord.HTTPException:
            logger.warning("진위확인 결과 응답 실패: channel=%s", message.channel.id)

    async def _managed_channel_id(
        self, guild: discord.Guild, key: str
    ) -> Optional[int]:
        """설정(.env) 또는 DB에 기록된 관리 대상 채널 ID를 반환한다."""
        settings = self.bot.settings
        if key == PANEL_CHANNEL_KEY and settings.panel_channel_id:
            return settings.panel_channel_id
        if key == VERIFY_CHANNEL_KEY and settings.verification_channel_id:
            return settings.verification_channel_id
        record = await self.bot.managed_channels.get(guild.id, key)
        return record.discord_id if record is not None else None

    # ---------- 관리자: 패널 수동 재게시 ----------

    @app_commands.command(
        name="패널", description="[관리자] 기능별 안내 패널을 (재)게시합니다."
    )
    async def repost_panel(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.ensure_panels(interaction.guild)
        await interaction.followup.send(
            "✅ 기능별 안내 패널을 게시했습니다.", ephemeral=True
        )

    # ---------- 관리자: 채널 구성 재실행 ----------

    @app_commands.command(
        name="채널구성", description="[관리자] 용도별 채널/카테고리를 (재)구성합니다."
    )
    async def setup_channels(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "관리자만 사용할 수 있습니다.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.bot.channel_service.ensure_all(interaction.guild)
            await self.ensure_panels(interaction.guild)
        except Exception:  # noqa: BLE001
            logger.exception("채널 구성 실패: guild=%s", interaction.guild.id)
            await interaction.followup.send(
                "⚠️ 채널 구성 중 오류가 발생했습니다. "
                "봇에 '채널 관리' 권한이 있는지 확인해주세요.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "✅ 용도별 채널/카테고리 구성을 완료했습니다.", ephemeral=True
        )


# 기능별 패널 마커 (중복 게시 방지용, 임베드 푸터에 기록)
PANEL_MARKER_VERIFY = "MindFinder · 인증 패널"
PANEL_MARKER_MATCH = "MindFinder · 매칭 패널"
PANEL_MARKER_COMMUNITY = "MindFinder · 커뮤니티 패널"
PANEL_MARKER_ADMIN = "MindFinder · 관리자 패널"
PANEL_MARKER_HELP = "MindFinder 안내 패널"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCog(bot))
