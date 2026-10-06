"""입퇴원 사실확인서 파서 테스트."""

from __future__ import annotations

import pytest

from bot.utils.certificate import (
    CertificateParseError,
    is_psychiatric_text,
    parse_certificate_text,
    psychiatric_departments,
)

# 예시 PDF(O3_20261006124557.pdf.pdf)에서 추출한 텍스트를 재현한 샘플
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
    period = cert.period()
    assert period == (2026, 1, 2026, 10)


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
