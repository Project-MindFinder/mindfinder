"""입퇴원 사실확인서(PDF) 파싱 유틸리티.

서울대학교병원 등에서 발급하는 '입퇴원 사실확인서' PDF 에서
- 문서번호 / 발급번호
- 진료과(입원과·퇴원과)
- 입퇴원 기간(연·월)
을 추출하고, 정신과(정신건강의학과) 관련 입원인지 판정한다.

PDF 텍스트 추출기는 글자 사이에 공백을 끼워 넣는 경우가 많아
(예: '입 원 과', '소 아 정 신 과') 라벨 매칭 시 공백을 제거한 문자열을 함께 사용한다.
진료과 추출은 줄 단위로 수행해 '입원과퇴원과' 같은 서식 라벨을 걸러낸다.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

# 정신건강의학과 관련 키워드 (진료과 판정)
PSYCHIATRY_KEYWORDS = (
    "정신건강의학과",
    "정신과",
    "소아정신과",
    "청소년정신과",
    "정신의학과",
    "정신병동",
    "폐쇄병동",
)

# 진료과 라벨(실제 진료과가 아니라 서식 라벨) — 진료과 추출 시 제외
_DEPARTMENT_LABELS = {
    "입원과",
    "퇴원과",
    "진료과",
    "입원과퇴원과",
    "진료과및진료일자",
    "입원기간",
    "진료일자",
    "입원기간및진료일자",
}

# 진료과 후보 토큰: 한글 2자 이상 + '과'/'응급실'/'병동' 으로 끝남
_DEPT_TOKEN_RE = re.compile(r"[가-힣]{2,}(?:과|응급실|병동)")

_DOC_NO_RE = re.compile(r"문서번호\s*[:：]?\s*([A-Za-z0-9\-]{6,})")
_ISSUE_NO_RE = re.compile(r"제\s*([0-9]{3,})\s*호")
_YEAR_RE = re.compile(r"(19[89][0-9]|20[0-9]{2})")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_PATIENT_NAME_RE = re.compile(r"성\s*명\s*[:：]?\s*([가-힣*]{2,10})")

# 병원 판별 키워드
HOSPITAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "snuh": ("서울대학교병원", "서울대병원", "분당서울대병원"),
}

# 전각 문자 → 반각 문자 정규화 (０１２３…：)
_FULLWIDTH_MAP = str.maketrans("０１２３４５６７８９：", "0123456789:")


class CertificateParseError(ValueError):
    """PDF 파싱에 실패했을 때 발생."""


@dataclass
class ParsedCertificate:
    """입퇴원 사실확인서에서 추출한 정보."""

    document_no: str | None = None
    issue_no: str | None = None
    hospital_key: str | None = None
    hospital_name: str | None = None
    departments: list[str] = field(default_factory=list)
    is_psychiatric: bool = False
    psychiatric_departments: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    month_days: list[tuple[int, int]] = field(default_factory=list)
    patient_name: str | None = None
    raw_text: str = ""

    def period(self) -> tuple[int, int, int, int] | None:
        """(시작 연도, 시작 월, 종료 연도, 종료 월) 를 추정한다.

        연도가 하나이고 월·일 목록이 있으면 그 연도의 최소/최대 월을 기간으로 본다.
        추정이 불가능하면 None 을 반환한다.
        """
        if not self.years or not self.month_days:
            return None
        months = sorted({m for m, _ in self.month_days if 1 <= m <= 12})
        if not months:
            return None
        if len(self.years) == 1:
            year = self.years[0]
            return year, months[0], year, months[-1]
        # 연도가 여러 개면 정렬해 앞/뒤를 매칭 (근사치)
        years = sorted(self.years)
        return years[0], months[0], years[-1], months[-1]

    def summary(self) -> str:
        """사람이 읽기 쉬운 요약 문자열."""
        dept = ", ".join(self.departments) if self.departments else "확인 불가"
        period = self.period()
        period_text = (
            f"{period[0]}년 {period[1]}월 ~ {period[2]}년 {period[3]}월"
            if period
            else "기간 확인 불가"
        )
        return (
            f"문서번호={self.document_no or '?'} / "
            f"발급번호={self.issue_no or '?'} / "
            f"진료과={dept} / "
            f"기간={period_text} / "
            f"정신과={'예' if self.is_psychiatric else '아니오'}"
        )


def _normalize(text: str) -> str:
    """전각 문자를 반각으로 바꾸고 공백류를 정리한다."""
    return text.translate(_FULLWIDTH_MAP)


def _compact(text: str) -> str:
    """모든 공백/개행을 제거한 압축 문자열."""
    return re.sub(r"\s+", "", text)


def extract_text(pdf_bytes: bytes) -> str:
    """PDF 바이트에서 텍스트를 추출한다."""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 의존성 미설치 시
        raise CertificateParseError(
            "pypdf 가 설치되어 있지 않습니다. requirements 를 설치해주세요."
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - 다양한 PDF 오류를 포괄
        raise CertificateParseError(f"PDF 를 읽을 수 없습니다: {exc}") from exc

    text = _normalize("\n".join(pages)).strip()
    if not text:
        raise CertificateParseError(
            "PDF 에서 텍스트를 추출하지 못했습니다. 스캔 이미지 PDF 는 지원되지 않습니다."
        )
    return text


def _detect_hospital(text: str) -> tuple[str | None, str | None]:
    """텍스트에서 병원을 판별한다."""
    compact = _compact(text)
    for key, keywords in HOSPITAL_KEYWORDS.items():
        for kw in keywords:
            if _compact(kw) in compact:
                return key, kw
    return None, None


def _extract_departments(text: str) -> list[str]:
    """진료과 목록을 추출한다 (서식 라벨 제외, 중복 제거, 순서 유지)."""
    found: list[str] = []
    for line in text.splitlines():
        compact_line = _compact(line)
        if not compact_line:
            continue
        for match in _DEPT_TOKEN_RE.finditer(compact_line):
            token = match.group(0)
            # '입원과퇴원과' 처럼 라벨이 붙어 있으면 라벨 제거
            for label in _DEPARTMENT_LABELS:
                token = token.replace(label, "")
            if len(token) < 2 or not _DEPT_TOKEN_RE.fullmatch(token):
                continue
            if token not in found:
                found.append(token)
    return found


def is_psychiatric_text(text: str) -> bool:
    """텍스트에 정신건강의학과 관련 표현이 있는지 판정한다."""
    compact = _compact(text)
    return any(_compact(kw) in compact for kw in PSYCHIATRY_KEYWORDS)


def psychiatric_departments(departments: list[str]) -> list[str]:
    """진료과 목록 중 정신건강의학과 관련 진료과만 골라낸다."""
    result: list[str] = []
    for dept in departments:
        if is_psychiatric_text(dept):
            result.append(dept)
    return result


def parse_certificate(pdf_bytes: bytes) -> ParsedCertificate:
    """입퇴원 사실확인서 PDF 를 파싱한다.

    Raises:
        CertificateParseError: PDF 를 읽을 수 없거나 서식이 아닐 때.
    """
    text = extract_text(pdf_bytes)
    return parse_certificate_text(text)


def parse_certificate_text(text: str) -> ParsedCertificate:
    """이미 추출된 텍스트에서 사실확인서 정보를 파싱한다 (테스트 용이)."""
    text = _normalize(text)
    compact = _compact(text)

    if "사실확인서" not in compact and "입퇴원" not in compact:
        raise CertificateParseError(
            "입퇴원 사실확인서 형식이 아닙니다. 다른 서류는 관리자 심사를 이용해주세요."
        )

    doc_match = _DOC_NO_RE.search(compact)
    issue_match = _ISSUE_NO_RE.search(compact)
    name_match = _PATIENT_NAME_RE.search(text)

    hospital_key, hospital_name = _detect_hospital(text)
    departments = _extract_departments(text)
    psych_departments = psychiatric_departments(departments)

    years = sorted({int(y) for y in _YEAR_RE.findall(text)})
    month_days = [(int(m), int(d)) for m, d in _MONTH_DAY_RE.findall(text)]

    return ParsedCertificate(
        document_no=doc_match.group(1) if doc_match else None,
        issue_no=issue_match.group(1) if issue_match else None,
        hospital_key=hospital_key,
        hospital_name=hospital_name,
        departments=departments,
        is_psychiatric=bool(psych_departments),
        psychiatric_departments=psych_departments,
        years=years,
        month_days=month_days,
        patient_name=name_match.group(1) if name_match else None,
        raw_text=text,
    )
