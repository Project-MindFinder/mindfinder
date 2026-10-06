"""입퇴원 사실확인서(PDF) 파싱 유틸리티.

서울대학교병원 등에서 발급하는 '입퇴원 사실확인서' PDF 에서
- 문서번호 / 발급번호
- 진료과(입원과·퇴원과)
- 입원 기간(연·월) — 여러 건일 수 있음
을 추출하고, 정신과(정신건강의학과) 관련 입원인지 판정한다.

PDF 텍스트 추출기는 글자 사이에 공백을 끼워 넣는 경우가 많아
(예: '입 원 과', '소 아 정 신 과') 라벨 매칭 시 공백을 제거한 문자열을 함께 사용한다.
진료과 추출은 줄 단위로 수행해 '입원과퇴원과' 같은 서식 라벨을 걸러낸다.

여러 건의 입원 이력
--------------------
한 사람이 여러 번, 여러 병동/병원에 입원했을 수 있다. 문서의 '입원 기간' 열에서
'~'(물결표)로 표시된 행이 실제 입원 시작/종료에 해당하며, 행이 여러 개면
입원 이력도 여러 건이다. 각 이력은 [`AdmissionEntry`] 로 정규화하고
[`ParsedCertificate.admissions`] 에 담는다.

레이아웃(layout) 추출
---------------------
plain 모드에서는 연도(2026)와 '월/일'이 서로 다른 줄로 분리되어 나온다.
pypdf 의 ``extraction_mode="layout"`` 로 표를 행 단위로 정렬해 뽑는 것을 우선하되,
아래 샘플 PDF 처럼 layout 모드가 빈 문자열을 반환하는 경우에는 plain 으로 폴백한다.

실제 샘플(O3_20261006124557.pdf.pdf)의 표를 좌표 기준으로 정렬하면 아래와 같다::

    [진료일자] 2026 년 10 월 06 일
    [입원 1]  소아정신과  2026 년 05 월 11 일 ~ 2026 년 07 월 30 일
    [입원 2]  소아응급실  2026 년 01 월 24 일 ~ 2026 년 01 월 26 일

즉 '~' 가 있는 2개 행이 실제 입원 이력이다. plain 으로 폴백하면 연도와
'월/일'이 분리되므로, 순서대로 짝지어 '~'(종료) 표식을 기준으로 이력을 복원한다.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

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

# 진료과 후보 토큰: 한글 2~8자 + '과'/'응급실'/'병동' 으로 끝남.
# 비탐욕(?)으로 매칭해 '소아정신과소아정신과' 같은 인접 중복을 각각 분리한다.
_DEPT_TOKEN_RE = re.compile(r"[가-힣]{2,8}?(?:과|응급실|병동)")

# 문서확인번호: '문서번호 : H26O...' 형태. 뒤따르는 서식 텍스트는 별도로 제거한다.
_DOC_NO_RE = re.compile(r"문서\s*번호\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9\-]*)")
# 문서확인번호 뒤에 붙는 'Page1' / '페이지1' 등 서식 텍스트를 잘라낸다.
_DOC_NO_TRAIL_RE = re.compile(r"(?i)(page|페이지)\d*$")
_ISSUE_NO_RE = re.compile(r"제\s*([0-9]{3,})\s*호")
_YEAR_RE = re.compile(r"(19[89][0-9]|20[0-9]{2})")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# '년 MM월 DD일' / 'MM월 DD일' 형태 (연도가 앞에 오는 표기 포함)
_MONTH_DAY_ALT_RE = re.compile(r"(?:(\d{2,4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 독립된 4자리 연도 토큰 (앞뒤에 숫자가 붙지 않아야 함). '~' 접두 여부를 함께 잡는다.
_YEAR_TOKEN_RE = re.compile(r"(?<![0-9])(?P<tilde>~)?\s*(?P<year>(?:19|20)\d{2})(?![0-9])")
# 한 줄에 입원 시작/종료가 함께 있는(레이아웃 정렬된) 경우: 'Y년 M월 [D일] ~ Y년 M월'
_INLINE_PERIOD_RE = re.compile(
    r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월(?:\s*\d{1,2}\s*일)?"
    r"\s*[~\-–]\s*"
    r"((?:19|20)\d{2})\s*년\s*(\d{1,2})\s*월"
)
_PATIENT_NAME_RE = re.compile(r"성\s*명\s*[:：]?\s*([가-힣*]{2,10})")

# 병원 판별 키워드
# 주의: 어린이병원(7A병동)을 먼저 검사해야 '서울대학교' 공통 접두사에 가려지지 않는다.
HOSPITAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 어린이병원(7A병동)을 먼저 검사한다. '서울대학교병원 어린이병원'처럼
    # 공백이 섞인 표기도 어린이병원으로 인식되도록 변형을 포함한다.
    "snuh_child": (
        "서울대학교어린이병원",
        "서울대학교병원어린이병원",
        "서울대어린이병원",
        "서울대병원어린이병원",
        "어린이병원",
    ),
    "snuh": ("서울대학교병원", "서울대병원", "분당서울대병원"),
}

# 전각 문자 → 반각 문자 정규화 (０１２３…：)
_FULLWIDTH_MAP = str.maketrans("０１２３４５６７８９：", "0123456789:")


class CertificateParseError(ValueError):
    """PDF 파싱에 실패했을 때 발생."""


@dataclass
class AdmissionEntry:
    """입원 이력 한 건 (문서의 '입원 기간' 행 기준).

    퇴원 정보가 없으면(입원 중) ``discharge_year``/``discharge_month`` 가 None 이다.
    """

    admit_year: int
    admit_month: int
    discharge_year: int | None = None
    discharge_month: int | None = None
    # 해당 행에서 확인된 진료과(입원과/퇴원과). 매핑이 불가하면 None.
    department: str | None = None

    def period(self) -> tuple[int, int, int, int]:
        """(시작 연, 시작 월, 종료 연, 종료 월) 튜플. 종료가 없으면 시작값을 쓴다."""
        return (
            self.admit_year,
            self.admit_month,
            self.discharge_year if self.discharge_year is not None else self.admit_year,
            self.discharge_month
            if self.discharge_month is not None
            else self.admit_month,
        )

    def text(self) -> str:
        """사람이 읽기 쉬운 기간 문자열."""
        if self.discharge_year is not None and self.discharge_month is not None:
            end = f"{self.discharge_year}년 {self.discharge_month}월"
        else:
            end = "진행 중"
        dept = f" ({self.department})" if self.department else ""
        return f"{self.admit_year}년 {self.admit_month}월 ~ {end}{dept}"


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
    # '~' 로 표시된 입원 기간 행에서 복원한 입원 이력 목록 (여러 건 가능).
    admissions: list[AdmissionEntry] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    month_days: list[tuple[int, int]] = field(default_factory=list)
    # '~' 로 표시된 입원 기간의 월 목록 (admissions 가 없을 때 period() 폴백용)
    period_months: list[int] = field(default_factory=list)
    patient_name: str | None = None
    raw_text: str = ""

    def period(self) -> tuple[int, int, int, int] | None:
        """대표 입원 기간 (시작 연도, 시작 월, 종료 연도, 종료 월).

        여러 건이면 **가장 이른 입원**(admissions[0])을 대표로 사용한다.
        admissions 가 없으면 '~' 표시 월 또는 연·월 최소/최대로 추정한다.
        """
        # 1) '입원 기간' 행에서 복원한 실제 이력 우선
        if self.admissions:
            return self.admissions[0].period()

        # 2) '~' 로 표시된 입원 기간 월로 추정
        if self.period_months:
            months = [m for m in self.period_months if 1 <= m <= 12]
            if months:
                start_month, end_month = months[0], months[-1]
                if self.years:
                    start_year = self.years[0]
                    end_year = self.years[-1]
                else:
                    start_year = end_year = 0
                return start_year, start_month, end_year, end_month

        # 3) 폴백: 전체 월 최소/최대
        if not self.years or not self.month_days:
            return None
        months = sorted({m for m, _ in self.month_days if 1 <= m <= 12})
        if not months:
            return None
        if len(self.years) == 1:
            year = self.years[0]
            return year, months[0], year, months[-1]
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
        if self.admissions:
            admissions_text = " / ".join(a.text() for a in self.admissions)
        else:
            admissions_text = period_text
        return (
            f"문서번호={self.document_no or '?'} / "
            f"발급번호={self.issue_no or '?'} / "
            f"진료과={dept} / "
            f"입원={admissions_text} / "
            f"정신과={'예' if self.is_psychiatric else '아니오'}"
        )


def _normalize(text: str) -> str:
    """전각 문자를 반각으로 바꾸고 공백류를 정리한다."""
    return text.translate(_FULLWIDTH_MAP)


def _compact(text: str) -> str:
    """모든 공백/개행을 제거한 압축 문자열."""
    return re.sub(r"\s+", "", text)


def _extract_pages(reader, *, mode: str) -> list[str]:
    """페이지별 텍스트를 추출한다 (mode: 'layout' | 'plain').

    일부 PDF 는 layout 모드에서 예외를 던지거나 빈 값을 반환하므로,
    페이지 단위로 실패를 흡수하고 빈 문자열로 대체한다.
    """
    pages: list[str] = []
    for page in reader.pages:
        try:
            if mode == "layout":
                pages.append(page.extract_text(extraction_mode="layout") or "")
            else:
                pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - 페이지별 실패는 무시하고 계속
            logger.debug("PDF 텍스트 추출 실패(mode=%s)", mode, exc_info=True)
            pages.append("")
    return pages


def extract_text(pdf_bytes: bytes) -> str:
    """PDF 바이트에서 텍스트를 추출한다.

    1) pypdf 의 layout 모드로 표를 행 단위로 정렬해 뽑는다.
    2) layout 결과가 비어 있으면 plain 모드로 폴백한다.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 의존성 미설치 시
        raise CertificateParseError(
            "pypdf 가 설치되어 있지 않습니다. requirements 를 설치해주세요."
        ) from exc

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        layout_text = _normalize("\n".join(_extract_pages(reader, mode="layout"))).strip()
        if layout_text:
            logger.debug("layout 모드로 텍스트 추출: %d자", len(layout_text))
            text = layout_text
        else:
            # layout 모드가 빈 값을 내는 PDF 는 plain 으로 폴백한다.
            logger.debug("layout 결과 없음 → plain 모드로 폴백")
            text = _normalize("\n".join(_extract_pages(reader, mode="plain"))).strip()
    except Exception as exc:  # noqa: BLE001 - 다양한 PDF 오류를 포괄
        raise CertificateParseError(f"PDF 를 읽을 수 없습니다: {exc}") from exc

    if not text:
        raise CertificateParseError(
            "PDF 에서 텍스트를 추출하지 못했습니다. 스캔 이미지 PDF 는 지원되지 않습니다."
        )
    return text


