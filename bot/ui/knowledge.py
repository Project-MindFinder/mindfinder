"""지식기반 인증 UI.

흐름:
  1) 이미 서류 인증을 마친 사용자(asker)가 본인의 인증된 이력을 고르고
     "그 시기·병원에 실제로 있었던 사람만 아는" 질문을 작성한다.
  2) 응답자(respondent)에게 DM 으로 질문이 전달되고, 답변을 작성한다.
  3) asker 가 정답 여부를 판정한다 (정답 → 관리자 승인 대기).
  4) 관리자가 최종 승인하면 응답자에게 해당 병원 인증이 부여된다.

재시작 후에도 버튼이 동작하도록 모든 뷰는 영구 뷰(persistent view)이며
custom_id 에 신청 번호를 담는다. 코그가 시작 시 DB 에서 대기 건을 조회해
`register_persistent_views()` 로 뷰를 다시 등록한다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

# 관리자 승인 대기 상태
PENDING_REVIEW_STATUS = "asker_confirmed"


async def notify_admins(
    bot: "MindFinderBot",
    embed: discord.Embed,
    view: Optional[discord.ui.View] = None,
) -> None:
    """관리자 역할 사용자들에게 DM 으로 알린다 (실패해도 무시)."""
    settings = bot.settings
    seen: set[int] = set()
    for guild in bot.guilds:
        admin_role = (
            guild.get_role(settings.admin_role_id) if settings.admin_role_id else None
        )
        for member in guild.members:
            if member.bot or member.id in seen:
                continue
            is_admin = member.guild_permissions.administrator
            if admin_role is not None and admin_role in member.roles:
                is_admin = True
            if not is_admin:
                continue
            seen.add(member.id)
            try:
                await member.send(embed=embed, view=view)
            except discord.Forbidden:
                logger.debug("관리자 DM 전송 실패: user=%s", member.id)
            except discord.HTTPException:
                logger.exception("관리자 DM 오류: user=%s", member.id)


# --------------------------------------------------------------------------- #
# asker: 질문 작성
# --------------------------------------------------------------------------- #
class QuestionModal(discord.ui.Modal, title="지식기반 인증 질문"):
    """응답자에게 보낼 질문을 작성한다."""

    질문 = discord.ui.TextInput(
        label="질문",
        style=discord.TextStyle.paragraph,
        placeholder="예: 3월에 입원했을 때 담당 간호사실 위치가 어디였나요?",
        max_length=500,
    )

    def __init__(
        self, bot: "MindFinderBot", admission_id: int, respondent_id: int
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

        # 응답자에게 DM 전송
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
            "✅ 질문을 보냈습니다. 상대가 답변하면 판정할 수 있습니다.",
            ephemeral=True,
        )


class AskQuestionView(discord.ui.View):
    """asker 가 본인의 인증된 이력을 골라 질문을 작성하도록 하는 뷰."""

    def __init__(
        self,
        bot: "MindFinderBot",
        admissions: list,
        respondent_id: int,
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
            QuestionModal(self.bot, admission_id, self.respondent_id)
        )


# --------------------------------------------------------------------------- #
# respondent: 답변 작성
# --------------------------------------------------------------------------- #
class AnswerModal(discord.ui.Modal, title="질문 답변"):
    답변 = discord.ui.TextInput(
        label="답변",
        style=discord.TextStyle.paragraph,
        placeholder="기억나는 내용을 자유롭게 적어주세요.",
        max_length=1000,
    )

    def __init__(self, bot: "MindFinderBot", request_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        req = await self.bot.knowledge_service.answer(
            self.request_id, interaction.user.id, str(self.답변.value)
        )
        if req is None:
            await interaction.response.send_message(
                "이미 처리되었거나 유효하지 않은 요청입니다.", ephemeral=True
            )
            return

        # asker 에게 판정 요청
        asker = None
        try:
            asker = await self.bot.fetch_user(req.asker_id)
        except discord.HTTPException:
            asker = None

        hospital_name = await self.bot.knowledge_service.hospital_name(req.hospital_id)
        embed = discord.Embed(
            title="📝 답변이 도착했습니다 — 정답 여부를 판정해주세요",
            description=(
                f"**병원**: {hospital_name}\n**기간**: {req.period_text}\n\n"
                f"**질문**\n> {req.question}\n\n**답변**\n> {req.answer}"
            ),
            color=discord.Color.teal(),
        )
        if asker is not None:
            try:
                await asker.send(
                    embed=embed, view=VerdictView(self.bot, self.request_id)
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.info("asker DM 전송 실패: user=%s", req.asker_id)

        await interaction.response.send_message(
            "✅ 답변을 제출했습니다. 질문자가 판정하면 결과를 알려드립니다.",
            ephemeral=True,
        )


class AnswerRequestView(discord.ui.View):
    """응답자가 답변을 작성하도록 하는 영구 뷰."""

    def __init__(self, bot: "MindFinderBot", request_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        button = discord.ui.Button(
            label="답변 작성",
            style=discord.ButtonStyle.success,
            custom_id=f"mf:kn:answer:{request_id}",
        )
        button.callback = self._answer
        self.add_item(button)

    async def _answer(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AnswerModal(self.bot, self._request_id()))

    def _request_id(self) -> int:
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id:
                return int(child.custom_id.rsplit(":", 1)[-1])
        return 0


# --------------------------------------------------------------------------- #
# asker: 정답 판정
# --------------------------------------------------------------------------- #
class VerdictView(discord.ui.View):
    """asker 가 응답의 정답 여부를 판정하는 영구 뷰."""

    def __init__(self, bot: "MindFinderBot", request_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id

        correct = discord.ui.Button(
            label="정답 (인증 진행)",
            style=discord.ButtonStyle.success,
            custom_id=f"mf:kn:yes:{request_id}",
        )
        correct.callback = self._make(True)
        self.add_item(correct)

        wrong = discord.ui.Button(
            label="오답",
            style=discord.ButtonStyle.danger,
            custom_id=f"mf:kn:no:{request_id}",
        )
        wrong.callback = self._make(False)
        self.add_item(wrong)

    def _make(self, correct: bool):
        async def _callback(interaction: discord.Interaction) -> None:
            req = await self.bot.knowledge_service.verdict(
                self.request_id, interaction.user.id, correct
            )
            if req is None:
                await interaction.response.send_message(
                    "이미 판정되었거나 유효하지 않은 요청입니다.", ephemeral=True
                )
                return
            if not correct:
                await interaction.response.send_message(
                    "오답으로 처리했습니다.", ephemeral=True
                )
                return

            await interaction.response.send_message(
                "✅ 정답으로 확인했습니다. 관리자 최종 승인을 기다립니다.",
                ephemeral=True,
            )
            # 관리자에게 최종 승인 요청
            hospital_name = await self.bot.knowledge_service.hospital_name(
                req.hospital_id
            )
            embed = discord.Embed(
                title="🔔 지식기반 인증 — 관리자 승인 대기",
                description=(
                    f"**신청번호**: #{req.id}\n"
                    f"**질문자**: <@{req.asker_id}>\n"
                    f"**응답자**: <@{req.respondent_id}>\n"
                    f"**병원**: {hospital_name}\n**기간**: {req.period_text}\n\n"
                    f"**질문**\n> {req.question}\n\n**답변**\n> {req.answer}\n\n"
                    "정답으로 확인되었습니다. 최종 승인/거절해주세요."
                ),
                color=discord.Color.gold(),
            )
            await notify_admins(
                self.bot, embed, AdminReviewView(self.bot, self.request_id)
            )

        return _callback


# --------------------------------------------------------------------------- #
# 관리자: 최종 승인 / 거절
# --------------------------------------------------------------------------- #
class AdminRejectModal(discord.ui.Modal, title="지식기반 인증 거절"):
    사유 = discord.ui.TextInput(
        label="거절 사유 (선택)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )

    def __init__(self, bot: "MindFinderBot", request_id: int) -> None:
        super().__init__()
        self.bot = bot
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note = str(self.사유.value).strip() or None
        req = await self.bot.knowledge_service.admin_reject(
            self.request_id, interaction.user.id, note
        )
        if req is None:
            await interaction.response.send_message(
                "이미 처리되었거나 유효하지 않은 요청입니다.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"❌ 신청 #{self.request_id} 를 거절했습니다.", ephemeral=True
        )


class AdminReviewView(discord.ui.View):
    """관리자가 지식기반 인증을 최종 승인/거절하는 영구 뷰."""

    def __init__(self, bot: "MindFinderBot", request_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id

        approve = discord.ui.Button(
            label="최종 승인",
            style=discord.ButtonStyle.success,
            custom_id=f"mf:kn:appr:{request_id}",
        )
        approve.callback = self._approve
        self.add_item(approve)

        reject = discord.ui.Button(
            label="거절",
            style=discord.ButtonStyle.danger,
            custom_id=f"mf:kn:rej:{request_id}",
        )
        reject.callback = self._reject
        self.add_item(reject)

    async def _approve(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 승인할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        req = await self.bot.knowledge_service.admin_approve(
            self.request_id, interaction.user.id
        )
        if req is None:
            await interaction.followup.send(
                "이미 처리되었거나 유효하지 않은 요청입니다.", ephemeral=True
            )
            return

        # 응답자에게 해당 병원 인증(역할) 부여
        await self._grant_respondent(req)
        await interaction.followup.send(
            f"✅ 신청 #{self.request_id} 를 승인했습니다. 응답자에게 인증을 부여했습니다.",
            ephemeral=True,
        )

    async def _reject(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            await interaction.response.send_message(
                "관리자만 거절할 수 있습니다.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            AdminRejectModal(self.bot, self.request_id)
        )

    async def _grant_respondent(self, req) -> None:
        hospital_name = await self.bot.knowledge_service.hospital_name(req.hospital_id)
        for guild in self.bot.guilds:
            member = guild.get_member(req.respondent_id)
            if member is None:
                continue
            try:
                await self.bot.access.grant_hospital_role(
                    guild, member, req.hospital_id, hospital_name
                )
                await self.bot.access.grant_tier(guild, member, 1)
            except Exception:  # noqa: BLE001 - 역할 부여 실패가 승인을 막지 않도록
                logger.exception("지식인증 역할 부여 실패: user=%s", req.respondent_id)
            try:
                await self.bot.channel_service.ensure_hospital_channel(
                    guild, req.hospital_id, hospital_name
                )
            except Exception:  # noqa: BLE001
                logger.exception("병원 채널 확보 실패: hospital=%s", req.hospital_id)

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        settings = self.bot.settings
        if settings.admin_role_id and hasattr(interaction.user, "roles"):
            return any(r.id == settings.admin_role_id for r in interaction.user.roles)
        return False


# --------------------------------------------------------------------------- #
# 재시작 후 영구 뷰 재등록
# --------------------------------------------------------------------------- #
async def register_persistent_views(bot: "MindFinderBot") -> None:
    """DB 에 남아 있는 대기 건에 대해 영구 뷰를 다시 등록한다."""
    # 응답 대기 → 응답자 답변 버튼
    for req in await bot.knowledge.list_by_status("awaiting_answer"):
        bot.add_view(AnswerRequestView(bot, req.id), message_id=req.message_id)
    # 판정 대기 → asker 판정 버튼
    for req in await bot.knowledge.list_by_status("awaiting_verdict"):
        bot.add_view(VerdictView(bot, req.id))
    # 관리자 승인 대기 → 관리자 버튼
    for req in await bot.knowledge.list_by_status(PENDING_REVIEW_STATUS):
        bot.add_view(AdminReviewView(bot, req.id))
