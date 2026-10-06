"""증빙 서류 '지문(fingerprint)' 유틸리티.

개인정보 보호를 위해 증빙 서류(PDF) **원본을 보관하지 않는다.**
대신 서류 텍스트에서 개인 식별 요소(성명·주민번호·문서/발급번호 등)를
제거한 '정규화 텍스트'의 해시(=지문)와, 유사도 비교용 토큰 집합(MinHash)만
저장한다. 이를 통해

- **동일 서류 재사용(중복 제출) 탐지**: 같은 지문이면 완전히 동일한 서류.
- **유사 서류(거의 같은 서류) 탐지**: 토큰 집합 자카드 유사도가 임계값 이상이면
  같은 서류로 판단한다 (스캔본/재발급 등 표현만 다른 경우 포착).

원본 PDF 는 검증 직후 폐기되므로 DB 에는 지문과 유사도 데이터만 남는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

# 유사 서류로 판정하는 자카드 유사도 기본 임계값.
# 주의: 같은 병원의 **동일 서식** 서류는 개인·기간이 달라도 서식 문구가 대부분을
# 차지해 유사도가 0.8 이상으로 나온다. 따라서 '다른 서류'를 중복으로 오판하지
# 않도록 임계값을 매우 높게(거의 동일 문서 수준) 둔다.
# 실질적인 중복 방지는 문서확인번호(고유값)로 처리한다.
DEFAULT_SIMILARITY_THRESHOLD = 0.98

# 유사도 비교에 사용할 토큰(문자 n-gram) 길이
_SHINGLE_SIZE = 4
# 지문에 포함할 최대 토큰 수 (MinHash 서명 길이)
_MAX_SHINGLES = 128

# 개인 식별 요소 제거용 패턴 (지문 계산 전 마스킹)
_SSN_RE = re.compile(r"\b\d{6}\s*-\s*\d{7}\b")
_PHONE_RE = re.compile(r"\b0\d{1,2}\s*-?\s*\d{3,4}\s*-?\s*\d{4}\b")
_LONG_DIGITS_RE = re.compile(r"\d{5,}")
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class DocumentFingerprint:
    """서류 지문 및 유사도 비교용 데이터."""

    # 정규화 텍스트의 SHA-256 해시 (동일 서류 판정용)
    fingerprint: str
    # 토큰(문자 n-gram) MinHash 서명 (유사도 비교용, 최대 _MAX_SHINGLES 개)
    shingles: frozenset[str]

    def similarity(self, other: "DocumentFingerprint") -> float:
        """두 지문의 자카드 유사도 (0.0 ~ 1.0)."""
        if not self.shingles or not other.shingles:
            return 0.0
        intersection = len(self.shingles & other.shingles)
        union = len(self.shingles | other.shingles)
        if union == 0:
            return 0.0
        return intersection / union


def normalize_document_text(text: str) -> str:
    """서류 텍스트에서 개인정보/공백을 제거해 비교 가능한 형태로 만든다."""
    if not text:
        return ""
    cleaned = _SSN_RE.sub(" ", text)
    cleaned = _PHONE_RE.sub(" ", cleaned)
    # 문서번호·발급번호처럼 서류마다 달라지는 긴 숫자열은 제거한다.
    cleaned = _LONG_DIGITS_RE.sub(" ", cleaned)
    # 공백/줄바꿈을 모두 제거해 레이아웃 차이를 무시한다.
    return _WS_RE.sub("", cleaned)


def _shingles(normalized: str, size: int = _SHINGLE_SIZE) -> frozenset[str]:
    """정규화 텍스트에서 문자 n-gram 집합을 만든다 (MinHash 서명)."""
    if not normalized:
        return frozenset()
    if len(normalized) <= size:
        return frozenset({normalized})
    grams = {normalized[i : i + size] for i in range(len(normalized) - size + 1)}
    # MinHash: 해시값이 가장 작은 순서로 최대 _MAX_SHINGLES 개만 보관한다.
    ordered = sorted(grams, key=lambda g: hashlib.sha256(g.encode("utf-8")).digest())
    return frozenset(ordered[:_MAX_SHINGLES])


def fingerprint_text(text: str) -> DocumentFingerprint:
    """서류 텍스트로부터 지문을 계산한다."""
    normalized = normalize_document_text(text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return DocumentFingerprint(fingerprint=digest, shingles=_shingles(normalized))


def encode_shingles(shingles: frozenset[str] | set[str]) -> str:
    """토큰 집합을 DB 저장용 JSON 문자열로 직렬화한다."""
    return json.dumps(sorted(shingles), ensure_ascii=False)


def decode_shingles(raw: str | None) -> frozenset[str]:
    """DB 에 저장된 토큰 JSON 문자열을 집합으로 복원한다."""
    if not raw:
        return frozenset()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(str(item) for item in data)


def is_similar(
    fingerprint: DocumentFingerprint,
    stored_fingerprint: str | None,
    stored_shingles: str | None,
    *,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> bool:
    """저장된 지문과 비교해 동일/유사 서류인지 판정한다.

    - 해시가 완전히 같으면 즉시 True (동일 서류).
    - 그렇지 않으면 MinHash 토큰 자카드 유사도가 임계값 이상이면 True (유사 서류).
    """
    if stored_fingerprint and stored_fingerprint == fingerprint.fingerprint:
        return True
    other = DocumentFingerprint(
        fingerprint=stored_fingerprint or "",
        shingles=decode_shingles(stored_shingles),
    )
    return fingerprint.similarity(other) >= threshold