def _extract_document_no(text: str) -> str | None:
    """문서확인번호를 추출·정규화한다.

    'Page1' / '페이지1' 등 문서확인번호 뒤에 붙은 서식 텍스트와
    한글 라벨을 제거하고 영문/숫자만 남긴다 (예: 'H26O31011206142319').
    """
    match = _DOC_NO_RE.search(text)
    if match is None:
        return None
    # 영문/숫자만 남긴다 (한글 라벨·구두점 제거)
    cleaned = re.sub(r"[^A-Za-z0-9]", "", match.group(1))
    # 뒤에 붙은 'Page1' 등 서식 텍스트 제거
    cleaned = _DOC_NO_TRAIL_RE.sub("", cleaned)
    cleaned = cleaned.strip("-")
    return cleaned[:40] or None


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


def _line_department(line: str, departments: list[str]) -> str | None:
    """한 줄에서 진료과를 찾는다 (레이아웃 정렬 행의 진료과 매핑용)."""
    compact_line = _compact(line)
    if not compact_line:
        return None
    for dept in departments:
        if _compact(dept) in compact_line:
            return dept
    match = _DEPT_TOKEN_RE.search(compact_line)
    if match is None:
        return None
    token = match.group(0)
    for label in _DEPARTMENT_LABELS:
        token = token.replace(label, "")
    if token and _DEPT_TOKEN_RE.fullmatch(token):
        return token
    return None


