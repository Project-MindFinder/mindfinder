"""병원명 정규화 유틸리티.

사용자가 자유 입력한 병원명을 대표 명칭으로 정규화하여 매칭 정확도를 높인다.
- 공백/특수문자 제거
- 흔한 접미사(의료원, 병원, 정신건강의학과 등) 정리
- 대표 병원 목록 및 별칭 매핑 제공
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
    """대표 병원 목록과 별칭을 정의한다.

    실제 운영 시에는 관리자가 /병원추가 로 확장하거나,
    이 딕셔너리를 데이터 파일로 분리해 관리할 수 있다.
    """
    return {
        # 대표 명칭 : [별칭들...]
        "국립정신건강센터": ["국정센", "국립정신건강센터", "국립정신병원"],
        "서울특별시립정신병원": ["서울시립정신병원", "시립정신병원"],
        "국립나주병원": ["나주국립병원", "나주병원"],
        "국립춘천병원": ["춘천국립병원", "춘천병원"],
        "국립공주병원": ["공주국립병원", "공주병원"],
        "국립부곡병원": ["부곡국립병원", "부곡병원"],
        "국립소록도병원": ["소록도병원"],
        "서울대학교병원": ["서울대병원", "분당서울대병원", "서울대학교병원"],
        "연세대학교세브란스병원": ["세브란스병원", "세브란스"],
        "삼성서울병원": ["삼성병원", "삼성서울병원"],
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
