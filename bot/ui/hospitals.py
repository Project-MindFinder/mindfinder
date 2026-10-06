"""병원 선택 UI (드롭다운 + 병원 추가) 및 서류 인증 공통 처리.

요구사항: 병원은 타이핑이 아니라 **리스트(드롭다운)** 로 보여주고,
목록에 없으면 **추가**할 수 있어야 한다.

사용 흐름:
  1) HospitalSelectView 를 띄운다 (등록된 병원 드롭다운 + "병원 추가" 버튼).
  2) 병원을 고르면 on_select(interaction, hospital_id) 콜백이 호출된다.
     (일반적으로 handle_hospital_selection 을 사용)
  3) 서울대학교병원 → 자동 진위확인 안내, 그 외 → 수동 심사(기간 입력) 모달.
  4) "병원 추가" 로 새 병원을 만들면 드롭다운이 즉시 갱신된다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

# 디스코드 Select 옵션 최대 개수
MAX_OPTIONS = 25

SelectCallback = Callable[[discord.Interaction, int], Awaitable[None]]


# --------------------------------------------------------------------------- #
# 병원 추가 모달
# --------------------------------------------------------------------------- #
class AddHospitalModal(discord.ui.Modal, title="병원 추가"):
    """목록에 없는 병원을 새로 등록한다."""

    이름 = discord.ui.TextInput(
        label="병원 이름",
        placeholder="예: 국립정신건강센터",
        max_length=50,
    )

    def __init__(
        self,
        bot: "MindFinderBot",
        on_added: Optional[Callable[[object], Awaitable[None]]] = None,
    ) -> None:
        super().__init__()
        self.bot = bot
        self.on_added = on_added

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.이름.value).strip()
        if len(name) < 2:
            await interaction.response.send_message(
                "⚠️ 병원 이름을 2자 이상 입력해주세요.", ephemeral=True
            )
            return

        hospital = await self.bot.verification_service.resolve_hospital(name)
        # 새 병원 전용 채널/역할을 확보한다 (실패해도 등록은 유지).
        if interaction.guild is not None:
            try:
                # 역할은 사용자가 커뮤니티 패널에서 선택할 때만 만든다.
                await self.bot.channel_service.ensure_hospital_channel(
                    interaction.guild, hospital.id, hospital.name, with_role=False
                )
            except Exception:  # noqa: BLE001 - 채널 생성 실패가 등록을 막지 않도록
                logger.exception("병원 채널 확보 실패: hospital=%s", hospital.id)

        await interaction.response.send_message(
            f"✅ 병원이 추가되었습니다: **{hospital.name}**", ephemeral=True
        )
        if self.on_added is not None:
            await self.on_added(hospital)


# --------------------------------------------------------------------------- #
# 병원 드롭다운
# --------------------------------------------------------------------------- #
class HospitalSelect(discord.ui.Select):
    """등록된 병원 목록 드롭다운."""

    def __init__(
        self, hospitals: list, on_select: SelectCallback, *, placeholder: str
    ) -> None:
        options = [
            discord.SelectOption(label=h.name[:100], value=str(h.id))
            for h in hospitals[:MAX_OPTIONS]
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="mf:hospital:select",
        )
        self._on_select = on_select

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._on_select(interaction, int(self.values[0]))


class HospitalSelectView(discord.ui.View):
    """병원 드롭다운 + 병원 추가 버튼을 담은 뷰."""

    def __init__(
        self,
        bot: "MindFinderBot",
        on_select: SelectCallback,
        *,
        placeholder: str = "병원을 선택하세요",
        add_label: str = "➕ 병원 추가",
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.on_select = on_select
        self.placeholder = placeholder

        add_button = discord.ui.Button(
            label=add_label, style=discord.ButtonStyle.secondary, row=1
        )
        add_button.callback = self._add_hospital
        self.add_item(add_button)

    async def load(self) -> "HospitalSelectView":
        """드롭다운 옵션을 채운다."""
        await self.reload()
        return self

    async def reload(self) -> None:
        """드롭다운을 최신 병원 목록으로 다시 채운다."""
        for child in list(self.children):
            if isinstance(child, HospitalSelect):
                self.remove_item(child)
        hospitals = list(await self.bot.hospitals.list_all())
        if hospitals:
            self.add_item(
                HospitalSelect(hospitals, self.on_select, placeholder=self.placeholder)
            )

    async def _add_hospital(self, interaction: discord.Interaction) -> None:
        message = interaction.message

        async def _on_added(_hospital: object) -> None:
            await self.reload()
            if message is not None:
                try:
                    await message.edit(view=self)
                except discord.HTTPException:
                    logger.debug("병원 목록 갱신 실패: message=%s", message.id)

        await interaction.response.send_modal(AddHospitalModal(self.bot, _on_added))


# --------------------------------------------------------------------------- #
# 수동 심사 모달 (서울대병원 외 병원)
# --------------------------------------------------------------------------- #
class ManualProofModal(discord.ui.Modal, title="서류 심사 신청"):
    """선택한 병원에 대한 입원 기간을 입력받아 자기 신고 이력을 만들고 심사를 요청한다."""

    입원연도 = discord.ui.TextInput(label="입원 연도", placeholder="예: 2020", max_length=4)
    입원월 = discord.ui.TextInput(label="입원 월 (1~12)", placeholder="예: 3", max_length=2)
    퇴원연도 = discord.ui.TextInput(
        label="퇴원 연도 (입원 중이면 비워두세요)", required=False, max_length=4
    )
    퇴원월 = discord.ui.TextInput(label="퇴원 월 (1~12)", required=False, max_length=2)

    def __init__(self, bot: "MindFinderBot", hospital_id: int, hospital_name: str) -> None:
        super().__init__()
        self.bot = bot
        self.hospital_id = hospital_id
        self.hospital_name = hospital_name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from bot.services.verification import VERIFICATION_LABEL
        from bot.utils.periods import PeriodError

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

        # 자기 신고 등록 → 1단계 역할 부여 + 병원 채널 확보
        if interaction.guild is not None:
            member = interaction.guild.get_member(interaction.user.id)
            if member is not None:
                try:
                    await self.bot.access.grant_tier(interaction.guild, member, 1)
                except Exception:  # noqa: BLE001
                    logger.exception("1단계 역할 부여 실패: user=%s", interaction.user.id)
            try:
                # 역할은 사용자가 커뮤니티 패널에서 선택할 때만 만든다.
                await self.bot.channel_service.ensure_hospital_channel(
                    interaction.guild, self.hospital_id, hospital_name, with_role=False
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


# --------------------------------------------------------------------------- #
# 병원 선택 공통 처리 (서울대병원=자동 / 그 외=수동)
# --------------------------------------------------------------------------- #
async def handle_hospital_selection(
    bot: "MindFinderBot", interaction: discord.Interaction, hospital_id: int
) -> None:
    """드롭다운에서 병원을 고른 뒤의 공통 처리."""
    hospital = await bot.hospitals.get_by_id(hospital_id)
    if hospital is None:
        await interaction.response.send_message(
            "병원 정보를 찾을 수 없습니다.", ephemeral=True
        )
        return

    if bot.verification_service.supports_auto_verify(hospital.name):
        await interaction.response.send_message(
            f"🏥 **{hospital.name}** 은(는) 자동 진위확인을 지원합니다.\n"
            "`/진위확인` 명령 또는 패널 채널에 **입퇴원 사실확인서 PDF** 를 올려주세요.\n"
            "자동으로 진위·정신과 여부를 확인해 인증해드립니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_modal(
        ManualProofModal(bot, hospital.id, hospital.name)
    )
