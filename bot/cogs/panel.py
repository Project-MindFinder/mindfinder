"""상시 패널 코그: 고정 메시지 + 버튼/모달로 기능을 제공한다.

슬래시 커맨드 대신, 채널에 **항상 떠 있는 안내 패널**을 게시하고
버튼을 눌러 기능을 사용하는 방식을 제공한다.
(슬래시 커맨드도 그대로 유지되어 둘 다 사용할 수 있다.)

- 패널 채널이 없으면 봇이 자동으로 만든다(PANEL_CHANNEL_ID 또는 AUTO_CREATE_PANEL_CHANNEL).
- 패널 메시지는 재시작해도 유지되도록 영구 뷰(persistent view)를 사용한다.
- 서울대병원 사실확인서 PDF 를 패널 채널에 올리면 자동 진위확인 후 인증한다.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.services.matching import Candidate, MatchingService
from bot.services.verification import VERIFICATION_LABEL, VerificationService
from bot.utils.periods import PeriodError

logger = logging.getLogger(__name__)

# 패널 메시지를 식별하는 푸터 마커 (중복 게시 방지)
PANEL_MARKER = "MindFinder 안내 패널"
# 자동 생성할 패널 채널 이름
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

        embed = discord.Embed(
            title="✅ 입원 이력이 등록되었습니다",
            description=(
                f"**병원**: {hospital_name}\n"
                f"**기간**: {period.text()}\n"
                f"**상태**: {VERIFICATION_LABEL['self_reported']}"
            ),
            color=discord.Color.yellow(),
        )
        embed.set_footer(text="서울대병원 서류를 올리면 자동 인증됩니다.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
                            "패널의 '받은 신청' 버튼에서 수락/거절할 수 있습니다."
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
# 메인 패널 (영구 뷰)
# --------------------------------------------------------------------------- #
class MainPanelView(discord.ui.View):
    """채널에 상시 게시되는 안내 패널 (재시작해도 동작하도록 영구 뷰)."""

    def __init__(self, bot: MindFinderBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.vservice = VerificationService(bot)
        self.mservice = MatchingService(bot)

    # --- 인증 ---
    @discord.ui.button(
        label="입원 이력 등록", style=discord.ButtonStyle.primary,
        custom_id="mf:admission", row=0,
    )
    async def admission(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            AdmissionModal(self.bot, self.vservice)
        )

    @discord.ui.button(
        label="서울대병원 서류 인증", style=discord.ButtonStyle.success,
        custom_id="mf:truth", row=0,
    )
    async def truth(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_message(
            "📄 이 채널에 **서울대병원 입퇴원 사실확인서 PDF** 를 그대로 올려주세요.\n"
            "자동으로 진위·정신과 여부를 확인해 인증해드립니다.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="내 이력", style=discord.ButtonStyle.secondary,
        custom_id="mf:history", row=0,
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

    # --- 매칭 ---
    @discord.ui.button(
        label="매칭 희망 켜기", style=discord.ButtonStyle.primary,
        custom_id="mf:match_on", row=1,
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
        custom_id="mf:match_off", row=1,
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
        custom_id="mf:find", row=1,
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

    # --- 받은 신청 / 채널 ---
    @discord.ui.button(
        label="받은 신청", style=discord.ButtonStyle.success,
        custom_id="mf:incoming", row=2,
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
        custom_id="mf:channels", row=2,
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
        label="도움 안내", style=discord.ButtonStyle.secondary,
        custom_id="mf:help", row=2,
    )
    async def help_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        embed = discord.Embed(
            title="🧭 MindFinder 사용법",
            description=(
                "이 패널의 버튼으로 모든 기능을 사용할 수 있습니다.\n"
                "슬래시 명령어도 그대로 사용할 수 있습니다."
            ),
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="인증",
            value=(
                "**입원 이력 등록** — 병원·기간을 입력해 이력 등록\n"
                "**서울대병원 서류 인증** — 사실확인서 PDF 자동 진위확인\n"
                "**내 이력** — 등록한 이력 확인"
            ),
            inline=False,
        )
        embed.add_field(
            name="매칭",
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


# --------------------------------------------------------------------------- #
# 코그
# --------------------------------------------------------------------------- #
class PanelCog(commands.Cog):
    """상시 패널 게시 및 패널 채널의 PDF 자동 진위확인."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot
        self.vservice = VerificationService(bot)
        # 영구 뷰 등록 (재시작 후에도 버튼이 동작하도록)
        bot.add_view(MainPanelView(bot))
        self._panel_ready = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._panel_ready:
            return
        self._panel_ready = True
        for guild in self.bot.guilds:
            try:
                await self.ensure_panel(guild)
            except Exception:  # noqa: BLE001 - 한 서버 실패가 전체를 막지 않도록
                logger.exception("패널 게시 실패: guild=%s", guild.id)

    async def _resolve_panel_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        """패널 채널을 찾거나, 없으면 생성한다."""
        settings = self.bot.settings
        # 1) 설정된 채널 ID
        if settings.panel_channel_id:
            ch = guild.get_channel(settings.panel_channel_id)
            if isinstance(ch, discord.TextChannel):
                return ch
            logger.warning(
                "PANEL_CHANNEL_ID(%s) 채널을 찾을 수 없습니다.", settings.panel_channel_id
            )
        # 2) 이름으로 검색
        for ch in guild.text_channels:
            if ch.name == PANEL_CHANNEL_NAME:
                return ch
        # 3) 자동 생성
        if not settings.auto_create_panel_channel:
            return None
        try:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                    embed_links=True,
                    read_message_history=True,
                ),
            }
            channel = await guild.create_text_channel(
                name=PANEL_CHANNEL_NAME,
                overwrites=overwrites,
                topic="MindFinder 안내 패널 · 버튼으로 이용하세요.",
                reason="MindFinder 패널 채널 자동 생성",
            )
            logger.info("패널 채널 자동 생성: guild=%s channel=%s", guild.id, channel.id)
            return channel
        except discord.Forbidden:
            logger.warning("패널 채널 생성 권한 없음: guild=%s", guild.id)
            return None
        except discord.HTTPException:
            logger.exception("패널 채널 생성 실패: guild=%s", guild.id)
            return None

    def _build_panel_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🧭 MindFinder 안내 패널",
            description=(
                "폐쇄병동 입원 경험자들을 위한 커뮤니티입니다.\n"
                "아래 **버튼**을 눌러 이용하세요."
            ),
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="① 인증",
            value=(
                "**입원 이력 등록** — 병원·기간 입력\n"
                "**서울대병원 서류 인증** — 사실확인서 PDF 자동 검증\n"
                "**내 이력** — 등록 이력 확인"
            ),
            inline=False,
        )
        embed.add_field(
            name="② 매칭",
            value=(
                "**매칭 희망 켜기/끄기** · **동료 찾기**\n"
                "**받은 신청** · **내 매칭 채널**"
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
        embed.set_footer(text=PANEL_MARKER)
        return embed

    async def ensure_panel(self, guild: discord.Guild) -> None:
        """패널 채널을 확보하고 패널 메시지를 게시/갱신한다."""
        channel = await self._resolve_panel_channel(guild)
        if channel is None:
            return

        embed = self._build_panel_embed()
        view = MainPanelView(self.bot)

        # 기존 패널 메시지 찾기 (푸터 마커로 식별)
        existing: Optional[discord.Message] = None
        try:
            async for msg in channel.history(limit=50):
                if (
                    msg.author.id == self.bot.user.id
                    and msg.embeds
                    and msg.embeds[0].footer
                    and msg.embeds[0].footer.text == PANEL_MARKER
                ):
                    existing = msg
                    break
        except discord.HTTPException:
            logger.exception("패널 메시지 조회 실패: channel=%s", channel.id)

        try:
            if existing is not None:
                await existing.edit(embed=embed, view=view)
                logger.info("패널 메시지 갱신: channel=%s", channel.id)
            else:
                await channel.send(embed=embed, view=view)
                logger.info("패널 메시지 게시: channel=%s", channel.id)
        except discord.HTTPException:
            logger.exception("패널 메시지 전송 실패: channel=%s", channel.id)

    # ---------- 패널 채널의 PDF 자동 진위확인 ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """패널 채널에 올라온 PDF 를 자동 진위확인한다."""
        if message.author.bot or message.guild is None:
            return
        if not message.attachments:
            return

        # 패널 채널에서만 동작
        settings = self.bot.settings
        panel_channel_id = None
        if settings.panel_channel_id:
            panel_channel_id = settings.panel_channel_id
        else:
            ch = await self._resolve_panel_channel(message.guild)
            panel_channel_id = ch.id if ch else None
        if panel_channel_id is None or message.channel.id != panel_channel_id:
            return

        pdfs = [
            a for a in message.attachments if a.filename.lower().endswith(".pdf")
        ]
        if not pdfs:
            return

        if not settings.snuh_truth_enabled:
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
            await self._grant_verified_role(message.guild, message.author.id)

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

    async def _grant_verified_role(self, guild: discord.Guild, user_id: int) -> None:
        role_id = self.bot.settings.verified_role_id
        if not role_id:
            return
        member = guild.get_member(user_id)
        role = guild.get_role(role_id)
        if member and role:
            try:
                await member.add_roles(role, reason="자동 진위확인 인증")
            except discord.Forbidden:
                logger.warning("인증 역할 부여 권한 없음: user=%s", user_id)

    # ---------- 관리자: 패널 수동 재게시 ----------

    @app_commands.command(
        name="패널", description="[관리자] 안내 패널을 이 채널에 (재)게시합니다."
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
        embed = self._build_panel_embed()
        await interaction.channel.send(embed=embed, view=MainPanelView(self.bot))
        await interaction.followup.send("✅ 패널을 게시했습니다.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCog(bot))
