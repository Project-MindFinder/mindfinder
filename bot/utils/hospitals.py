"""병원명 정규화 유틸리티.

사용자가 자유 입력한 병원명을 대표 명칭으로 정규화하여 매칭 정확도를 높인다.
- 공백/특수문자 제거
- 흔한 접미사(의료원, 병원, 정신건강의학과 등) 정리
- 대표 병원 목록 및 별칭 매핑 제공

**병동(예: 7A병동, 62병동)은 병원명 하드코딩이 아니라 DB(hospitals.name)에
포함해 저장한다.** 여기서는 시드 시점의 대표 명칭/별칭만 정의한다.
"""

from __future__ import annotations

import re

# 정규화 시 제거할 법인/지점 표기 및 일반 접미사
_STRIP_TOKENS = [
    "재단법인", "의료법인", "학교법인", "사회복지법인",
    "(주)", "주식회사", "㈜",
]

# 정규화에서 제거할 구두점/공백
_PUNCT_RE = re.compile(r"[\s\.\,\-\_\(\)\[\]\/\\·・]+")


def normalize_name(name: str) -> str:
    """병원명을 정규화한다 (비교/중복 판정용)."""
    text = name.strip()
    for token in _STRIP_TOKENS:
        text = text.replace(token, "")
    text = _PUNCT_RE.sub("", text)
    return text.lower()


def _build_default_hospitals() -> dict[str, list[str]]:
    """기본 시드 병원 목록과 별칭을 정의한다.

    병동까지 포함한 명칭을 **대표명(DB 저장값)** 으로 둔다. 이렇게 하면
    별도의 병동 매핑 테이블 없이도 모든 표시 지점에서 병동이 함께 나온다.
    그 외 병원은 사용자가 패널의 '기타 병원 직접 입력' 으로 요청하면
    관리자가 검토·승인한 뒤 DB 에 추가된다.
    """
    return {
        # 대표 명칭(병동 포함) : [별칭들...]
        "서울대학교어린이병원 7A병동": [
            "서울대학교어린이병원",
            "서울대어린이병원",
            "서울대병원어린이병원",
            "서울대학교어린이병원 7A병동",
            "7A병동",
        ],
    }


def get_default_alias_map() -> dict[str, str]:
    """정규화된 별칭 -> 대표 명칭 매핑을 반환한다."""
    mapping: dict[str, str] = {}
    for canonical, aliases in _build_default_hospitals().items():
        mapping[normalize_name(canonical)] = canonical
        for alias in aliases:
            mapping[normalize_name(alias)] = canonical
    return mapping


def get_default_hospitals() -> list[str]:
    """대표 병원 명칭 목록을 반환한다."""
    return list(_build_default_hospitals().keys())


def resolve_canonical(name: str) -> str:
    """입력 병원명을 대표 명칭으로 변환한다. 매핑에 없으면 입력값을 다듬어 반환한다."""
    normalized = normalize_name(name)
    alias_map = get_default_alias_map()
    if normalized in alias_map:
        return alias_map[normalized]
    # 매핑에 없으면 공백만 정리한 원본을 대표 명칭으로 사용
    return re.sub(r"\s+", " ", name.strip())


def hospital_display_name(name: str) -> str:
    """병원 표시명을 만든다.

    병동이 이미 이름에 포함되어 있으면(DB 저장값) 그대로 반환한다.
    병동 정보가 없는 병원은 이름 그대로 반환한다.
    """
    return resolve_canonical(name)
