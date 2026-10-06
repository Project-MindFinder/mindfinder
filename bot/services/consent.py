"""약관·개인정보 동의 도메인 서비스.

인증(입원 이력 등록/서류 제출)을 진행하려면 개인정보 처리방침과
서비스 이용약관에 **필수 동의**해야 한다. 본 서비스는

- 현재 유효한 문서 버전을 제공하고
- 이용자가 필요한 모든 문서에 동의했는지 확인하며
- 동의 시각을 DB 에 기록한다.

UI(슬래시 커맨드/패널)와 무관하게 재사용할 수 있도록 순수 로직만 담는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

# 문서 종류 식별자
DOC_PRIVACY = "privacy"
DOC_TERMS = "terms"

# 문서 종류 → 사람이 읽는 이름
DOC_LABELS = {
    DOC_PRIVACY: "개인정보 처리방침",
    DOC_TERMS: "서비스 이용약관",
}


class ConsentService:
    """개인정보 처리방침·서비스 이용약관 동의 확인/기록."""

    def __init__(self, bot: "MindFinderBot") -> None:
        self.bot = bot

    # ---------- 버전 ----------

    def current_versions(self) -> dict[str, str]:
        """문서 종류별 현재 버전."""
        settings = self.bot.settings
        return {
            DOC_PRIVACY: settings.privacy_policy_version,
            DOC_TERMS: settings.terms_of_service_version,
        }

    def doc_urls(self) -> dict[str, str | None]:
        """문서 종류별 공개 URL (설정되지 않았으면 None)."""
        settings = self.bot.settings
        return {
            DOC_PRIVACY: settings.privacy_policy_url,
            DOC_TERMS: settings.terms_of_service_url,
        }

    # ---------- 동의 확인/기록 ----------

    async def missing_docs(self, user_id: int) -> list[str]:
        """현재 버전 기준으로 아직 동의하지 않은 문서 종류 목록."""
        missing: list[str] = []
        for doc_type, version in self.current_versions().items():
            if not await self.bot.consents.has_consent(user_id, doc_type, version):
                missing.append(doc_type)
        return missing

    async def has_consented(self, user_id: int) -> bool:
        """필요한 모든 문서에 동의했는지 여부."""
        return not await self.missing_docs(user_id)

    async def record_all(self, user_id: int) -> None:
        """필요한 모든 문서에 대한 동의를 현재 버전으로 기록한다."""
        for doc_type, version in self.current_versions().items():
            await self.bot.consents.record(user_id, doc_type, version)

    async def record(self, user_id: int, doc_types: Iterable[str]) -> None:
        """지정한 문서 종류에 대해서만 동의를 기록한다."""
        versions = self.current_versions()
        for doc_type in doc_types:
            version = versions.get(doc_type)
            if version is None:
                continue
            await self.bot.consents.record(user_id, doc_type, version)
