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
    COMMUNITY_CHANNEL_KEY,
    COMMUNITY_CHANNEL_NAME,
    MATCH_CHANNEL_KEY,
    MATCH_CHANNEL_NAME,
    PANEL_CHANNEL_KEY,
    PANEL_CHANNEL_NAME as MANAGED_PANEL_CHANNEL_NAME,
    VERIFY_CHANNEL_KEY,
    VERIFY_CHANNEL_NAME,
)
from bot.services.matching import Candidate, MatchingService
from bot.services.verification import VERIFICATION_LABEL, VerificationService
from bot.ui.consent import build_consent_embed, ensure_consent
from bot.ui.hospitals import HospitalSelectView
from bot.utils.periods import PeriodError

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
# 모달: 서울대병원 사실확인서 PDF 업로드 (창에서 파일 선택)
# --------------------------------------------------------------------------- #
class SNUHTruthModal(discord.ui.Modal, title="서울대병원 서류 인증"):
    """창(모달)에서 입퇴원 사실확인서 PDF 를 업로드받아 자동 진위확인한다."""

    def __init__(self, bot: MindFinderBot, service: VerificationService) -> None:
        super().__init__()
        self.bot = bot
        self.service = service
        upload = discord.ui.FileUpload(
            custom_id="mf:truth_pdf",
            required=True,
            max_values=1,
        )
        self.서류 = upload
        self.add_item(
            discord.ui.Label(
                text="입퇴원 사실확인서 PDF (서울대병원 발급)",
                description="발급받은 PDF 파일을 선택하세요. 원본은 저장하지 않습니다.",
                component=upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        attachments = list(getattr(self.서류, "values", []) or [])
        if not attachments:
            await interaction.response.send_message(
                "⚠️ PDF 파일을 첨부해주세요.", ephemeral=True
            )
            return
        attachment = attachments[0]
        if not attachment.filename.lower().endswith(".pdf"):
            await interaction.response.send_message(
                "⚠️ PDF 파일만 제출할 수 있습니다.", ephemeral=True
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
        label="서울대병원 서류 인증", style=discord.ButtonStyle.success,
        custom_id="mf:v_truth", row=0,
    )
    async def truth(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        async def _proceed(inner: discord.Interaction) -> None:
            await inner.response.send_modal(SNUHTruthModal(self.bot, self.vservice))

        if not await ensure_consent(self.bot, interaction, _proceed):
            return
        await _proceed(interaction)

    @discord.ui.button(
        label="다른 병원 서류 인증", style=discord.ButtonStyle.secondary,
        custom_id="mf:v_other", row=0,
    )
    async def other_hospital(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
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
        if self.vservice.supports_auto_verify(hospital.name):
            await interaction.response.send_message(
                f"🏥 **{hospital.name}** 은(는) 자동 진위확인을 지원합니다.\n"
                "위의 **서울대병원 서류 인증** 버튼을 눌러 PDF 를 업로드해주세요.",
                ephemeral=True,
            )
            return
        from bot.ui.hospitals import ManualProofModal

        await interaction.response.send_modal(
            ManualProofModal(self.bot, hospital.id, hospital.name)
        )

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
        await self.mservice.set_matching(interaction.user.id, True)
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
    """커뮤니티 안내 패널: 병원별 채널/기간 스레드 안내."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="병원별 채널 안내", style=discord.ButtonStyle.primary,
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
                "같은 시기(입원 기간)에 입원했던 분들과 이야기할 수 있습니다."
            ),
            color=discord.Color.teal(),
        )
        names = ", ".join(h.name for h in hospitals[:20]) or "아직 등록된 병원이 없습니다."
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
        bot.add_view(HelpPanelView(bot))
        self._panels_ready = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._panels_ready:
            return
        self._panels_ready = True
        for guild in self.bot.guilds:
            # 1) 용도별 채널/카테고리 전체 구성 (인증/커뮤니티/매칭/안내/병원별)
            try:
                await self.bot.channel_service.ensure_all(guild)
            except Exception:  # noqa: BLE001 - 한 서버 실패가 전체를 막지 않도록
                logger.exception("채널 자동 구성 실패: guild=%s", guild.id)
            # 2) 기능별 패널 게시
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

    async def _post_panel(
        self,
        channel: discord.TextChannel,
        marker: str,
        embed: discord.Embed,
        view: discord.ui.View,
    ) -> None:
        """채널에 패널 메시지를 게시하거나 기존 메시지를 갱신한다."""
        existing: Optional[discord.Message] = None
        try:
            async for msg in channel.history(limit=50):
                if (
                    msg.author.id == self.bot.user.id
                    and msg.embeds
                    and msg.embeds[0].footer
                    and msg.embeds[0].footer.text == marker
                ):
                    existing = msg
                    break
        except discord.HTTPException:
            logger.exception("패널 메시지 조회 실패: channel=%s", channel.id)
        try:
            if existing is not None:
                await existing.edit(embed=embed, view=view)
                logger.info("패널 갱신: channel=%s marker=%s", channel.id, marker)
            else:
                await channel.send(embed=embed, view=view)
                logger.info("패널 게시: channel=%s marker=%s", channel.id, marker)
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
                "**서울대병원** 사실확인서 PDF → 자동 진위확인\n"
                "**그 외 병원** → 관리자 수동 심사\n"
                "2단계 인증 → **병원·기간 커뮤니티 + 매칭** 이용"
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
            name="개인정보 보호",
            value=(
                "연락처는 봇이 저장·전달하지 않습니다. "
                "매칭 성사 시 비공개 채널에서 직접 대화하세요."
            ),
            inline=False,
        )
        embed.set_footer(text=PANEL_MARKER_HELP)
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
PANEL_MARKER_HELP = "MindFinder 안내 패널"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCog(bot))
