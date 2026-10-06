"""입퇴원 사실확인서 파서 테스트."""

from __future__ import annotations

import pypdf
import pytest

from bot.utils import certificate
from bot.utils.certificate import (
    AdmissionEntry,
    CertificateParseError,
    extract_text,
    is_psychiatric_text,
    parse_certificate_text,
    psychiatric_departments,
)

# 예시 PDF(O3_20261006124557.pdf.pdf)의 plain 추출 텍스트를 재현한 샘플.
# 실제 문서는 '입원 기간' 열에 '~' 로 표시된 2건의 입원 이력을 가진다.
SAMPLE_TEXT = """입 퇴 원 사 실 확 인 서
문서번호 : H26O31004506124556
제 1132147 호
1
계 : 2건
Page
 진찰권 번호 :
 성
 주민등록번호:
진료과 및 진료일자
 명  : *******
*******
입 원 과 퇴 원 과

소아정신과
입 원 기 간
1.
2.
3.
4.
*******
소아응급실
소아정신과
년  10 월  06 일
년  01 월  24 일
년  05 월  11 일
년  01 월  26 일
년  07 월  30 일
(PDER)
2026
2026
2026
~ 2026
~ 2026
서 울 대 학 교 병 원 장
* 병명은 기재되지 않으니 병명이 필요한 경우에는 별도로 진단서를 발급받으십시오.
"""

# layout 모드로 표가 행 단위 정렬되어 나온 경우의 샘플 (진료과 + 시작~종료가 한 줄).
LAYOUT_TEXT = """입 퇴 원 사 실 확 인 서
문서번호 : H26O31011206142319
제 1132147 호
계 : 2건
입 원 과  퇴 원 과  입 원 기 간
소아정신과   2026 년 05 월 11 일  ~ 2026 년 07 월 30 일
소아응급실   2026 년 01 월 24 일  ~ 2026 년 01 월 26 일
서 울 대 학 교 병 원 장
"""


def test_parse_document_and_issue_no() -> None:
    cert = parse_certificate_text(SAMPLE_TEXT)
    assert cert.document_no == "H26O31004506124556"
    assert cert.issue_no == "1132147"


def test_detect_snuh_hospital() -> None:
    cert = parse_certificate_text(SAMPLE_TEXT)
    assert cert.hospital_key == "snuh"
    assert cert.hospital_name == "서울대학교병원"


def test_departments_exclude_form_labels() -> None:
    cert = parse_certificate_text(SAMPLE_TEXT)
    # '입원과퇴원과' 라벨은 진료과가 아니므로 제외되어야 한다.
    assert "입원과퇴원과" not in cert.departments
    assert "입원과" not in cert.departments
    assert "퇴원과" not in cert.departments
    # 실제 진료과는 포함
    assert "소아정신과" in cert.departments
    assert "소아응급실" in cert.departments


def test_psychiatry_detection() -> None:
    cert = parse_certificate_text(SAMPLE_TEXT)
    assert cert.is_psychiatric is True
    assert "소아정신과" in cert.psychiatric_departments


def test_period_estimation() -> None:
    cert = parse_certificate_text(SAMPLE_TEXT)
    # period() 는 대표(가장 이른) 입원 이력을 반환한다.
    # 실제 입원은 01월(응급실)과 05월(정신과) 두 건이며, 대표는 01월 건이다.
    period = cert.period()
    assert period == (2026, 1, 2026, 1)


def test_not_a_certificate_raises() -> None:
    with pytest.raises(CertificateParseError):
        parse_certificate_text("이것은 아무 관련 없는 문서입니다.")


def test_is_psychiatric_text_handles_spacing() -> None:
    # 글자 사이에 공백이 끼어 있어도 정신과로 판정해야 한다.
    assert is_psychiatric_text("소 아 정 신 과") is True
    assert is_psychiatric_text("정신건강의학과") is True
    assert is_psychiatric_text("정형외과") is False


def test_psychiatric_departments_filter() -> None:
    depts = ["소아정신과", "소아응급실", "정신건강의학과", "내과"]
    assert psychiatric_departments(depts) == ["소아정신과", "정신건강의학과"]


