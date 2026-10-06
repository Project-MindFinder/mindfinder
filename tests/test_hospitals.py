"""병원명 정규화 로직 테스트."""

from __future__ import annotations

from bot.utils.hospitals import normalize_name, resolve_canonical


def test_normalize_removes_spaces_and_punct() -> None:
    assert normalize_name("서울 대학교 병원") == "서울대학교병원"
    assert normalize_name("국립정신건강센터(서울)") == "국립정신건강센터서울"


def test_normalize_removes_corporate_tokens() -> None:
    assert normalize_name("(주)마음병원") == "마음병원"
    assert normalize_name("의료법인 좋은병원") == "좋은병원"


def test_resolve_canonical_alias() -> None:
    assert resolve_canonical("세브란스병원") == "연세대학교세브란스병원"
    assert resolve_canonical("서울대병원") == "서울대학교병원"
    assert resolve_canonical("국정센") == "국립정신건강센터"


def test_resolve_canonical_unknown_passthrough() -> None:
    # 매핑에 없는 병원은 공백만 정리해 반환
    assert resolve_canonical("동네  마음의원") == "동네 마음의원"
