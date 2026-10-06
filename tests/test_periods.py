"""입원 기간 겹침 판정 로직 테스트."""

from __future__ import annotations

from bot.db.models import Admission


def make_admission(
    admit_year: int,
    admit_month: int,
    discharge_year: int | None,
    discharge_month: int | None,
    is_ongoing: bool = False,
) -> Admission:
    """테스트용 Admission 객체를 생성한다 (DB 없이 도메인 로직만 검증)."""

    def ym(y: int, m: int) -> int:
        return y * 12 + (m - 1)

    return Admission(
        id=0,
        user_id=1,
        hospital_id=1,
        admit_year=admit_year,
        admit_month=admit_month,
        discharge_year=discharge_year,
        discharge_month=discharge_month,
        is_ongoing=is_ongoing,
        admit_ym=ym(admit_year, admit_month),
        discharge_ym=None if is_ongoing else ym(discharge_year, discharge_month),
        verification="self_reported",
    )


def test_overlap_same_month() -> None:
    a = make_admission(2020, 3, 2020, 5)
    b = make_admission(2020, 5, 2020, 7)
    # 5월이 양쪽에 포함되므로 겹침
    assert a.overlaps(b) is True
    assert b.overlaps(a) is True


def test_no_overlap() -> None:
    a = make_admission(2020, 1, 2020, 3)
    b = make_admission(2020, 4, 2020, 6)
    assert a.overlaps(b) is False


def test_contained() -> None:
    a = make_admission(2020, 1, 2020, 12)
    b = make_admission(2020, 5, 2020, 6)
    assert a.overlaps(b) is True


def test_ongoing_overlaps_past() -> None:
    a = make_admission(2020, 1, 2020, 3)
    b = make_admission(2021, 1, None, None, is_ongoing=True)
    assert a.overlaps(b) is False  # 2021년 입원은 2020년과 안 겹침


def test_ongoing_overlaps_current() -> None:
    a = make_admission(2021, 1, 2021, 3)
    b = make_admission(2021, 2, None, None, is_ongoing=True)
    assert a.overlaps(b) is True


def test_period_text() -> None:
    a = make_admission(2020, 3, 2020, 5)
    assert a.period_text() == "2020년 3월 ~ 2020년 5월"

    b = make_admission(2021, 2, None, None, is_ongoing=True)
    assert b.period_text() == "2021년 2월 ~ 현재 입원 중"
