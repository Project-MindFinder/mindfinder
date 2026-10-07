"""상시 패널 코그: 용도별 채널에 고정 패널을 게시하고 버튼/모달로 기능을 제공한다.

슬래시 커맨드 대신, **기능별 전용 채널**에 항상 떠 있는 안내 패널을 게시하고
버튼을 눌러 기능을 사용하는 방식을 제공한다.
(슬래시 커맨드도 그대로 유지되어 둘 다 사용할 수 있다.)

- 채널이 없으면 봇이 자동으로 만든다(ChannelService.ensure_all).
- 패널 메시지는 재시작해도 유지되도록 영구 뷰(persistent view)를 사용한다.
- 서울대병원 사실확인서 PDF 는 패널 버튼 → **창(모달) 업로드** 로 제출한다.
"""

from __future__ import annotations

import io
import logging
import random
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.db.seed import reset_verifications
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
def _year_options() -> list[discord.SelectOption]:
    """올해부터 24년 전까지의 연도 옵션 (디스코드 옵션 최대 25개)."""
    current = datetime.now().year
    return [
        discord.SelectOption(label=f"{y}년", value=str(y))
        for y in range(current, current - 25, -1)
    ]


def _month_options() -> list[discord.SelectOption]:
    return [discord.SelectOption(label=f"{m}월", value=str(m)) for m in range(1, 13)]


