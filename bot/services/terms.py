"""약관·개인정보 전문 게시 서비스.

인증 전 동의 화면에서 "전문은 관리자에게 문의하세요" 라고 안내하는 대신,
약관/개인정보 처리방침 **전문을 전용 채널에 게시**해 누구나 열람할 수 있게 한다.

- `docs/PRIVACY.md`, `docs/TERMS.md` 파일을 읽어
- 디스코드 메시지 길이 제한(2000자)에 맞게 분할해
- 지정 채널에 순서대로 게시하고
- 메시지 링크(URL)를 반환한다.

`ConsentService.doc_urls()` 는 `.env` 의 URL 설정값을 그대로 반환하므로,
URL 이 비어 있으면 이 서비스가 게시한 메시지 링크를 동의 임베드에 노출한다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import discord

from bot.services.consent import DOC_LABELS, DOC_PRIVACY, DOC_TERMS

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

logger = logging.getLogger(__name__)

# 디스코드 메시지 최대 길이(2000)보다 여유를 둔 분할 크기
_CHUNK_SIZE = 1900

# 문서 종류 → 저장소 루트 기준 파일 경로
DOC_FILES: dict[str, str] = {
    DOC_PRIVACY: "docs/PRIVACY.md",
    DOC_TERMS: "docs/TERMS.md",
}


def split_text(text: str, size: int = _CHUNK_SIZE) -> list[str]:
    """긴 텍스트를 줄바꿈 기준으로 ``size`` 이하 조각으로 나눈다.

    한 줄이 ``size`` 보다 길면 그 줄을 강제로 잘라낸다.
    """
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        while len(line) > size:
            # 너무 긴 줄은 하드 분할한다.
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:size])
            line = line[size:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > size:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text[:size]]


class TermsService:
    """약관·개인정보 전문을 채널에 게시하고 링크를 관리한다."""

    def __init__(self, bot: "MindFinderBot") -> None:
        self.bot = bot
        # 문서 종류 → 게시된 메시지 URL (프로세스 캐시)
        self._urls: dict[str, str] = {}

    # ---------- 파일 읽기 ----------

    def _doc_path(self, doc_type: str) -> Optional[Path]:
        rel = DOC_FILES.get(doc_type)
        if rel is None:
            return None
        return Path(rel)

    def read_doc(self, doc_type: str) -> Optional[str]:
        """문서 전문을 읽는다. 파일이 없으면 None."""
        path = self._doc_path(doc_type)
        if path is None or not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("약관 파일 읽기 실패: %s", path)
            return None

    # ---------- 게시 ----------

    async def _delete_previous(self, channel: discord.TextChannel) -> None:
        """이전에 게시한 약관 메시지를 모두 지운다 (중복 방지).

        약관 채널은 봇 전용(읽기 전용)이므로 봇이 올린 메시지를 전부 지운다.
        """
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id == self.bot.user.id:
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        logger.debug("약관 메시지 삭제 실패: %s", msg.id)
        except discord.HTTPException:
            logger.debug("약관 메시지 조회 실패: channel=%s", channel.id)

    def _build_messages(self) -> list[tuple[str, str]]:
        """(문서종류, 메시지 본문) 목록을 만든다 (첫 조각에만 제목)."""
        result: list[tuple[str, str]] = []
        for doc_type in (DOC_PRIVACY, DOC_TERMS):
            text = self.read_doc(doc_type)
            if text is None:
                continue
            label = DOC_LABELS.get(doc_type, doc_type)
            chunks = split_text(text)
            for idx, chunk in enumerate(chunks):
                content = (
                    f"**📜 MindFinder {label}**\n{chunk}" if idx == 0 else chunk
                )
                result.append((doc_type, content))
        return result

    async def _existing_bot_messages(
        self, channel: discord.TextChannel
    ) -> list[discord.Message]:
        """채널에 있는 봇 메시지를 오래된 순으로 반환한다."""
        messages: list[discord.Message] = []
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id == self.bot.user.id:
                    messages.append(msg)
        except discord.HTTPException:
            logger.debug("약관 메시지 조회 실패: channel=%s", channel.id)
        messages.reverse()
        return messages

    async def post_all(self, guild: discord.Guild) -> dict[str, str]:
        """약관·개인정보 전문을 전용 채널에 게시하고 문서별 URL 을 반환한다.

        내용이 이전과 같으면 **지우지 않고 그대로 둔다** (불필요한 삭제·재게시 방지).
        """
        channel = await self.bot.channel_service.managed_text_channel(
            guild, "ch:terms"
        )
        if channel is None:
            logger.warning("약관 채널을 찾을 수 없습니다: guild=%s", guild.id)
            return {}

        desired = self._build_messages()
        existing = await self._existing_bot_messages(channel)

        # 내용이 완전히 동일하면 그대로 유지하고 기존 링크만 재사용한다.
        if len(existing) == len(desired) and all(
            m.content == content for m, (_dt, content) in zip(existing, desired)
        ):
            urls: dict[str, str] = {}
            for msg, (doc_type, _content) in zip(existing, desired):
                if doc_type not in urls:
                    urls[doc_type] = msg.jump_url
            self._urls.update(urls)
            logger.info("약관 변경 없음 → 재게시 생략: guild=%s", guild.id)
            return urls

        # 변경사항이 있으면 이전 메시지를 모두 지우고 새로 게시한다.
        await self._delete_previous(channel)

        urls = {}
        first_url: dict[str, Optional[str]] = {}
        for doc_type, content in desired:
            try:
                message = await channel.send(content)
            except discord.HTTPException:
                logger.exception("약관 게시 실패: doc=%s", doc_type)
                break
            if doc_type not in first_url:
                first_url[doc_type] = message.jump_url
        for doc_type, url in first_url.items():
            if url is not None:
                urls[doc_type] = url
        self._urls.update(urls)
        logger.info("약관 재게시 완료: guild=%s (%d개 메시지)", guild.id, len(desired))
        return urls

    async def post_all_guilds(self) -> None:
        """모든 길드에 약관을 게시한다."""
        for guild in self.bot.guilds:
            try:
                await self.post_all(guild)
            except Exception:  # noqa: BLE001 - 한 서버 실패가 전체를 막지 않도록
                logger.exception("약관 게시 실패: guild=%s", guild.id)

    # ---------- 조회 ----------

    def cached_url(self, doc_type: str) -> Optional[str]:
        """캐시된 문서 URL (없으면 None)."""
        return self._urls.get(doc_type)

    def doc_urls(self) -> dict[str, Optional[str]]:
        """문서 종류별 URL. `.env` 설정값이 있으면 그것을, 없으면 게시 링크.

        약관 채널은 전체 공개 채널이므로 게시 링크(jump_url)도 누구나 열 수 있다.
        """
        settings = self.bot.settings
        configured = {
            DOC_PRIVACY: settings.privacy_policy_url,
            DOC_TERMS: settings.terms_of_service_url,
        }
        return {
            doc_type: configured.get(doc_type) or self._urls.get(doc_type)
            for doc_type in (DOC_PRIVACY, DOC_TERMS)
        }