def _valid_admission(entry: AdmissionEntry) -> bool:
    """월 값이 유효한 입원 이력인지 확인한다."""
    if not (1 <= entry.admit_month <= 12):
        return False
    if entry.discharge_month is not None and not (1 <= entry.discharge_month <= 12):
        return False
    return True


def _sort_admissions(entries: list[AdmissionEntry]) -> list[AdmissionEntry]:
    """입원 이력을 시간순(가장 이른 입원 먼저)으로 정렬한다."""
    return sorted(
        entries,
        key=lambda e: (
            e.admit_year,
            e.admit_month,
            e.discharge_year or 0,
            e.discharge_month or 0,
        ),
    )


def _extract_admissions_from_tokens(text: str) -> list[AdmissionEntry]:
    """plain 텍스트에서 연도/월·일 토큰을 짝지어 입원 이력을 복원한다.

    plain 모드에서는 연도(2026)와 '월/일'이 분리되어 나오고, '~' 가 붙은 연도가
    종료를 나타낸다. 월/일 토큰과 연도 토큰을 순서대로 짝지은 뒤, '~'(종료) 표식을
    기준으로 시작~종료 쌍을 만든다.
    """
    points = [(int(m), int(d)) for m, d in _MONTH_DAY_RE.findall(text)]
    if not points:
        return []

    year_tokens = [(int(y), bool(t)) for t, y in _YEAR_TOKEN_RE.findall(text)]
    # 개수가 맞지 않으면 연도 정보를 신뢰할 수 없으므로 이력 복원을 포기한다.
    if len(points) != len(year_tokens):
        logger.debug(
            "입원 이력 복원 생략: 월/일 토큰 %d개, 연도 토큰 %d개",
            len(points),
            len(year_tokens),
        )
        return []

    dated = [
        (yy, mm, tilde)
        for (mm, _dd), (yy, tilde) in zip(points, year_tokens)
        if 1 <= mm <= 12
    ]
    starts = [p for p in dated if not p[2]]
    ends = [p for p in dated if p[2]]
    if not ends:
        return []

    # 종료('~') 개수만큼 시작을 뒤에서부터 짝지어 이력을 만든다.
    # (진료일자처럼 '~' 가 없는 단독 날짜는 시작 후보에서 자연히 제외된다.)
    count = len(ends)
    starts_used = starts[-count:] if len(starts) >= count else starts

    entries: list[AdmissionEntry] = []
    for (start_year, start_month, _), (end_year, end_month, _) in zip(
        starts_used, ends
    ):
        entry = AdmissionEntry(
            admit_year=start_year,
            admit_month=start_month,
            discharge_year=end_year,
            discharge_month=end_month,
        )
        if _valid_admission(entry):
            entries.append(entry)
    return _sort_admissions(entries)


