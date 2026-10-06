"""병원명 정규화 로직 테스트."""

from __future__ import annotations

from bot.utils.hospitals import (
    hospital_display_name,
    normalize_name,
    resolve_canonical,
)


def test_normalize_removes_spaces_and_punct() -> None:
    assert normalize_name("서울 대학교 병원") == "서울대학교병원"
    assert normalize_name("국립정신건강센터(서울)") == "국립정신건강센터서울"


def test_normalize_removes_corporate_tokens() -> None:
    assert normalize_name("(주)마음병원") == "마음병원"
    assert normalize_name("의료법인 좋은병원") == "좋은병원"


def test_resolve_canonical_alias() -> None:
    # 기본 시드는 '서울대학교어린이병원 7A병동'(병동 포함 대표명) 하나다.
    assert resolve_canonical("서울대어린이병원") == "서울대학교어린이병원 7A병동"
    assert resolve_canonical("서울대학교어린이병원") == "서울대학교어린이병원 7A병동"
    # 시드에 없는 병원은 공백만 정리해 그대로 반환한다.
    assert resolve_canonical("세브란스병원") == "세브란스병원"
    assert resolve_canonical("국립정신건강센터") == "국립정신건강센터"
    # 서울대병원(62병동)은 기본 시드에서 제외되어 더 이상 별칭이 아니다.
    assert resolve_canonical("서울대병원") == "서울대병원"


def test_resolve_canonical_unknown_passthrough() -> None:
    # 매핑에 없는 병원은 공백만 정리해 반환
    assert resolve_canonical("동네  마음의원") == "동네 마음의원"


def test_child_hospital_is_separate_from_main() -> None:
    # 서울대학교어린이병원(7A병동)은 서울대학교병원(62병동)과 다른 병원이다.
    assert resolve_canonical("서울대어린이병원") == "서울대학교어린이병원 7A병동"
    assert resolve_canonical("서울대병원어린이병원") == "서울대학교어린이병원 7A병동"
    # 일반 서울대병원은 어린이병원으로 잘못 매핑되지 않아야 한다.
    assert resolve_canonical("서울대병원") != resolve_canonical("서울대어린이병원")


def test_hospital_display_name_includes_ward() -> None:
    # 대표명에 병동이 포함되어 저장되므로 표시명에도 병동이 함께 나온다.
    assert hospital_display_name("서울대어린이병원") == "서울대학교어린이병원 7A병동"
    # 병동 정보가 없는 병원은 이름 그대로 반환한다.
    assert hospital_display_name("국립정신건강센터") == "국립정신건강센터"
