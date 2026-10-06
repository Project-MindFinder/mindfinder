"""일반 명령 코그 (도움말, 병원 목록 등)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.core import MindFinderBot
from bot.ui.consent import ConsentView, build_consent_embed
from bot.utils.hospitals import get_default_hospitals

logger = logging.getLogger(__name__)


class GeneralCog(commands.Cog):
    """기본 정보성 명령을 제공한다."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot

    @app_commands.command(name="도움말", description="MindFinder 사용법을 안내합니다.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="MindFinder 사용 안내",
            description=(
                "폐쇄병동 입원 경험자들을 위한 커뮤니티 및 매칭 봇입니다.\n"
                "개인정보 보호를 위해 연락처는 봇이 저장하지 않습니다."
            ),
            color=discord.Color.teal(),
        )
        embed.add_field(
            name="🖱️ 버튼 패널 (권장)",
            value=(
                "서버의 `mindfinder-패널` 채널에 **항상 떠 있는 안내 메시지**의 버튼을\n"
                "누르면 슬래시 커맨드 없이도 인증·매칭을 이용할 수 있습니다.\n"
                "PDF 사실확인서를 그 채널에 올리면 자동 진위확인됩니다.\n"
                "`/패널` — [관리자] 패널 메시지를 다시 게시/갱신"
            ),
            inline=False,
        )
        embed.add_field(
            name="1. 인증",
            value=(
                "`/인증` — 입원 이력을 등록합니다 (자기 신고)\n"
                "`/진위확인` — 서울대병원 입퇴원 사실확인서 PDF 를 자동 검증 후 즉시 인증\n"
                "`/내이력` — 내가 등록한 이력을 조회합니다\n"
                "`/서류제출` — 서류 심사로 인증 배지를 신청합니다 (다른 병원)\n"
                "`/심사목록` `/심사승인` `/심사거절` — [관리자] 심사 처리"
            ),
            inline=False,
        )
        embed.add_field(
            name="2. 매칭",
            value=(
                "`/매칭` — 매칭 희망 여부 설정 (on/off)\n"
                "`/매칭찾기` — 같은 병원·시기 동료를 찾습니다\n"
                "`/매칭신청` — 후보에게 매칭을 신청합니다\n"
                "`/매칭요청함` — 나에게 온 매칭 신청을 확인합니다\n"
                "`/매칭수락` `/매칭거절` — 신청 처리 (수락 시 비공개 채널 생성)"
            ),
            inline=False,
        )
        embed.add_field(
            name="3. 매칭 채널",
            value=(
                "`/매칭채널` — 내가 참여 중인 비공개 채널 목록\n"
                "`/매칭초대` — 동료를 내 매칭 채널에 추가 초대 (여러 명 공유)\n"
                "`/매칭종료` — 매칭 채널 닫기 (기록 보존)"
            ),
            inline=False,
        )
        embed.add_field(
            name="4. 커뮤니티",
            value="`/커뮤니티` — 병원·기간 단위 커뮤니티를 확인합니다",
            inline=False,
        )
        embed.add_field(
            name="5. 약관·개인정보",
            value=(
                "`/약관` — 개인정보 처리방침과 서비스 이용약관을 확인합니다\n"
                "인증(이력 등록/서류 제출) 시 두 문서에 **필수 동의**가 필요합니다."
            ),
            inline=False,
        )
        embed.add_field(
            name="개인정보 보호",
            value=(
                "매칭이 성사되면 봇이 **비공개 채널**을 만들어 당사자들만 초대합니다.\n"
                "전화번호/SNS는 봇이 저장하거나 전달하지 않습니다."
            ),
            inline=False,
        )
        embed.set_footer(text="위기 상황 시 `/도움` 명령으로 상담 안내를 확인하세요.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="약관", description="개인정보 처리방침과 서비스 이용약관을 확인합니다."
    )
    async def terms_command(self, interaction: discord.Interaction) -> None:
        embed = build_consent_embed(self.bot)
        missing = await self.bot.consent_service.missing_docs(interaction.user.id)
        if missing:
            view = ConsentView(self.bot, interaction.user.id)
            await interaction.response.send_message(
                embed=embed, view=view, ephemeral=True
            )
        else:
            embed.set_footer(text="✅ 필수 문서에 모두 동의하셨습니다.")
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="병원목록", description="등록된 대표 병원 목록을 봅니다.")
    async def hospital_list(self, interaction: discord.Interaction) -> None:
        hospitals = get_default_hospitals()
        # 줄바꿈으로 나열
        listing = "\n".join(f"• {name}" for name in hospitals)
        embed = discord.Embed(
            title="대표 병원 목록",
            description=(
                f"{listing}\n\n"
                "목록에 없어도 `/인증 신청`에서 직접 입력할 수 있습니다."
            ),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: MindFinderBot) -> None:
    await bot.add_cog(GeneralCog(bot))