# --------------------------------------------------------------------------- #
# 여러 건의 입원 이력
# --------------------------------------------------------------------------- #
def test_multiple_admissions_from_plain_text() -> None:
    """plain 텍스트(연도/월·일 분리)에서 2건의 입원 이력을 복원한다."""
    cert = parse_certificate_text(SAMPLE_TEXT)
    assert len(cert.admissions) == 2

    periods = [a.period() for a in cert.admissions]
    # 시간순 정렬: 01월 건이 먼저, 05월 건이 다음
    assert periods == [(2026, 1, 2026, 1), (2026, 5, 2026, 7)]


def test_admission_entries_details() -> None:
    """AdmissionEntry 의 연·월 필드가 정확히 채워진다."""
    cert = parse_certificate_text(SAMPLE_TEXT)
    first = cert.admissions[0]
    assert (first.admit_year, first.admit_month) == (2026, 1)
    assert (first.discharge_year, first.discharge_month) == (2026, 1)

    second = cert.admissions[1]
    assert (second.admit_year, second.admit_month) == (2026, 5)
    assert (second.discharge_year, second.discharge_month) == (2026, 7)


def test_multiple_admissions_from_layout_rows() -> None:
    """layout 정렬 행(진료과 + 시작~종료 한 줄)에서 이력과 진료과를 매핑한다."""
    cert = parse_certificate_text(LAYOUT_TEXT)
    assert len(cert.admissions) == 2

    by_dept = {a.department: a.period() for a in cert.admissions}
    assert by_dept["소아응급실"] == (2026, 1, 2026, 1)
    assert by_dept["소아정신과"] == (2026, 5, 2026, 7)


def test_admission_entry_text() -> None:
    entry = AdmissionEntry(
        admit_year=2026,
        admit_month=5,
        discharge_year=2026,
        discharge_month=7,
        department="소아정신과",
    )
    assert entry.text() == "2026년 5월 ~ 2026년 7월 (소아정신과)"


def test_summary_includes_admissions() -> None:
    cert = parse_certificate_text(SAMPLE_TEXT)
    summary = cert.summary()
    assert "입원=" in summary
    assert "2026년 1월 ~ 2026년 1월" in summary
    assert "2026년 5월 ~ 2026년 7월" in summary


def test_document_no_strips_page_suffix() -> None:
    """문서확인번호 뒤에 'Page1' 등이 붙어도 제거한다."""
    text = "입 퇴 원 사 실 확 인 서\n문서번호 : H26O31011206142319Page1\n"
    cert = parse_certificate_text(text)
    assert cert.document_no == "H26O31011206142319"


# --------------------------------------------------------------------------- #
# extract_text: layout 우선 + plain 폴백
# --------------------------------------------------------------------------- #
class _FakePage:
    """layout/plain 결과를 지정할 수 있는 가짜 페이지."""

    def __init__(self, *, layout: str = "", plain: str = "") -> None:
        self._layout = layout
        self._plain = plain

    def extract_text(self, extraction_mode: str = "plain", **kwargs) -> str:  # noqa: ANN003
        return self._layout if extraction_mode == "layout" else self._plain


class _FakeReader:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages


def _patch_reader(monkeypatch: pytest.MonkeyPatch, pages: list[_FakePage]) -> None:
    monkeypatch.setattr(pypdf, "PdfReader", lambda _stream: _FakeReader(pages))


def test_extract_text_prefers_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reader(monkeypatch, [_FakePage(layout="입퇴원 사실확인서", plain="무시됨")])
    assert extract_text(b"%PDF-1.4") == "입퇴원 사실확인서"


def test_extract_text_falls_back_to_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    # layout 결과가 비어 있으면 plain 으로 폴백해야 한다.
    _patch_reader(monkeypatch, [_FakePage(layout="", plain="입퇴원 사실확인서")])
    assert extract_text(b"%PDF-1.4") == "입퇴원 사실확인서"


def test_extract_text_raises_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_reader(monkeypatch, [_FakePage(layout="", plain="")])
    with pytest.raises(CertificateParseError):
        extract_text(b"%PDF-1.4")
