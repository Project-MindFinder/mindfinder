"""입원 기간 입력 검증 테스트."""

from __future__ import annotations

import pytest

from bot.utils.periods import (
    PeriodError,
    current_year,
    validate_period,
)


def test_valid_period() -> None:
    period = validate_period(2020, 3, 2020, 5, False)
    assert period.start_year == 2020
    assert period.start_month == 3
    assert period.end_year == 2020
    assert period.end_month == 5
    assert period.is_ongoing is False


def test_valid_ongoing() -> None:
    period = validate_period(2021, 1, None, None, True)
    assert period.is_ongoing is True
    assert period.end_ym() is None


def test_start_after_end_raises() -> None:
    with pytest.raises(PeriodError):
        validate_period(2020, 6, 2020, 3, False)


def test_future_start_raises() -> None:
    future = current_year() + 1
    with pytest.raises(PeriodError):
        validate_period(future, 1, future, 3, False)


def test_invalid_month_raises() -> None:
    with pytest.raises(PeriodError):
        validate_period(2020, 13, 2020, 5, False)


def test_missing_discharge_raises() -> None:
    with pytest.raises(PeriodError):
        validate_period(2020, 3, None, None, False)