class HospitalChooseView(discord.ui.View):
    """병원을 **선택**(드롭다운)한다. 선택 후 다음 단계로 이어진다."""

    def __init__(
        self,
        bot: MindFinderBot,
        service: VerificationService,
        mode: str,
        hospitals: Optional[list] = None,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.service = service
        self.mode = mode
        # 주어진 목록이 있으면 그것만, 없으면 load() 에서 전체 병원을 채운다.
        self.hospitals: list = list(hospitals) if hospitals else []

    async def load(self) -> "HospitalChooseView":
        if not self.hospitals:
            self.hospitals = list(await self.bot.hospitals.list_all())
        options = [
            discord.SelectOption(
                label=hospital_display_name(h.name)[:100], value=str(h.id)
            )
            for h in self.hospitals[:25]
        ]
        if not options:
            options = [
                discord.SelectOption(label="등록된 병원이 없습니다", value="0")
            ]
        select: discord.ui.Select = discord.ui.Select(
            placeholder="병원을 선택하세요", options=options
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)
        return self

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values or values[0] == "0":
            await interaction.response.send_message(
                "선택할 병원이 없습니다. ‘기타 병원 직접 입력’으로 요청해 주세요.",
                ephemeral=True,
            )
            return
        hospital_id = int(values[0])
        hospital = await self.bot.hospitals.get_by_id(hospital_id)
        if hospital is None:
            await interaction.response.send_message(
                "병원을 찾을 수 없습니다.", ephemeral=True
            )
            return
        if self.mode == "verify":
            await interaction.response.send_modal(
                UnifiedVerifyModal(self.bot, self.service, hospital_id)
            )
            return
        if self.mode == "knowledge":
            # 지식기반 인증: 먼저 **입원 병원·기간**을 입력받고, 그 뒤에 질문을 낸다.
            await interaction.response.send_message(
                f"**{hospital_display_name(hospital.name)}**\n"
                "지식기반 인증을 진행할 **입원 기간**을 선택하세요.",
                view=PeriodPickerView(self.bot, self.service, hospital_id, "knowledge"),
                ephemeral=True,
            )
            return
        if self.mode == "other":
            # 기타 방법 인증은 기간을 다시 묻지 않는다 (서류 인증 시 이미 등록됨).
            # 바로 '기타 인증 방법'만 입력받는다.
            await interaction.response.send_modal(
                OtherVerifyReasonModal(
                    self.bot,
                    self.service,
                    hospital_id=hospital_id,
                    start_year=None,
                    start_month=None,
                    end_year=None,
                    end_month=None,
                    is_ongoing=False,
                )
            )
            return
        await interaction.response.send_message(
            f"**{hospital_display_name(hospital.name)}** 입원 기간을 선택하세요.",
            view=PeriodPickerView(self.bot, self.service, hospital_id, self.mode),
            ephemeral=True,
        )


class PeriodPickerView(discord.ui.View):
    """연/월 드롭다운(Month Picker)으로 입원 기간을 고른다 (타이핑 없음).

    mode: admission(이력 등록) | other(기타 방법) | knowledge(지식기반)
    """

    def __init__(
        self,
        bot: MindFinderBot,
        service: VerificationService,
        hospital_id: int,
        mode: str = "admission",
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.service = service
        self.hospital_id = hospital_id
        self.mode = mode

        self.start_year = discord.ui.Select(
            placeholder="입원 연도", options=_year_options()
        )
        self.start_month = discord.ui.Select(
            placeholder="입원 월", options=_month_options()
        )
        # 퇴원은 '입원 중'일 수 있으므로 선택 안 해도 되게 한다 (min_values=0).
        self.end_year = discord.ui.Select(
            placeholder="퇴원 연도 (입원 중이면 비워두세요)",
            options=_year_options(),
            min_values=0,
        )
        self.end_month = discord.ui.Select(
            placeholder="퇴원 월 (입원 중이면 비워두세요)",
            options=_month_options(),
            min_values=0,
        )
        for item in (self.start_year, self.start_month, self.end_year, self.end_month):
            item.callback = self._ack  # type: ignore[method-assign]
            self.add_item(item)

    async def _ack(self, interaction: discord.Interaction) -> None:
        """드롭다운 선택을 조용히 접수한다 (실패 표시 방지)."""
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            pass

    @discord.ui.button(label="이대로 등록", style=discord.ButtonStyle.success, row=4)
    async def submit(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        def _pick(select: discord.ui.Select) -> Optional[int]:
            return int(select.values[0]) if select.values else None

        start_year = _pick(self.start_year)
        start_month = _pick(self.start_month)
        if start_year is None or start_month is None:
            await interaction.response.send_message(
                "⚠️ 입원 연도와 월을 선택해주세요.", ephemeral=True
            )
            return
        end_year = _pick(self.end_year)
        end_month = _pick(self.end_month)
        is_ongoing = end_year is None and end_month is None
        if not is_ongoing and (end_year is None or end_month is None):
            await interaction.response.send_message(
                "⚠️ 퇴원 연도와 월을 모두 선택하거나, 둘 다 비워주세요.", ephemeral=True
            )
            return

        hospital = await self.bot.hospitals.get_by_id(self.hospital_id)
        if hospital is None:
            await interaction.response.send_message(
                "병원을 찾을 수 없습니다.", ephemeral=True
            )
            return
        # 기타 방법 / 지식기반 인증은 다음 단계로 넘긴다 (이력 등록은 여기서 처리).
        if self.mode == "other":
            await interaction.response.send_modal(
                OtherVerifyReasonModal(
                    self.bot,
                    self.service,
                    hospital_id=self.hospital_id,
                    start_year=start_year,
                    start_month=start_month,
                    end_year=end_year,
                    end_month=end_month,
                    is_ongoing=is_ongoing,
                )
            )
            return
        if self.mode == "knowledge":
            # 지식기반 인증도 **입원 이력을 먼저 등록**한 뒤 질문을 낸다.
            # (인증 후 다시 눌러 또 등록할 수 있다 — 이력이 여러 개 쌓여도 무방)
            try:
                await self.service.register_admission(
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
            if interaction.guild is not None:
                member = interaction.guild.get_member(interaction.user.id)
                if member is not None:
                    try:
                        await self.bot.access.grant_tier(
                            interaction.guild, member, 1
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "1단계 역할 부여 실패: user=%s", interaction.user.id
                        )
            await interaction.response.edit_message(
                content=(
                    "✅ 입원 기간이 등록되었습니다. 아래에서 **지식기반 인증 질문**을 "
                    "확인해 답변해주세요."
                ),
                view=None,
            )
            await run_knowledge_verification(
                self.bot,
                interaction,
                hospital_id=self.hospital_id,
                start_year=start_year,
                start_month=start_month,
                end_year=end_year,
                end_month=end_month,
                is_ongoing=is_ongoing,
            )
            return
        if self.mode == "knowledge_ask":
            await interaction.response.send_modal(
                KnowledgeQuestionModal(
                    self.bot,
                    hospital_id=self.hospital_id,
                    start_year=start_year,
                    start_month=start_month,
                    end_year=end_year,
                    end_month=end_month,
                    is_ongoing=is_ongoing,
                )
            )
            return

        try:
            _id, period, hospital_name = await self.service.register_admission(
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
                f"**병원**: {hospital_display_name(hospital_name)}\n"
                f"**기간**: {period.text()}\n"
                f"**상태**: {VERIFICATION_LABEL['self_reported']}"
            ),
            color=discord.Color.yellow(),
        )
        embed.set_footer(text="서류 인증을 받으면 병원·기간 커뮤니티와 매칭을 이용할 수 있습니다.")
        await interaction.response.edit_message(content=None, embed=embed, view=None)


# --------------------------------------------------------------------------- #
# 지식기반 인증 (기간 선택 후 질문 풀에서 무작위로 답변)
# --------------------------------------------------------------------------- #
class KnowledgeQuestionModal(discord.ui.Modal, title="지식기반 질문 등록"):
    """서류 인증자(asker)가 병원·기간과 함께 질문을 풀에 등록한다."""

    질문 = discord.ui.TextInput(
        label="질문",
        placeholder="그 시기·병원에 있었던 사람만 아는 질문을 적어주세요.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(
        self,
        bot: MindFinderBot,
        *,
        hospital_id: int,
        start_year: int,
        start_month: int,
        end_year: Optional[int],
        end_month: Optional[int],
        is_ongoing: bool,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.hospital_id = hospital_id
        self.start_year = start_year
        self.start_month = start_month
        self.end_year = end_year
        self.end_month = end_month
        self.is_ongoing = is_ongoing

    async def on_submit(self, interaction: discord.Interaction) -> None:
        def _ym(year: int, month: int) -> int:
            return year * 12 + (month - 1)

        start_ym = _ym(self.start_year, self.start_month)
        end_ym = (
            start_ym
            if self.is_ongoing
            else _ym(self.end_year or self.start_year, self.end_month or 1)
        )
        if end_ym < start_ym:
            end_ym = start_ym
        period_text = (
            f"{self.start_year}년 {self.start_month}월 ~ "
            + ("현재 입원 중" if self.is_ongoing else f"{self.end_year}년 {self.end_month}월")
        )
        hospital_name = await self.bot.knowledge_service.hospital_name(self.hospital_id)
        request_id, message = await self.bot.knowledge_service.submit_question(
            interaction.user.id,
            hospital_id=self.hospital_id,
            admit_ym=start_ym,
            discharge_ym=None if self.is_ongoing else end_ym,
            period_text=f"{hospital_name} {period_text}",
            question=str(self.질문.value or ""),
        )
        if request_id is None:
            await interaction.response.send_message(f"⚠️ {message}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ 질문이 등록되었습니다 (질문번호 `{request_id}`).\n"
            f"**{hospital_display_name(hospital_name)} {period_text}** 기간을 "
            "선택한 사람에게 무작위로 제시됩니다.",
            ephemeral=True,
        )


class KnowledgeAnswerModal(discord.ui.Modal, title="지식기반 인증 답변"):
    """제시된 질문에 답을 타이핑한다."""

    답변 = discord.ui.TextInput(
        label="답변",
        placeholder="기억나는 대로 구체적으로 적어주세요.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(
        self,
        bot: MindFinderBot,
        request_id: int,
        *,
        hospital_id: int,
        start_year: int,
        start_month: int,
        end_year: Optional[int],
        end_month: Optional[int],
        is_ongoing: bool,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.request_id = request_id
        self.hospital_id = hospital_id
        self.start_year = start_year
        self.start_month = start_month
        self.end_year = end_year
        self.end_month = end_month
        self.is_ongoing = is_ongoing

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        req = await self.bot.knowledge_service.answer_to(
            self.request_id, interaction.user.id, str(self.답변.value or "")
        )
        if req is None:
            await interaction.followup.send(
                "⚠️ 이미 처리된 질문입니다.", ephemeral=True
            )
            return

        # 응답자 이력 등록(자기 신고) → 1단계 역할
        try:
            admission_id, period, hospital_name = await (
                self.bot.verification_service.register_admission(
                    user_id=interaction.user.id,
                    hospital_name=await self.bot.knowledge_service.hospital_name(
                        self.hospital_id
                    ),
                    start_year=self.start_year,
                    start_month=self.start_month,
                    end_year=self.end_year,
                    end_month=self.end_month,
                    is_ongoing=self.is_ongoing,
                )
            )
        except PeriodError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        if interaction.guild is not None:
            member = interaction.guild.get_member(interaction.user.id)
            if member is not None:
                try:
                    await self.bot.access.grant_tier(interaction.guild, member, 1)
                except Exception:  # noqa: BLE001
                    logger.exception("1단계 역할 부여 실패: user=%s", interaction.user.id)

        await interaction.followup.send(
            "✅ 답변을 제출했습니다. 질문자가 확인하면 관리자 승인을 거쳐 "
            "인증이 완료됩니다.",
            ephemeral=True,
        )


async def run_knowledge_verification(
    bot: MindFinderBot,
    interaction: discord.Interaction,
    *,
    hospital_id: int,
    start_year: int,
    start_month: int,
    end_year: Optional[int],
    end_month: Optional[int],
    is_ongoing: bool,
) -> None:
    """기간 선택 후, 해당 병원·기간 질문 풀에서 무작위로 하나를 내고 답변받는다."""
    def _ym(year: int, month: int) -> int:
        return year * 12 + (month - 1)

    start_ym = _ym(start_year, start_month)
    end_ym = start_ym if is_ongoing else _ym(end_year or start_year, end_month or 1)
    if end_ym < start_ym:
        end_ym = start_ym
    pool = await bot.knowledge.list_question_pool(hospital_id, start_ym, end_ym)
    pool = [q for q in pool if q.status in ("awaiting_answer", "awaiting_verdict")]
    if not pool:
        await interaction.followup.send(
            "이 병원·기간에 등록된 지식기반 질문이 아직 없습니다. "
            "잠시 후 다시 시도하거나 서류 인증을 이용해주세요.",
            ephemeral=True,
        )
        return
    question = random.choice(pool)
    hospital_name = await bot.knowledge_service.hospital_name(hospital_id)
    embed = discord.Embed(
        title="❓ 지식기반 인증 질문",
        description=(
            f"**병원**: {hospital_display_name(hospital_name)}\n"
            f"**기간**: {question.period_text}\n\n"
            f"**질문**: {question.question}"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="그 시기·병원에 실제로 있었던 사람만 답할 수 있는 질문입니다.")
    await interaction.followup.send(
        embed=embed,
        view=KnowledgeAnswerView(
            bot,
            question.id,
            hospital_id=hospital_id,
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month,
            is_ongoing=is_ongoing,
        ),
        ephemeral=True,
    )


class KnowledgeAnswerView(discord.ui.View):
    """제시된 질문에 답변 버튼을 눌러 답을 작성한다."""

    def __init__(
        self,
        bot: MindFinderBot,
        request_id: int,
        *,
        hospital_id: int,
        start_year: int,
        start_month: int,
        end_year: Optional[int],
        end_month: Optional[int],
        is_ongoing: bool,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.request_id = request_id
        self.kwargs = dict(
            hospital_id=hospital_id,
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month,
            is_ongoing=is_ongoing,
        )

    @discord.ui.button(label="답변 작성", style=discord.ButtonStyle.primary)
    async def answer(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            KnowledgeAnswerModal(self.bot, self.request_id, **self.kwargs)
        )


# --------------------------------------------------------------------------- #
# 모달: 통합 서류 인증 (자료 업로드만) — 병원은 앞 단계 드롭다운에서 선택
# --------------------------------------------------------------------------- #
class UnifiedVerifyModal(discord.ui.Modal, title="서류 인증"):
    """사용자는 **자료 업로드만** 한다.

    · 서울대병원 계열 → 올린 PDF 에서 기간·병원을 자동 추출해 자동 인증
    · 그 외 병원 → 자료만 접수하고, **병원·기간은 관리자가 승인 시 입력**
    """

    def __init__(
        self,
        bot: MindFinderBot,
        service: VerificationService,
        hospital_id: int,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.service = service
        self.hospital_id = hospital_id
        self.서류 = discord.ui.FileUpload(
            custom_id="mf:verify:pdf", required=True, max_values=1
        )
        self.add_item(
            discord.ui.Label(
                text="입퇴원 증빙 서류 (PDF)",
                description="PDF 파일을 업로드하세요. 기간·병원은 서류/관리자가 확인합니다.",
                component=self.서류,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        hospital = await self.bot.hospitals.get_by_id(self.hospital_id)
        if hospital is None:
            await interaction.response.send_message(
                "⚠️ 병원을 찾을 수 없습니다.", ephemeral=True
            )
            return

        attachments = list(getattr(self.서류, "values", []) or [])
        attachment = attachments[0] if attachments else None
        if attachment is None:
            await interaction.response.send_message(
                "⚠️ 증빙 서류(PDF)를 업로드해주세요.", ephemeral=True
            )
            return
        if not attachment.filename.lower().endswith(".pdf"):
            await interaction.response.send_message(
                "⚠️ PDF 파일만 제출할 수 있습니다.", ephemeral=True
            )
            return

        if await self.service.supports_auto_verify(hospital):
            await self._submit_auto(interaction, hospital, attachment)
            return
        await self._submit_manual(interaction, hospital, attachment)

    async def _submit_auto(
        self,
        interaction: discord.Interaction,
        hospital,
        attachment,
    ) -> None:
        """서울대병원 계열: PDF 에서 기간을 추출해 자동 진위확인 (사용자 입력 없음)."""
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
            hospital_id=self.hospital_id,
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

    async def _submit_manual(
        self, interaction: discord.Interaction, hospital, attachment
    ) -> None:
        """그 외 병원: 자료만 접수하고 병원·기간은 관리자가 확정한다."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        request_id = await self.service.submit_document_upload(
            interaction.user.id,
            hospital_name=hospital.name,
            method="document",
        )

        # 관리자 심사 채널에 자료(첨부)와 심사 버튼을 게시한다.
        posted = False
        try:
            data = await attachment.read()
        except discord.HTTPException:
            data = None
        if data is not None:
            try:
                await notify_admins_of_document_request(
                    self.bot,
                    request_id=request_id,
                    user_id=interaction.user.id,
                    hospital_name=hospital.name,
                    filename=attachment.filename,
                    data=data,
                )
                posted = True
            except Exception:  # noqa: BLE001
                logger.exception("수동 심사 접수 알림 실패: request=%s", request_id)

        embed = discord.Embed(
            title="📄 서류 심사가 접수되었습니다",
            description=(
                f"**병원(선택)**: {hospital_display_name(hospital.name)}\n"
                f"**심사번호**: `{request_id}`\n\n"
                "제출한 자료를 관리자가 확인한 뒤, **병원·기간을 입력해** "
                "인증을 완료합니다. 별도로 입력하실 내용은 없습니다."
            ),
            color=discord.Color.orange(),
        )
        if not posted:
            settings = self.bot.settings
            hint = (
                f"<#{settings.verification_channel_id}>"
                if settings.verification_channel_id
                else "관리자 심사 채널"
            )
            embed.add_field(
                name="안내",
                value=f"{hint} 에 `심사번호: {request_id}` 와 함께 자료를 첨부해주세요.",
                inline=False,
            )
        embed.set_footer(
            text="서류 원본은 저장하지 않으며, 심사 완료 후 삭제됩니다 (지문만 보관)."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# 모달: 기타 방법 인증 (병원·기간은 앞 단계에서 선택, 방법만 타이핑)
# --------------------------------------------------------------------------- #
class OtherVerifyReasonModal(discord.ui.Modal, title="기타 방법 인증"):
    """병원·기간은 앞 단계에서 고르고, **기타 인증 방법만** 타이핑한다."""

    인증방법 = discord.ui.TextInput(
        label="기타 인증 방법",
        placeholder="예: 진료기록 사본, 보호자 확인, 병원 발급 서류 사진 등",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500,
    )

    def __init__(
        self,
        bot: MindFinderBot,
        service: VerificationService,
        *,
        hospital_id: int,
        start_year: Optional[int] = None,
        start_month: Optional[int] = None,
        end_year: Optional[int] = None,
        end_month: Optional[int] = None,
        is_ongoing: bool = False,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.service = service
        self.hospital_id = hospital_id
        self.start_year = start_year
        self.start_month = start_month
        self.end_year = end_year
        self.end_month = end_month
        self.is_ongoing = is_ongoing

    async def on_submit(self, interaction: discord.Interaction) -> None:
        hospital = await self.bot.hospitals.get_by_id(self.hospital_id)
        if hospital is None:
            await interaction.response.send_message(
                "⚠️ 병원을 찾을 수 없습니다.", ephemeral=True
            )
            return

        reason = str(self.인증방법.value or "").strip()
        await interaction.response.defer(ephemeral=True, thinking=True)

        # 기간이 주어지지 않았으면, 이 병원에 이미 등록된(서류 인증 등) 이력을 쓴다.
        # 서류 인증 때 기간이 이미 등록되므로 여기서 다시 묻지 않는다.
        admission_id: Optional[int] = None
        period = None
        hospital_name = hospital.name
        if self.start_year is None or self.start_month is None:
            existing = [
                a
                for a in await self.bot.admissions.list_by_user(interaction.user.id)
                if a.hospital_id == hospital.id
            ]
            if existing:
                admission_id = existing[0].id
                period = None
        else:
            try:
                admission_id, period, hospital_name = (
                    await self.service.register_admission(
                        user_id=interaction.user.id,
                        hospital_name=hospital.name,
                        start_year=self.start_year,
                        start_month=self.start_month,
                        end_year=self.end_year,
                        end_month=self.end_month,
                        is_ongoing=self.is_ongoing,
                    )
                )
            except PeriodError as exc:
                await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
                return

        if admission_id is None:
            await interaction.followup.send(
                "⚠️ 먼저 **서류 인증** 으로 입원 이력을 등록하거나, "
                "**입원 이력 등록**을 완료해주세요.",
                ephemeral=True,
            )
            return

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
                await self.bot.channel_service.ensure_hospital_channel(
                    interaction.guild, hospital.id, hospital.name, with_role=False
                )
            except Exception:  # noqa: BLE001
                logger.exception("병원 채널 확보 실패: hospital=%s", hospital.id)

        # 기간은 선택 입력이므로 없으면 기존 이력의 기간을 사용한다.
        period_text = period.text() if period is not None else "(등록된 이력 기간)"
        if period is None:
            adm = await self.bot.admissions.get_by_id(admission_id)
            if adm is not None:
                period_text = adm.period_text()

        await notify_admins_of_other_request(
            self.bot,
            request_id=request_id,
            user_id=interaction.user.id,
            hospital_name=hospital_name,
            period_text=period_text,
            reason=reason,
        )

        embed = discord.Embed(
            title="🟠 기타 방법 인증이 접수되었습니다",
            description=(
                f"**병원**: {hospital_display_name(hospital_name)}\n"
                f"**기간**: {period_text}\n"
                f"**심사번호**: `{request_id}`\n\n"
                "**관리자가 확인 후 연락드리겠습니다.**"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="서류·지식기반 인증이 어려울 때 사용하는 대체 경로입니다.")
        await interaction.followup.send(embed=embed, ephemeral=True)


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
            msg = await channel.send(
                f"🟠 **기타 방법 인증 신청** (`{request_id}`)\n"
                f"· 신청자: <@{user_id}>\n"
                f"· 병원: **{hospital_display_name(hospital_name)}**\n"
                f"· 기간: {period_text}\n"
                f"· 사유: {reason[:300]}\n"
                "확인 후 승인하면 2단계 인증이 부여됩니다.",
                view=view,
            )
            await bot.verifications.set_proof_message(request_id, channel.id, msg.id)
        except discord.HTTPException:
            logger.exception("기타 인증 알림 실패: request=%s", request_id)
        return


async def notify_admins_of_document_request(
    bot: MindFinderBot,
    *,
    request_id: int,
    user_id: int,
    hospital_name: str,
    filename: str,
    data: bytes,
) -> None:
    """관리자 채널에 수동 서류 심사(자료 업로드)를 게시한다."""
    for guild in bot.guilds:
        channel = await bot.channel_service.managed_text_channel(
            guild, ADMIN_CHANNEL_KEY
        )
        if channel is None:
            continue
        view = DocumentReviewView(bot, request_id)
        try:
            file = discord.File(io.BytesIO(data), filename=filename or "서류.pdf")
            msg = await channel.send(
                content=(
                    f"📄 **서류 심사 접수** (`{request_id}`)\n"
                    f"· 제출자: <@{user_id}>\n"
                    f"· 선택 병원: {hospital_display_name(hospital_name)}\n"
                    "· 승인 시 **병원·기간을 입력**해야 이력이 생성됩니다."
                ),
                file=file,
                view=view,
            )
            # 승인/거절 즉시 이 메시지(자료)를 삭제할 수 있도록 기록한다.
            await bot.verifications.set_proof_message(request_id, channel.id, msg.id)
        except discord.HTTPException:
            logger.exception("서류 심사 알림 실패: request=%s", request_id)
        return


async def delete_review_message(bot: MindFinderBot, request_id: int) -> None:
    """심사 요청의 관리자 검토 메시지(자료 포함)를 삭제한다."""
    req = await bot.verifications.get_by_id(request_id)
    if req is None or not req.proof_channel_id or not req.proof_message_id:
        return
    channel = bot.get_channel(req.proof_channel_id)
    if isinstance(channel, discord.TextChannel):
        try:
            msg = await channel.fetch_message(req.proof_message_id)
            await msg.delete()
        except discord.HTTPException:
            logger.debug("심사 메시지 삭제 실패: %s", req.proof_message_id)


# --------------------------------------------------------------------------- #
# 모달: 기타 병원 직접 입력 (관리자 승인 요청)
# --------------------------------------------------------------------------- #
class HospitalRequestModal(discord.ui.Modal, title="기타 병원 추가 요청"):
    """목록에 없는 병원을 직접 입력해 등록을 요청한다 (관리자 승인 후 추가)."""

    병원 = discord.ui.TextInput(
        label="병원 이름", placeholder="예: 국립정신건강센터", max_length=100
    )
    병동 = discord.ui.TextInput(
        label="병동", placeholder="예: 7A병동 / 62병동", max_length=50
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
        # 병동은 필수 입력이다 (공백만 입력한 경우도 막는다).
        if not str(self.병동.value or "").strip():
            await interaction.response.send_message(
                "⚠️ 병동을 입력해주세요.", ephemeral=True
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
                # 역할은 사용자가 커뮤니티 패널에서 선택할 때만 만든다.
                await self.bot.channel_service.ensure_hospital_channel(
                    interaction.guild, hospital.id, hospital.name, with_role=False
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

        # 2단계 역할 + 병원 역할/카테고리 + 기간 채널 부여
        if interaction.guild is not None:
            shim = _ResultShim(hospital_name) if hospital_name else None
            await grant_verified_access(
                self.bot, interaction.guild, req.user_id, shim
            )

        await interaction.response.send_message(
            "✅ 기타 방법 인증을 승인했습니다.", ephemeral=True
        )
        # 승인 즉시 관리자 검토 메시지(자료)를 삭제한다.
        await delete_review_message(self.bot, self.request_id)
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
        # 거절 즉시 관리자 검토 메시지(자료)를 삭제한다.
        await delete_review_message(self.bot, self.request_id)
        try:
            user = await self.bot.fetch_user(req.user_id)
            await user.send(
                "🔴 기타 방법 인증이 **거절**되었습니다. "
                "서류 인증 또는 지식기반 인증을 시도해보세요."
            )
        except Exception:  # noqa: BLE001
            logger.debug("기타 인증 거절 DM 실패: user=%s", req.user_id)


class ManualApproveModal(discord.ui.Modal, title="수동 인증 승인 (병원·기간 입력)"):
    """관리자가 병원·기간을 입력해 수동 심사를 승인한다."""

    병원 = discord.ui.TextInput(
        label="병원 이름", placeholder="예: 서울대학교어린이병원", max_length=100
    )
    병동 = discord.ui.TextInput(
        label="병동 (선택)", placeholder="예: 7A병동", required=False, max_length=50
    )
    입원기간 = discord.ui.TextInput(
        label="입원 기간",
        placeholder="예: 2020-03 ~ 2020-06 · 입원 중이면 시작만",
        required=False,
        max_length=40,
    )

    def __init__(self, bot: MindFinderBot, request_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        service = VerificationService(self.bot)
        try:
            start_year, start_month, end_year, end_month = parse_period_input(
                str(self.입원기간.value or "")
            )
        except PeriodError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        is_ongoing = end_year is None and end_month is None
        hospital_name = str(self.병원.value or "").strip()
        ward = str(self.병동.value or "").strip() or None

        req = await service.approve_manual(
            self.request_id,
            interaction.user.id,
            hospital_name=hospital_name,
            ward=ward,
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month,
            is_ongoing=is_ongoing,
        )
        if req is None:
            await interaction.response.send_message(
                "이미 처리된 요청입니다.", ephemeral=True
            )
            return

        # 2단계 역할 + 병원 포럼 확보 (병원 역할은 사용자 선택 시에만)
        if interaction.guild is not None:
            admissions = await self.bot.admissions.list_by_user(req.user_id)
            result = next(
                (a for a in admissions if a.id == req.admission_id),
                _ResultShim(hospital_name),
            )
            try:
                await grant_verified_access(
                    self.bot, interaction.guild, req.user_id, result
                )
            except Exception:  # noqa: BLE001
                logger.exception("수동 승인 역할 부여 실패: user=%s", req.user_id)

        display = f"{hospital_name} {ward}".strip() if ward else hospital_name
        await interaction.response.send_message(
            f"✅ 심사 #{self.request_id} 승인 — **{display}** 이력이 생성되었습니다.",
            ephemeral=True,
        )
        # 승인 즉시 관리자 검토 메시지(자료)를 삭제한다.
        await delete_review_message(self.bot, self.request_id)


class DocumentReviewView(discord.ui.View):
    """관리자가 수동 서류 심사를 승인/거절하는 영구 뷰 (요청별 custom_id)."""

    def __init__(self, bot: MindFinderBot, request_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id
        approve = discord.ui.Button(
            label="승인 (병원·기간 입력)",
            style=discord.ButtonStyle.success,
            custom_id=f"mf:dre:approve:{request_id}",
        )
        reject = discord.ui.Button(
            label="거절", style=discord.ButtonStyle.danger,
            custom_id=f"mf:dre:reject:{request_id}",
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
        await interaction.response.send_modal(
            ManualApproveModal(self.bot, self.request_id)
        )

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
        await interaction.response.send_message("서류 심사를 거절했습니다.", ephemeral=True)
        # 거절 즉시 관리자 검토 메시지(자료)를 삭제한다.
        await delete_review_message(self.bot, self.request_id)


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
class KnowledgeAskModal(discord.ui.Modal, title="지식기반 인증하기"):
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
            "✅ 질문을 보냈습니다. 상대가 답변하면 '지식기반 인증 현황'에서 "
            "판정할 수 있습니다.",
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
    """인증 전용 패널: 버튼 대신 하나의 드롭다운(선택)으로 모든 기능을 제공한다."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.vservice = VerificationService(bot)
        select: discord.ui.Select = discord.ui.Select(
            placeholder="무엇을 할까요? (인증 메뉴)",
            custom_id="mf:v_action",
            options=[
                discord.SelectOption(
                    label="입원 이력 등록", value="admission", emoji="🏥",
                    description="병원·기간을 입력해 입원 이력을 등록(임시인증)",
                ),
                discord.SelectOption(
                    label="서류 인증", value="verify", emoji="📄",
                    description="병원을 고른 뒤 서류·기간으로 2단계 인증",
                ),
                discord.SelectOption(
                    label="지식기반 인증", value="knowledge", emoji="🧠",
                    description="병원·시기를 고르고 그 시기 경험 질문에 답해 인증",
                ),
                discord.SelectOption(
                    label="기타 방법 인증", value="other", emoji="✍️",
                    description="병원·기간 선택 후 기타 방법으로 관리자 승인 요청",
                ),
                discord.SelectOption(
                    label="기타 병원 직접 입력", value="request", emoji="🏥",
                    description="목록에 없는 병원을 직접 입력해 등록 요청",
                ),
            ],
        )
        select.callback = self._on_action  # type: ignore[method-assign]
        self.add_item(select)

    async def _on_action(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            await interaction.response.send_message(
                "선택이 취소되었습니다.", ephemeral=True
            )
            return
        handler = {
            "admission": self._do_admission,
            "verify": self._do_verify,
            "knowledge": self._do_knowledge,
            "other": self._do_other,
            "request": self._do_request,
        }.get(values[0])
        if handler is None:
            await interaction.response.send_message(
                "알 수 없는 선택입니다.", ephemeral=True
            )
            return
        await handler(interaction)

    async def _do_admission(self, interaction: discord.Interaction) -> None:
        """입원 이력을 자기 신고로 등록한다 (병원·기간 선택 → 임시인증)."""
        async def _proceed(inner: discord.Interaction) -> None:
            view = await HospitalChooseView(
                self.bot, self.vservice, "admission"
            ).load()
            await inner.response.send_message(
                "입원 이력을 등록할 **병원을 선택**하세요.",
                view=view,
                ephemeral=True,
            )

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _do_verify(self, interaction: discord.Interaction) -> None:
        async def _proceed(inner: discord.Interaction) -> None:
            view = await HospitalChooseView(
                self.bot, self.vservice, "verify"
            ).load()
            await inner.response.send_message(
                "인증할 **병원을 선택**하세요.",
                view=view,
                ephemeral=True,
            )

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _do_other(self, interaction: discord.Interaction) -> None:
        async def _proceed(inner: discord.Interaction) -> None:
            view = await HospitalChooseView(
                self.bot, self.vservice, "other"
            ).load()
            await inner.response.send_message(
                "기타 방법 인증을 진행할 **병원을 선택**하세요.",
                view=view,
                ephemeral=True,
            )

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _do_knowledge(self, interaction: discord.Interaction) -> None:
        """서류 인증을 마친 사람이 병원·시기를 골라 지식기반 인증을 진행한다."""
        async def _proceed(inner: discord.Interaction) -> None:
            view = await HospitalChooseView(
                self.bot, self.vservice, "knowledge"
            ).load()
            await inner.response.send_message(
                "지식기반 인증을 진행할 **병원을 선택**하세요.",
                view=view,
                ephemeral=True,
            )

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    async def _do_ask(self, interaction: discord.Interaction) -> None:
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

    async def _do_request(self, interaction: discord.Interaction) -> None:
        """목록에 없는 병원을 직접 입력해 등록을 요청한다 (관리자 승인 후 추가)."""

        async def _proceed(inner: discord.Interaction) -> None:
            await inner.response.send_modal(HospitalRequestModal(self.bot))

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

# --------------------------------------------------------------------------- #
# 매칭 채널 닫기 (동적, 비영구)
# --------------------------------------------------------------------------- #
class MatchChannelCloseView(discord.ui.View):
    """내가 참여 중인 매칭 채널 중 하나를 골라 닫는다."""

    def __init__(
        self, bot: MindFinderBot, user_id: int, channels: list
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        options = [
            discord.SelectOption(
                label=(ch.name or "매칭 채널")[:100],
                value=str(ch.id),
                description="이 채널을 닫습니다 (기록 보존)",
            )
            for ch in channels[:25]
        ]
        select: discord.ui.Select = discord.ui.Select(
            placeholder="닫을 매칭 채널을 선택하세요", options=options
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "본인의 매칭 채널만 닫을 수 있습니다.", ephemeral=True
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            await interaction.response.send_message(
                "선택이 취소되었습니다.", ephemeral=True
            )
            return
        channel = interaction.guild.get_channel(int(values[0]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "채널을 찾을 수 없습니다.", ephemeral=True
            )
            return
        ok = await self.bot.matching_service.close_channel(
            interaction.guild, interaction.user.id, channel
        )
        if not ok:
            await interaction.response.send_message(
                "이 매칭 채널의 참여자만 닫을 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🔒 {channel.mention} 매칭 채널을 닫았습니다. (기록은 보존됩니다.)",
            ephemeral=True,
        )


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
            display = await self.mservice.display_name(interaction.guild, cand.user_id)
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

    @discord.ui.button(
        label="매칭 표시명 설정", style=discord.ButtonStyle.secondary,
        custom_id="mf:m_matchname", row=3,
    )
    async def match_name(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """매칭 시 상대에게 보일 **표시명 방식**을 설정한다."""
        mode, nickname = await self.bot.display_prefs.get_match_name(
            interaction.user.id
        )
        extra = f" (현재 닉네임: {nickname})" if nickname else ""
        await interaction.response.send_message(
            "**매칭할 때 상대에게 보일 이름**을 선택하세요.\n"
            "· 기본 마스킹 — 디스코드 표시명 가운데만 가림\n"
            "· 실명 — 디스코드 표시명 그대로\n"
            "· **병원에서 불린 닉네임(추천)** — 입원 당시 불리던 이름"
            + extra,
            view=MatchNameView(self.bot, mode),
            ephemeral=True,
        )

    @discord.ui.button(
        label="매칭 채널 닫기", style=discord.ButtonStyle.danger,
        custom_id="mf:m_close", row=4,
    )
    async def close_channel(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """내가 참여 중인 매칭 채널을 골라 닫는다 (읽기 전용 전환, 기록 보존)."""
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
                "닫을 매칭 채널이 없습니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "닫을 **매칭 채널을 선택**하세요. (기록은 보존되고 채팅만 막힙니다.)",
            view=MatchChannelCloseView(self.bot, interaction.user.id, chans),
            ephemeral=True,
        )


# --------------------------------------------------------------------------- #
# 커뮤니티 패널 (영구 뷰) — 💬-전체-커뮤니티 채널
# --------------------------------------------------------------------------- #
class DisplayScopeView(discord.ui.View):
    """내 입원 정보(역할태그/프로필)를 **어디까지 노출할지** 선택한다."""

    # (값, 라벨, 설명)
    _OPTIONS = [
        ("period", "병원 + 기간까지", "내가 입원했던 병원과 기간을 함께 표시"),
        ("hospital", "병원만", "입원했던 병원만 표시 (기간은 비공개)"),
        ("none", "아무것도 안 보이게 (비공개)", "입원 정보를 표시하지 않음"),
    ]

    def __init__(self, bot: MindFinderBot, current: str) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=label,
                value=value,
                description=description,
                default=(value == current),
            )
            for value, label, description in self._OPTIONS
        ]
        select: discord.ui.Select = discord.ui.Select(
            placeholder="내 입원 정보 공개 범위를 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            await interaction.response.send_message(
                "선택이 취소되었습니다.", ephemeral=True
            )
            return
        scope = values[0]
        await self.bot.display_prefs.set_scope(interaction.user.id, scope)
        label = next(
            (lbl for val, lbl, _ in self._OPTIONS if val == scope), scope
        )
        await interaction.response.send_message(
            f"✅ 공개 범위를 **{label}** 로 설정했습니다.",
            ephemeral=True,
        )


class MatchNameModal(discord.ui.Modal, title="매칭 표시명 (병원 닉네임)"):
    """병원에서 불린 닉네임을 입력받는다."""

    닉네임 = discord.ui.TextInput(
        label="병원에서 불린 닉네임",
        placeholder="예: 7A 별명, 병동에서 불리던 이름",
        required=True,
        max_length=50,
    )

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        nickname = str(self.닉네임.value or "").strip()
        if not nickname:
            await interaction.response.send_message(
                "⚠️ 닉네임을 입력해주세요.", ephemeral=True
            )
            return
        await self.bot.display_prefs.set_match_name(
            interaction.user.id, "nickname", nickname
        )
        await interaction.response.send_message(
            f"✅ 매칭 표시명을 **병원 닉네임({nickname})** 으로 설정했습니다.",
            ephemeral=True,
        )


class MatchNameView(discord.ui.View):
    """매칭 시 상대에게 보일 **표시명 방식**을 고른다."""

    _OPTIONS = [
        (
            "masked",
            "기본 마스킹 (예: 홍*동)",
            "디스코드 표시명을 가운데만 가려서 보여줍니다.",
        ),
        (
            "real",
            "실명 (디스코드 표시명 그대로)",
            "디스코드에 설정된 이름을 그대로 보여줍니다.",
        ),
        (
            "nickname",
            "병원에서 불린 닉네임 (추천)",
            "입원 당시 병동에서 불리던 이름을 직접 입력합니다.",
        ),
    ]

    def __init__(self, bot: MindFinderBot, current: str) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=label, value=value, description=desc,
                default=(value == current),
            )
            for value, label, desc in self._OPTIONS
        ]
        select: discord.ui.Select = discord.ui.Select(
            placeholder="매칭 표시명 방식을 선택하세요", options=options
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            await interaction.response.send_message(
                "선택이 취소되었습니다.", ephemeral=True
            )
            return
        mode = values[0]
        if mode == "nickname":
            # 닉네임은 모달로 직접 입력받는다.
            await interaction.response.send_modal(MatchNameModal(self.bot))
            return
        await self.bot.display_prefs.set_match_name(interaction.user.id, mode)
        label = next(
            (lbl for val, lbl, _ in self._OPTIONS if val == mode), mode
        )
        await interaction.response.send_message(
            f"✅ 매칭 표시명을 **{label}** 로 설정했습니다.", ephemeral=True
        )


class HospitalRoleSelectView(discord.ui.View):
    """인증받은 병원 중 **역할(태그)을 표시할 병원**을 사용자가 선택/해제한다.

    병원 역할 태그는 자동으로 달지 않는다. 선택 상태는 DB(hospital_optins)에
    사람별로 저장하고, 선택한 병원만 역할이 부여되어 기간 채널이 보인다.
    """

    def __init__(self, bot: MindFinderBot, hospitals: list, opted_in: set) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.hospitals = hospitals
        options = [
            discord.SelectOption(
                label=hospital_display_name(h.name)[:100],
                value=str(h.id),
                description=("표시 중 (해제하려면 다시 선택)" if h.id in opted_in
                             else "선택 시 역할 태그 표시 + 기간 채널 열림"),
                default=(h.id in opted_in),
            )
            for h in hospitals[:25]
        ]
        select: discord.ui.Select = discord.ui.Select(
            placeholder="역할 태그를 표시할 병원을 선택하세요",
            options=options,
            min_values=0,
            max_values=min(len(options), 25),
        )
        select.callback = self._on_select  # type: ignore[method-assign]
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버에서만 사용할 수 있습니다.", ephemeral=True
            )
            return
        values = {int(v) for v in (interaction.data.get("values", []) if interaction.data else [])}
        await interaction.response.defer(ephemeral=True, thinking=True)
        member = interaction.guild.get_member(interaction.user.id)
        changed: list[str] = []
        for hospital in self.hospitals:
            want = hospital.id in values
            already = await self.bot.access.is_hospital_optin(
                interaction.user.id, hospital.id
            )
            if want == already:
                continue
            await self.bot.access.set_hospital_optin(
                interaction.user.id, hospital.id, want
            )
            if member is not None:
                if want:
                    await self.bot.access.grant_hospital_role(
                        interaction.guild, member, hospital.id, hospital.name
                    )
                else:
                    await self.bot.access.revoke_hospital_role(
                        interaction.guild, member, hospital.id
                    )
            changed.append(
                f"{hospital_display_name(hospital.name)} → "
                + ("표시" if want else "숨김")
            )
        # 선택에 따라 기간 채널 접근을 동기화한다.
        try:
            await self.bot.channel_service.ensure_community_access(
                interaction.guild, interaction.user.id
            )
        except Exception:  # noqa: BLE001
            logger.exception("커뮤니티 접근 동기화 실패: user=%s", interaction.user.id)
        msg = "변경된 항목이 없습니다."
        if changed:
            msg = "✅ 역할 태그 설정을 반영했습니다.\n" + "\n".join(changed)
        await interaction.followup.send(msg, ephemeral=True)


class CommunityPanelView(discord.ui.View):
    """커뮤니티 패널: 인증 기반 병원·기간 커뮤니티 / 지식기반 인증 / 내 정보."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="병원 역할 태그 설정", style=discord.ButtonStyle.primary,
        custom_id="mf:c_hospitalrole", row=0,
    )
    async def hospital_role_select(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """인증받은 병원 중 **역할 태그를 표시할 병원**을 직접 선택/해제한다.

        역할 태그는 자동으로 달지 않고, 선택한 병원만 태그가 붙고
        그 병원의 기간 채널이 열린다.
        """
        hospitals = await self._verified_hospitals_for(interaction.user.id)
        if not hospitals:
            await interaction.response.send_message(
                "먼저 **서류 인증 또는 지식기반 인증**을 완료해야 "
                "병원 역할 태그를 선택할 수 있습니다.",
                ephemeral=True,
            )
            return
        opted_in = set(
            await self.bot.access.optedin_hospital_ids(interaction.user.id)
        )
        await interaction.response.send_message(
            "**역할 태그를 표시할 병원**을 선택하세요.\n"
            "· 선택하면 해당 병원 태그가 붙고 그 병원의 기간 채널이 열립니다.\n"
            "· 다시 눌러 선택을 해제하면 태그가 사라집니다.",
            view=HospitalRoleSelectView(self.bot, hospitals, opted_in),
            ephemeral=True,
        )

    async def _verified_hospitals_for(self, user_id: int) -> list:
        """사용자가 서류 인증(verified)을 받은 병원 객체 목록."""
        admissions = await self.bot.admissions.list_by_user(user_id)
        verified_ids = {a.hospital_id for a in admissions if a.verification == "verified"}
        hospitals = []
        for hospital in await self.bot.hospitals.list_all():
            if hospital.id in verified_ids:
                hospitals.append(hospital)
        return hospitals

    # 주의: '지식기반 인증'(질문에 답해 인증)은 **인증탭**에서만 진행한다.
    # 커뮤니티 패널에는 '질문 등록'(내가 질문을 풀에 올림)만 둔다.

    @discord.ui.button(
        label="지식기반 질문 등록", style=discord.ButtonStyle.success,
        custom_id="mf:c_kask", row=1,
    )
    async def knowledge_ask(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """서류 인증자가 본인 병원·기간에 대한 질문을 풀에 등록한다."""
        hospitals = await self._verified_hospitals_for(interaction.user.id)
        if not hospitals:
            await interaction.response.send_message(
                "먼저 **서류 인증** 을 완료해야 질문을 등록할 수 있습니다.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "질문을 등록할 **병원을 선택**하세요.",
            view=await HospitalChooseView(
                self.bot, VerificationService(self.bot), "knowledge_ask",
                hospitals=hospitals,
            ).load(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="지식기반 인증 현황", style=discord.ButtonStyle.secondary,
        custom_id="mf:c_kstatus", row=1,
    )
    async def knowledge_status(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """내가 보낸 질문의 응답/판정 현황을 보여준다."""
        sent = await self.bot.knowledge.list_for_asker(interaction.user.id)
        embed = discord.Embed(title="🧠 지식기반 질문 현황", color=discord.Color.blurple())
        if sent:
            lines = [
                f"#{r.id} · {r.period_text} — "
                + {
                    "awaiting_answer": "응답 대기",
                    "awaiting_verdict": "판정 대기",
                    "asker_confirmed": "관리자 승인 대기",
                    "asker_rejected": "오답 처리",
                    "approved": "승인 완료",
                    "rejected": "거절",
                }.get(r.status, r.status)
                for r in sent[:8]
            ]
            embed.add_field(name="내가 등록한 질문", value="\n".join(lines), inline=False)
        else:
            embed.description = "등록한 지식기반 질문이 없습니다."
        await interaction.response.send_message(embed=embed, ephemeral=True)
        to_verdict = await self.bot.knowledge_service.list_for_respondent(
            interaction.user.id, "awaiting_verdict"
        )
        if to_verdict:
            await interaction.followup.send(
                "판정할 질문이 있습니다.",
                view=VerdictView(self.bot, to_verdict[0].id),
                ephemeral=True,
            )

    @discord.ui.button(
        label="내 기간 채널", style=discord.ButtonStyle.primary,
        custom_id="mf:c_threads", row=0,
    )
    async def my_threads(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """내 인증 이력(병원·기간)에 맞는 비공개 월 채널을 확보/표시한다."""
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
        embed = discord.Embed(
            title="🏥 내 병원·기간 커뮤니티",
            description=(
                "**인증(서류·지식)을 마친** 병원의, 내 입원 기간에 해당하는 "
                "**비공개 월 채널**이 자동으로 열립니다.\n"
                "같은 기간에 입원했던 인증자에게만 그 방이 보입니다."
            ),
            color=discord.Color.teal(),
        )
        found = False
        for adm in admissions:
            # 인증(verified)을 받은 이력만 병원·기간 커뮤니티가 열린다.
            if adm.verification != "verified":
                continue
            labels: list[str] = []
            for ym_value in self.bot.channel_service.months_for_admission(adm):
                channel = await self.bot.channel_service.ensure_period_channel(
                    interaction.guild,
                    adm.hospital_id,
                    adm.hospital.name,
                    ym_value,
                    with_role=True,
                )
                if channel is None:
                    continue
                labels.append(channel.mention)
            if labels:
                found = True
                embed.add_field(
                    name=hospital_display_name(adm.hospital.name),
                    value=" · ".join(labels[:25]),
                    inline=False,
                )
        if not found:
            embed.add_field(
                name="표시할 기간 채널 없음",
                value=(
                    "먼저 **서류 인증 또는 지식기반 인증**을 완료하세요. "
                    "인증이 완료되면 해당 병원·기간 채널이 자동으로 열립니다."
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
                "서류 인증을 받으면 **내가 인증받은 병원**의 전용 카테고리가 열립니다.\n"
                "그 안에 **월 단위 비공개 채널**이 생기고, "
                "내 입원 기간에 해당하는 채널에만 참여됩니다."
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
            "🧩 매칭은 **인증됨(서류·지식 인증)** 사용자만 이용할 수 있습니다.\n"
            "매칭 패널에서 '매칭 희망 켜기' → '동료 찾기' 로 신청하세요.\n"
            "연락처는 봇이 저장하지 않으며, 매칭 성사 시 비공개 채널에서 직접 대화합니다.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="공개 범위 설정", style=discord.ButtonStyle.secondary,
        custom_id="mf:c_display", row=2,
    )
    async def display_scope(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        """내 입원 정보(역할태그·프로필)를 어디까지 노출할지 선택한다."""
        current = await self.bot.display_prefs.get_scope(interaction.user.id)
        await interaction.response.send_message(
            "내 입원 정보를 **어디까지 공개**할지 선택하세요.\n"
            "(병원만 / 기간까지 / 아무것도 안 보이게)",
            view=DisplayScopeView(self.bot, current),
            ephemeral=True,
        )

# --------------------------------------------------------------------------- #
# 안내 패널 (영구 뷰) — 🧭-패널 채널
# --------------------------------------------------------------------------- #
class HelpPanelView(discord.ui.View):
    """안내·도움말 패널: 사용법, 이용약관·개인정보처리방침."""

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
                "**입원 이력 등록** — 병원·기간을 입력해 임시인증(자기 신고)\n"
                "**서류 인증** — 병원 선택 후 서류로 2단계 인증\n"
                "**기타 방법 인증** — 서류 외 방법으로 관리자 승인 요청\n"
                "**기타 병원 직접 입력** — 목록에 없는 병원 등록 요청"
            ),
            inline=False,
        )
        embed.add_field(
            name="💬 커뮤니티·지식",
            value=(
                "**내 기간 채널** — 내 인증 기간 커뮤니티 확인\n"
                "**지식기반 인증하기** — 동료에게 병동 경험 질문\n"
                "**지식기반 인증 현황** — 보낸 질문 응답/판정 확인\n"
                "**공개 범위 설정** — 병원만/기간까지/비공개"
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
        label="이용약관·개인정보처리방침", style=discord.ButtonStyle.secondary,
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
            1: "임시인증 (자기 신고) — 전체 커뮤니티 이용 가능",
            2: "인증됨 (서류·지식 인증) — 병원 커뮤니티·매칭 이용 가능",
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
        match_lines.append("사용 가능" if can_match else "인증됨(2단계) 후 사용 가능")
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

        # 참여 중인 병원·기간 커뮤니티(채널) 목록 — 병원별로 묶어서 보여준다.
        if interaction.guild is not None and admissions:
            by_hospital: dict[str, list[str]] = {}
            for adm in admissions:
                if adm.verification != "verified":
                    continue
                # 정신과 입원이 아닌 이력(예: 응급실 단순 관찰)은 커뮤니티 대상이 아니다.
                if not getattr(adm, "is_community_eligible", True):
                    continue
                label = hospital_display_name(adm.hospital.name)
                names = [
                    format_ym(ym_value)
                    for ym_value in bot.channel_service.months_for_admission(adm)
                ]
                if names:
                    by_hospital.setdefault(label, []).extend(names)
            thread_lines = []
            for label, names in by_hospital.items():
                # 중복 월은 제거하고 정렬한다.
                unique = sorted(set(names))
                thread_lines.append(f"· {label}: " + ", ".join(unique[:12]))
            if thread_lines:
                embed.add_field(
                    name="참여 중인 병원·기간 커뮤니티",
                    value="\n".join(thread_lines)[:1024],
                    inline=False,
                )

        # 입원 정보 공개 범위
        scope = await bot.display_prefs.get_scope(interaction.user.id)
        scope_label = {
            "none": "비공개 (아무것도 안 보이게)",
            "hospital": "병원만",
            "period": "병원 + 기간까지",
        }.get(scope, scope)
        embed.add_field(name="공개 범위", value=scope_label, inline=False)
        embed.set_footer(text="각 기능은 해당 채널의 패널 버튼에서 사용하세요.")
        await interaction.followup.send(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------- #
# 관리자 패널 (영구 뷰) — 🛠-관리자 채널
# --------------------------------------------------------------------------- #
# 관리자 기능 드롭다운 옵션 (value → 표시)
_ADMIN_OPTIONS: list[tuple[str, str, str]] = [
    ("hospitals", "병원 추가 요청 검토", "🏥"),
    ("hospitals_list", "병원 목록·삭제", "🗂️"),
    ("knowledge", "지식기반 인증 심사", "🧠"),
    ("reviews", "서류·기타 심사 목록", "📄"),
    ("stats", "운영 통계", "📊"),
    ("terms", "약관 재게시", "📜"),
    ("channels", "채널 재구성", "🧭"),
    ("maintain", "역할·채널 재정비", "🧹"),
    ("names", "채널 이름·위치 재정비", "✏️"),
    ("reset", "인증 이력 초기화", "🗑️"),
    ("ping", "봇 상태", "🏓"),
]


class AdminPanelView(discord.ui.View):
    """관리자 전용 패널: 드롭다운 하나로 모든 관리 기능을 제공한다.

    - 패널 채널에 고정되는 **영구 뷰**(``is_panel=True``)에는 드롭다운이 있으며,
      항목을 고르면 **나에게만 보이는 ephemeral 메시지 1개**를 새로 연다.
    - 그 ephemeral 메시지의 드롭다운(``is_panel=False``)에서 기능을 실행하면,
      결과를 **같은 메시지에 계속 덮어써서(edit)** 보여준다 → 메시지가 쌓이지 않는다.
    """

    def __init__(self, bot: MindFinderBot, *, is_panel: bool = True) -> None:
        super().__init__(timeout=None if is_panel else 1800)
        self.bot = bot
        self.is_panel = is_panel
        # ephemeral 뷰는 영구 저장이 필요 없으므로 custom_id 를 매번 새로 생성한다.
        # (discord.py 는 Select 의 custom_id 에 None 을 허용하지 않는다.)
        select: discord.ui.Select = discord.ui.Select(
            placeholder="🛠 관리자 기능 선택",
            custom_id="mf:a_action" if is_panel else f"mf:a_action:{id(self)}",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=value, emoji=emoji)
                for value, label, emoji in _ADMIN_OPTIONS
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await self._deny(interaction)
            return
        # 영구 패널에서 고르면 → ephemeral 메시지 1개를 열고 그 안에서 조작한다.
        if self.is_panel:
            await interaction.response.send_message(
                "🛠 **관리자 기능** — 아래에서 원하는 작업을 선택하세요.\n"
                "(이 메시지는 나만 볼 수 있고, 작업 결과가 이 메시지에 계속 표시됩니다.)",
                view=AdminPanelView(self.bot, is_panel=False),
                ephemeral=True,
            )
            return
        # ephemeral 메시지 안에서 고르면 → 같은 메시지에 결과를 덮어쓴다.
        values = getattr(interaction.data, "get", lambda *_: None)("values") or []
        value = values[0] if values else None
        await self._dispatch(interaction, value)

    async def _dispatch(
        self, interaction: discord.Interaction, value: Optional[str]
    ) -> None:
        handlers = {
            "hospitals": self.hospital_requests,
            "hospitals_list": self.list_hospitals,
            "knowledge": self.knowledge_review,
            "reviews": self.review_list,
            "stats": self.stats,
            "terms": self.repost_terms,
            "channels": self.rebuild_channels,
            "maintain": self.maintain,
            "names": self.channel_names,
            "reset": self.reset_verifications,
            "ping": self.ping,
        }
        handler = handlers.get(value or "")
        if handler is None:
            await interaction.response.edit_message(
                content="알 수 없는 항목입니다.", view=self
            )
            return
        await handler(interaction, None)

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        perms = interaction.user.guild_permissions
        return bool(perms.administrator or perms.manage_guild)

    async def _deny(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "관리자만 사용할 수 있습니다.", ephemeral=True
        )

    @staticmethod
    def _panel_view(bot: MindFinderBot) -> "AdminPanelView":
        """ephemeral 메시지에 계속 붙일 뷰(드롭다운 유지)."""
        return AdminPanelView(bot, is_panel=False)

    async def hospital_requests(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        pending = await self.bot.hospital_requests.list_by_status("pending")
        if not pending:
            await interaction.response.edit_message(
                content="대기 중인 병원 추가 요청이 없습니다.",
                embed=None,
                view=self._panel_view(self.bot),
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
        await interaction.response.edit_message(
            content=None, embed=embed, view=self._panel_view(self.bot)
        )
        # 각 요청 승인/거절 버튼을 관리자 채널에 게시한다.
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

    async def list_hospitals(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        """등록된 병원 목록과 각 병원의 인증 이력 수를 보여준다."""
        hospitals = await self.bot.hospitals.list_all()
        lines: list[str] = []
        for hospital in hospitals[:20]:
            count = await self.bot.admissions.count_by_hospital(hospital.id)
            ward = f" · {hospital.ward}" if getattr(hospital, "ward", None) else ""
            lines.append(
                f"• **{hospital.name}**{ward} (id={hospital.id}, 인증이력 {count})"
            )
        embed = discord.Embed(
            title=f"🗂️ 등록 병원 ({len(hospitals)}곳)",
            description="\n".join(lines) or "등록된 병원이 없습니다.",
            color=discord.Color.dark_teal(),
        )
        await interaction.response.edit_message(
            content=None, embed=embed, view=self._panel_view(self.bot)
        )

    async def knowledge_review(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        """관리자 최종 승인 대기 중인 지식기반 인증을 보여준다."""
        pending = await self.bot.knowledge.list_pending_review()
        if not pending:
            await interaction.response.edit_message(
                content="승인 대기 중인 지식기반 인증이 없습니다.",
                embed=None,
                view=self._panel_view(self.bot),
            )
            return
        embed = discord.Embed(
            title=f"🧠 지식기반 인증 승인 대기 ({len(pending)}건)",
            description=(
                "아래 신청을 검토하세요. 판정/승인은 각 신청의 버튼 또는 "
                "`/지식인증승인`·`/지식인증거절` 로 처리합니다."
            ),
            color=discord.Color.gold(),
        )
        for req in pending[:MAX_ACTIONS]:
            embed.add_field(
                name=f"신청 #{req.id} · {req.period_text}",
                value=f"신청자: <@{req.asker_id}>\n응답자: <@{req.respondent_id}>",
                inline=False,
            )
        await interaction.response.edit_message(
            content=None, embed=embed, view=self._panel_view(self.bot)
        )

    async def review_list(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        """대기 중인 서류 심사·기타 방법 인증 목록을 보여준다."""
        docs = await self.bot.verifications.list_pending_by_method("document")
        others = await self.bot.verifications.list_pending_by_method("other")
        if not docs and not others:
            await interaction.response.edit_message(
                content="대기 중인 서류·기타 심사가 없습니다.",
                embed=None,
                view=self._panel_view(self.bot),
            )
            return
        embed = discord.Embed(title="📄 대기 중인 심사", color=discord.Color.orange())
        if docs:
            embed.add_field(
                name=f"서류 심사 ({len(docs)}건)",
                value="\n".join(f"#{r.id} · <@{r.user_id}>" for r in docs[:MAX_ACTIONS]),
                inline=False,
            )
        if others:
            embed.add_field(
                name=f"기타 방법 인증 ({len(others)}건)",
                value="\n".join(
                    f"#{r.id} · <@{r.user_id}>" for r in others[:MAX_ACTIONS]
                ),
                inline=False,
            )
        await interaction.response.edit_message(
            content=None, embed=embed, view=self._panel_view(self.bot)
        )

    async def stats(self, interaction: discord.Interaction, _button=None) -> None:
        """등록 이력·인증·매칭 등 운영 현황 요약을 보여준다."""
        hospitals = await self.bot.hospitals.list_all()
        verified_ids = await self.bot.admissions.list_verified_hospital_ids()
        pending_hospitals = await self.bot.hospital_requests.list_by_status("pending")
        pending_docs = await self.bot.verifications.list_pending_by_method("document")
        pending_other = await self.bot.verifications.list_pending_by_method("other")
        pending_knowledge = await self.bot.knowledge.list_pending_review()
        embed = discord.Embed(title="📊 운영 통계", color=discord.Color.blurple())
        embed.add_field(name="등록 병원", value=f"{len(hospitals)}곳", inline=True)
        embed.add_field(name="인증된 병원", value=f"{len(verified_ids)}곳", inline=True)
        embed.add_field(
            name="대기 심사",
            value=(
                f"병원 추가 {len(pending_hospitals)} · "
                f"서류 {len(pending_docs)} · 기타 {len(pending_other)} · "
                f"지식 {len(pending_knowledge)}"
            ),
            inline=False,
        )
        await interaction.response.edit_message(
            content=None, embed=embed, view=self._panel_view(self.bot)
        )

    async def ping(self, interaction: discord.Interaction, _button=None) -> None:
        """봇 지연시간을 보여준다."""
        latency = round(self.bot.latency * 1000)
        await interaction.response.edit_message(
            content=f"🏓 Pong! 지연 {latency}ms",
            embed=None,
            view=self._panel_view(self.bot),
        )

    async def repost_terms(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        if interaction.guild is None:
            await interaction.response.edit_message(
                content="서버에서만 사용할 수 있습니다.", view=self._panel_view(self.bot)
            )
            return
        await interaction.response.defer()
        try:
            await self.bot.terms_service.post_all(interaction.guild)
            message = "✅ 이용약관·개인정보처리방침 전문을 다시 게시했습니다."
        except Exception:  # noqa: BLE001
            logger.exception("약관 재게시 실패: guild=%s", interaction.guild.id)
            message = "⚠️ 약관 재게시 중 오류가 발생했습니다."
        await interaction.edit_original_response(
            content=message, embed=None, view=self._panel_view(self.bot)
        )

    async def rebuild_channels(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        if interaction.guild is None:
            await interaction.response.edit_message(
                content="서버에서만 사용할 수 있습니다.", view=self._panel_view(self.bot)
            )
            return
        await interaction.response.defer()
        try:
            await self.bot.channel_service.ensure_all(interaction.guild)
            cog = interaction.client.get_cog("PanelCog")
            if cog is not None:
                await cog.ensure_panels(interaction.guild)
            message = "✅ 용도별 채널/패널 구성을 완료했습니다."
        except Exception:  # noqa: BLE001
            logger.exception("채널 재구성 실패: guild=%s", interaction.guild.id)
            message = (
                "⚠️ 채널 재구성 중 오류가 발생했습니다. "
                "봇에 '채널 관리' 권한이 있는지 확인해주세요."
            )
        await interaction.edit_original_response(
            content=message, embed=None, view=self._panel_view(self.bot)
        )

    async def maintain(self, interaction: discord.Interaction, _button=None) -> None:
        """역할 이름 정리 + 인증 기반 병원 역할/채널 동기화를 실행한다."""
        if interaction.guild is None:
            await interaction.response.edit_message(
                content="서버에서만 사용할 수 있습니다.", view=self._panel_view(self.bot)
            )
            return
        await interaction.response.defer()
        try:
            await self.bot.channel_service.migrate_role_names(interaction.guild)
            await self.bot.channel_service.ensure_all(interaction.guild)
            message = "✅ 역할 이름 정리와 인증 기반 채널 동기화를 완료했습니다."
        except Exception:  # noqa: BLE001
            logger.exception("역할·채널 재정비 실패: guild=%s", interaction.guild.id)
            message = "⚠️ 재정비 중 오류가 발생했습니다."
        await interaction.edit_original_response(
            content=message, embed=None, view=self._panel_view(self.bot)
        )

    async def channel_names(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        """봇이 관리하는 채널/카테고리를 ID 기준으로 찾아 이름·위치를 맞춘다."""
        if interaction.guild is None:
            await interaction.response.edit_message(
                content="서버에서만 사용할 수 있습니다.", view=self._panel_view(self.bot)
            )
            return
        await interaction.response.defer()
        records = await self.bot.managed_channels.list_for_guild(interaction.guild.id)
        fixed = 0
        for record in records:
            channel = interaction.guild.get_channel(record.discord_id)
            if channel is None:
                continue
            if getattr(channel, "name", None) != record.name:
                try:
                    await channel.edit(name=record.name)
                    fixed += 1
                except (discord.Forbidden, discord.HTTPException):
                    logger.debug("채널 이름 재정비 실패: %s", record.discord_id)
        await interaction.edit_original_response(
            content=(
                f"✅ 채널/카테고리 {len(records)}개를 ID 기준으로 확인했고, "
                f"{fixed}개 이름을 재정비했습니다."
            ),
            embed=None,
            view=self._panel_view(self.bot),
        )

    async def reset_verifications(
        self, interaction: discord.Interaction, _button=None
    ) -> None:
        """인증 관련 데이터를 전면 초기화한다 (되돌릴 수 없음)."""
        await interaction.response.defer()
        removed = await reset_verifications(self.bot.db)
        summary = ", ".join(f"{k}={v}" for k, v in removed.items() if v) or "삭제 없음"
        await interaction.edit_original_response(
            content=f"🗑️ 인증 이력 초기화 완료: {summary}",
            embed=None,
            view=self._panel_view(self.bot),
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

    # 병원 카테고리는 확보하되 **역할(태그)은 자동 부여하지 않는다.**
    # 역할은 사용자가 커뮤니티 패널의 '병원 역할 태그 설정'에서 직접 선택할 때만
    # 부여하고, 선택 상태는 DB(hospital_optins)로 사람별 관리한다.
    hospital_name = getattr(result, "hospital_name", None)
    if not hospital_name:
        return
    hospital = await bot.hospitals.get_by_name(hospital_name)
    if hospital is None:
        hospital = await VerificationService(bot).resolve_hospital(hospital_name)
    try:
        await bot.channel_service.ensure_hospital_category(
            guild, hospital.id, hospital.name, with_role=False
        )
    except Exception:  # noqa: BLE001
        logger.exception("병원 카테고리 확보 실패: hospital=%s", hospital.id)

    # 인증 직후 **내 입원 기간 채널을 열어 준다** (개별 권한, 역할 태그와 무관).
    try:
        await bot.channel_service.ensure_community_access(guild, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("인증 후 기간 채널 동기화 실패: user=%s", user_id)


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

        # 재시작 전 접수된 '수동 서류 심사' 검토 버튼을 다시 살린다.
        try:
            doc_pending = await self.bot.verifications.list_pending_by_method(
                "document"
            )
        except Exception:  # noqa: BLE001
            doc_pending = []
            logger.exception("서류 심사 요청 조회 실패")
        for req in doc_pending:
            try:
                self.bot.add_view(DocumentReviewView(self.bot, req.id))
            except Exception:  # noqa: BLE001
                logger.exception("서류 심사 뷰 등록 실패: request=%s", req.id)

        for guild in self.bot.guilds:
            # 1) 용도별 채널/카테고리 전체 구성 (인증/커뮤니티/매칭/관리자/약관/병원별)
            try:
                await self.bot.channel_service.ensure_all(guild)
            except Exception:  # noqa: BLE001 - 한 서버 실패가 전체를 막지 않도록
                logger.exception("채널 자동 구성 실패: guild=%s", guild.id)
            # 기존 역할 이름을 새 이름(임시인증/인증됨/병원 인증됨)으로 정리
            try:
                await self.bot.channel_service.migrate_role_names(guild)
            except Exception:  # noqa: BLE001
                logger.exception("역할 이름 정리 실패: guild=%s", guild.id)
            # 2) 약관·개인정보 전문 게시 (전용 채널)
            try:
                await self.bot.terms_service.post_all(guild)
            except Exception:  # noqa: BLE001
                logger.exception("약관 게시 실패: guild=%s", guild.id)
            # 3) 기능별 패널 게시
            await self.ensure_panels(guild)

    # ---------- 패널 게시 ----------

    async def ensure_panels(self, guild: discord.Guild) -> None:
        """기능별 채널에 상시 패널을 게시/갱신한다.

        커뮤니티/매칭 채널은 '대화'용이므로 패널을 두지 않고,
        커뮤니티 메뉴(역할 선택·지식 인증)는 🧭-패널 채널에 함께 게시한다.
        """
        specs = [
            (VERIFY_CHANNEL_KEY, PANEL_MARKER_VERIFY, self._build_verify_embed, VerifyPanelView),
            (ADMIN_CHANNEL_KEY, PANEL_MARKER_ADMIN, self._build_admin_embed, AdminPanelView),
            (
                PANEL_CHANNEL_KEY,
                PANEL_MARKER_COMMUNITY,
                self._build_community_embed,
                CommunityPanelView,
            ),
            (PANEL_CHANNEL_KEY, PANEL_MARKER_HELP, self._build_help_embed, HelpPanelView),
            # 매칭 채널에도 상시 패널을 게시해 사용법을 안내한다.
            (MATCH_CHANNEL_KEY, PANEL_MARKER_MATCH, self._build_match_embed, MatchPanelView),
        ]
        for key, marker, embed_builder, view_cls in specs:
            channel = await self._managed_channel(guild, key)
            if channel is None:
                continue
            await self._post_panel(channel, marker, embed_builder(), view_cls(self.bot))
        # 커뮤니티 채널(대화 전용)에 남아 있는 이전 패널만 정리한다.
        await self._delete_stale_panels(guild, (COMMUNITY_CHANNEL_KEY,))

    async def _delete_stale_panels(self, guild: discord.Guild, keys) -> None:  # noqa: ANN001
        """지정 채널에 남아 있는 봇 패널 메시지(마커 포함)를 삭제한다."""
        for key in keys:
            channel = await self._managed_channel(guild, key)
            if channel is None:
                continue
            try:
                async for msg in channel.history(limit=50):
                    if (
                        msg.author.id == self.bot.user.id
                        and msg.embeds
                        and msg.embeds[0].footer
                        and (msg.embeds[0].footer.text or "").startswith("MindFinder")
                    ):
                        try:
                            await msg.delete()
                            logger.info("이전 패널 정리: channel=%s", channel.id)
                        except discord.HTTPException:
                            logger.debug("이전 패널 삭제 실패: %s", msg.id)
            except discord.HTTPException:
                logger.debug("패널 정리 조회 실패: channel=%s", channel.id)

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
        """뷰 구성요소(버튼/셀렉트)를 비교 가능한 문자열로 만든다.

        주의 1: **기존 메시지의 컴포넌트**(디스코드 API 응답)와 **뷰 children**
        (로컬 객체)은 서로 다른 타입이라, 양쪽에 공통으로 안정적으로 존재하는
        값(custom_id / label)만 쓴다.
        (placeholder/options/style/disabled 는 API 응답 표현이 달라
         변경이 없어도 '변경됨'으로 오판할 수 있으므로 제외한다.)

        주의 2: **순서를 비교하지 않는다.** 디스코드 API 는 컴포넌트를 row 순으로
        묶어 반환하지만, 로컬 `view.children` 은 코드에 정의한 순서를 그대로
        유지한다. 두 순서가 다르면(예: row0/row1/row1/row0... 로 정의) 내용이
        같아도 '변경됨'으로 오판해 재시작마다 재게시된다. 따라서 정렬해 비교한다.
        """
        parts: list[str] = []
        for item in items:
            parts.append(
                "\x1f".join(
                    [
                        str(getattr(item, "custom_id", "") or ""),
                        str(getattr(item, "label", "") or ""),
                    ]
                )
            )
        # 정의 순서(row 배치)가 아니라 '구성 자체'의 변경만 감지한다.
        parts.sort()
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
        """채널에 패널 메시지를 게시/갱신한다.

        **변경사항이 있을 때만** 이전 패널 메시지를 지우고 새로 게시·고정한다.
        내용이 동일하면 아무것도 하지 않아 불필요한 삭제·재게시·재고정을 피한다.
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

        if matches:
            first = matches[0]
            unchanged = (
                self._embed_signature(first.embeds[0]) == self._embed_signature(embed)
                and self._items_signature(self._message_items(first))
                == self._items_signature(view.children)
            )
            # 변경사항이 없으면 그대로 둔다 (삭제·재전송·고정 안 함).
            if unchanged:
                return
            # 변경사항이 있으면 기존 패널(중복 포함)을 모두 지우고 새로 게시한다.
            for msg in matches:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    logger.debug("이전 패널 삭제 실패: %s", msg.id)
            logger.info("패널 변경 감지 → 재게시: channel=%s marker=%s", channel.id, marker)
            # 삭제로 생긴 '고정 해제' 시스템 메시지를 정리한다.
            await self._delete_pin_system_messages(channel)

        # 패널이 없으면(또는 변경으로 지웠으면) 새로 게시하고 고정한다.
        try:
            sent = await channel.send(embed=embed, view=view)
            logger.info("패널 게시: channel=%s marker=%s", channel.id, marker)
            try:
                await sent.pin(reason="MindFinder 상시 패널 고정")
                # 고정 시 생기는 '...고정하셨어요' 시스템 메시지를 삭제한다.
                await self._delete_pin_system_messages(channel)
            except discord.HTTPException:
                logger.debug("패널 고정 실패: channel=%s", channel.id)
        except discord.HTTPException:
            logger.exception("패널 전송 실패: channel=%s", channel.id)

    @staticmethod
    async def _delete_pin_system_messages(
        channel: discord.TextChannel, *, limit: int = 20
    ) -> None:
        """채널의 '고정/고정 해제' 시스템 메시지를 삭제한다.

        봇이 패널을 고정/삭제할 때 디스코드가 자동으로 남기는
        '...님이 이 채널에 메시지를 고정하셨어요' 류의 안내 메시지를 지운다.
        """
        # discord.py 버전에 따라 '고정 해제' 타입이 없을 수 있으므로 존재하는 것만 쓴다.
        pin_types = {
            getattr(discord.MessageType, name)
            for name in ("pins_add", "pins_remove")
            if hasattr(discord.MessageType, name)
        }
        try:
            async for msg in channel.history(limit=limit):
                if msg.type in pin_types:
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        logger.debug("고정 시스템 메시지 삭제 실패: %s", msg.id)
        except discord.HTTPException:
            logger.debug("고정 시스템 메시지 조회 실패: channel=%s", channel.id)

    # ---------- 패널 임베드 ----------

    def _build_verify_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🔐 인증",
            description=(
                "이곳에서 **입원 이력**을 등록하고 **서류 인증**을 받습니다.\n"
                "아래 **메뉴(드롭다운)** 에서 원하는 항목을 선택하세요."
            ),
            color=discord.Color.green(),
        )
        embed.add_field(
            name="① 입원 이력 등록 (자기 신고 → 임시인증)",
            value="병원·기간을 입력하면 **임시인증** → **전체 커뮤니티** 이용",
            inline=False,
        )
        embed.add_field(
            name="② 서류 인증 (→ 인증됨)",
            value=(
                "‘서류 인증’ 버튼 → **창(모달)에서 병원 선택** 후 진행\n"
                "**서울대병원 계열** 사실확인서 PDF → 자동 진위확인\n"
                "**그 외 병원** → 입원 기간 입력 후 관리자 수동 심사\n"
                "목록에 없는 병원은 ‘기타 병원 직접 입력’으로 요청\n"
                "**인증됨** → **병원·기간 커뮤니티 + 매칭** 이용"
            ),
            inline=False,
        )
        embed.add_field(
            name="③ 지식기반 인증 / 기타 방법",
            value=(
                "이미 서류 인증을 마친 사람이라면 **‘지식기반 인증하기’** 로 "
                "동료에게 병동 경험 질문을 보낼 수 있습니다(‘지식기반 인증 현황’에서 "
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
                "(**인증됨** 사용자 전용)"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="이용 순서",
            value=(
                "1) **매칭 희망 켜기**\n"
                "2) **동료 찾기** → 후보에게 신청\n"
                "3) 상대가 **수락**하면 비공개 채널 개설\n"
                "4) 대화가 끝나면 **매칭 채널 닫기** 로 정리 (기록 보존)"
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_MARKER_MATCH)
        return embed

    def _build_community_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="💬 전체 커뮤니티",
            description=(
                "**임시인증**(자기 신고) 이상이면 이용할 수 있는 전체 커뮤니티입니다.\n"
                "**인증됨**(서류·지식 인증)을 받으면 인증한 병원의 "
                "**병원·기간 커뮤니티**(월 단위 비공개 채널)가 자동으로 열립니다."
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
                "🧩 **매칭** — 동료 찾기 (인증됨)\n"
                "💬 **전체 커뮤니티** — 임시인증 이상\n"
                "🏥 **병원 카테고리** — 인증한 병원의 월 단위 비공개 기간 채널"
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
                "심사·통계·채널/역할 관리까지 모든 운영 기능을 이 패널에서 처리합니다."
            ),
            color=discord.Color.dark_red(),
        )
        embed.add_field(
            name="심사",
            value=(
                "· **병원 추가 요청 검토** — 사용자가 요청한 병원 승인/거절\n"
                "· **지식기반 인증 심사** — 최종 승인 대기 목록\n"
                "· **서류·기타 심사 목록** — 대기 중인 서류/기타 인증"
            ),
            inline=False,
        )
        embed.add_field(
            name="운영·정비",
            value=(
                "· **운영 통계** — 병원·인증·대기 심사 현황\n"
                "· **약관 재게시** — 이용약관·개인정보처리방침 재게시\n"
                "· **채널 재구성** — 용도별 채널/패널 재생성\n"
                "· **역할·채널 재정비** — 역할 이름 정리 + 인증 기반 동기화\n"
                "· **채널 이름·위치 재정비** — ID 기준으로 채널 이름 재정렬"
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
        # 운영 서버(GUILD_ID)에서만 동작한다.
        allowed = getattr(self.bot, "_is_allowed_guild", None)
        if callable(allowed) and not allowed(message.guild):
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
        # 자동검색은 PDF 를 한 번 확인하면 즉시 삭제한다 (원본 미보관).
        try:
            await message.delete()
        except discord.HTTPException:
            logger.debug("PDF 메시지 삭제 실패: %s", message.id)

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