def _extract_admissions(text: str, departments: list[str]) -> list[AdmissionEntry]:
    """문서에서 입원 이력 목록을 추출한다 (여러 건 가능)."""
    # 1) 레이아웃 정렬 행: 한 줄에 시작~종료가 모두 있는 경우
    entries: list[AdmissionEntry] = []
    for line in text.splitlines():
        for match in _INLINE_PERIOD_RE.finditer(line):
            entry = AdmissionEntry(
                admit_year=int(match.group(1)),
                admit_month=int(match.group(2)),
                discharge_year=int(match.group(3)),
                discharge_month=int(match.group(4)),
                department=_line_department(line, departments),
            )
            if _valid_admission(entry) and entry not in entries:
                entries.append(entry)
    if entries:
        return _sort_admissions(entries)

    # 2) 폴백: plain 텍스트의 연도/월·일 토큰을 짝지어 복원
    return _extract_admissions_from_tokens(text)


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

    document_no = _extract_document_no(text)
    issue_match = _ISSUE_NO_RE.search(compact)
    name_match = _PATIENT_NAME_RE.search(text)

    hospital_key, hospital_name = _detect_hospital(text)
    departments = _extract_departments(text)
    psych_departments = psychiatric_departments(departments)

    # '입원 기간' 행에서 입원 이력을 복원한다 (여러 건 가능).
    admissions = _extract_admissions(text, departments)

    # '년 MM월 DD일' / 'MM월 DD일' 를 연도와 함께 뽑는다 (진단/요약 보조용).
    dated: list[tuple[int, int, int]] = []  # (year, month, day)
    for y, m, d in _MONTH_DAY_ALT_RE.findall(text):
        mm, dd = int(m), int(d)
        if 1 <= mm <= 12 and 1 <= dd <= 31:
            dated.append((int(y) if y else 0, mm, dd))
    month_days = [(m, d) for _y, m, d in dated]
    if not month_days:
        month_days = [(int(m), int(d)) for m, d in _MONTH_DAY_RE.findall(text)]

    explicit_years = sorted({y for y, _m, _d in dated if y})
    years = explicit_years or sorted({int(y) for y in _YEAR_RE.findall(text)})

    # '~' 로 표시된 줄에서 기간 월을 모은다 (admissions 부재 시 폴백).
    period_months: list[int] = []
    for line in text.splitlines():
        if "~" not in line:
            continue
        for m, _d in _MONTH_DAY_RE.findall(line):
            mi = int(m)
            if 1 <= mi <= 12 and mi not in period_months:
                period_months.append(mi)
    if not period_months and explicit_years:
        period_months = [m for _y, m, _d in dated if _y]

    # 입원 이력이 있으면 years/month_days 를 이력 기준으로 보강한다.
    if admissions:
        years = sorted(
            {
                value
                for entry in admissions
                for value in (entry.admit_year, entry.discharge_year)
                if value
            }
        ) or years

    return ParsedCertificate(
        document_no=document_no,
        issue_no=issue_match.group(1) if issue_match else None,
        hospital_key=hospital_key,
        hospital_name=hospital_name,
        departments=departments,
        is_psychiatric=bool(psych_departments),
        psychiatric_departments=psych_departments,
        admissions=admissions,
        years=years,
        month_days=month_days,
        period_months=period_months,
        patient_name=name_match.group(1) if name_match else None,
        raw_text=text,
    )
