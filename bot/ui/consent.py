"""약관·개인정보 동의 UI.

인증(입원 이력 등록/서류 제출)을 진행하기 전에 개인정보 처리방침과
서비스 이용약관에 **필수 동의**를 받는다.

사용 흐름:
  1) 인증 버튼/명령을 누른다.
  2) 아직 동의하지 않았다면 `show_consent` 가 약관 안내 + 동의 버튼을 띄운다.
  3) 동의하면 `on_agree` 콜백이 호출되어 원래 하려던 인증 흐름을 이어간다.

슬래시 커맨드에서는 `ensure_consent(...)` 로 게이트한다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import discord

from bot.services.consent import DOC_LABELS, DOC_PRIVACY, DOC_TERMS

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

ConsentCallback = Callable[[discord.Interaction], Awaitable[None]]

# 약관 요약 (임베드용). 전문은 docs/PRIVACY.md, docs/TERMS.md 참고.
_PRIVACY_SUMMARY = (
    "• 수집: 디스코드 ID, 입원 병원·기간, (선택) 증빙 서류, 매칭 설정\n"
    "• 이용: 매칭·커뮤니티·인증 심사\n"
    "• 연락처(전화번호/SNS)는 저장·전달하지 않습니다.\n"
    "• 증빙 서류는 심사 완료 즉시 파기하며 관리자만 열람합니다.\n"
    "• 매칭 대화 내용은 봇이 저장하지 않습니다."
)
_TERMS_SUMMARY = (
    "• 본인의 실제 입원 이력만 등록해야 합니다 (허위·도용 금지).\n"
    "• 서류 위조·인증 부정 취득 시 인증이 취소될 수 있습니다.\n"
    "• 매칭은 양측 동의 시에만 성사됩니다.\n"
    "• 봇은 의료·법률·상담 서비스를 제공하지 않습니다.\n"
    "• 타인의 개인정보를 동의 없이 공유해서는 안 됩니다."
)


def _url_line(bot: "MindFinderBot", doc_type: str) -> str:
    """문서 전문 링크 문구를 만든다.

    `.env` URL 설정이 없으면 약관 채널에 게시된 메시지 링크를 사용한다.
    (약관 채널은 전체 공개이므로 누구나 열람할 수 있다.)
    """
    url = bot.consent_service.doc_urls().get(doc_type)
    terms = getattr(bot, "terms_service", None)
    if not url and terms is not None:
        url = terms.doc_urls().get(doc_type)
    if url:
        return f"[전문 보기]({url})"
    return "전문은 **📜 이용약관·개인정보처리방침** 채널에서 확인하실 수 있습니다."


def build_consent_embed(bot: "MindFinderBot") -> discord.Embed:
    """인증 전 필수 동의 안내 임베드를 만든다."""
    versions = bot.consent_service.current_versions()
    embed = discord.Embed(
        title="📜 이용약관 및 개인정보 처리방침 동의",
        description=(
            "인증(입원 이력 등록/서류 제출)을 진행하려면 아래 문서에 "
            "**필수적으로 동의**해야 합니다.\n"
            "동의 내용은 기록되며, 약관이 개정되면 다시 동의를 받습니다."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=f"개인정보 처리방침 (v{versions.get(DOC_PRIVACY, '-')})",
        value=f"{_PRIVACY_SUMMARY}\n{_url_line(bot, DOC_PRIVACY)}",
        inline=False,
    )
    embed.add_field(
        name=f"서비스 이용약관 (v{versions.get(DOC_TERMS, '-')})",
        value=f"{_TERMS_SUMMARY}\n{_url_line(bot, DOC_TERMS)}",
        inline=False,
    )
    embed.set_footer(text="아래 '동의하고 계속' 버튼을 눌러야 인증을 진행할 수 있습니다.")
    return embed


class ConsentView(discord.ui.View):
    """인증 전 필수 동의를 받는 뷰 (동의/취소)."""

    def __init__(
        self,
        bot: "MindFinderBot",
        user_id: int,
        on_agree: Optional[ConsentCallback] = None,
        *,
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.user_id = user_id
        self.on_agree = on_agree

    async def _reject_if_not_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "본인만 사용할 수 있습니다.", ephemeral=True
            )
            return True
        return False

    @discord.ui.button(
        label="동의하고 계속", style=discord.ButtonStyle.success, custom_id="mf:consent:agree"
    )
    async def agree(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if await self._reject_if_not_owner(interaction):
            return
        await self.bot.consent_service.record_all(interaction.user.id)
        self.stop()
        if self.on_agree is not None:
            await self.on_agree(interaction)
        else:
            await interaction.response.send_message(
                "✅ 동의가 완료되었습니다.", ephemeral=True
            )

    @discord.ui.button(
        label="동의하지 않음", style=discord.ButtonStyle.secondary, custom_id="mf:consent:decline"
    )
    async def decline(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if await self._reject_if_not_owner(interaction):
            return
        self.stop()
        await interaction.response.send_message(
            "동의하지 않으면 인증 및 인증이 필요한 기능을 이용할 수 없습니다.",
            ephemeral=True,
        )


async def show_consent(
    bot: "MindFinderBot",
    interaction: discord.Interaction,
    on_agree: ConsentCallback,
    *,
    followup: bool = False,
) -> None:
    """동의 안내 + 동의 뷰를 띄운다 (이미 defer 했으면 followup=True)."""
    view = ConsentView(bot, interaction.user.id, on_agree)
    kwargs = {"embed": build_consent_embed(bot), "view": view, "ephemeral": True}
    if followup:
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def ensure_consent(
    bot: "MindFinderBot",
    interaction: discord.Interaction,
    on_agree: ConsentCallback,
    *,
    followup: bool = False,
) -> bool:
    """이용자가 필수 문서에 동의했는지 확인한다.

    동의했다면 True 를 반환하고, 아직이면 동의 뷰를 띄운 뒤 False 를 반환한다.
    `followup` 은 이미 interaction 을 defer 했는지 여부다.
    """
    if await bot.consent_service.has_consented(interaction.user.id):
        return True
    await show_consent(bot, interaction, on_agree, followup=followup)
    return False
