"""입원 기간(연·월) 입력 검증 및 변환 유틸리티.

매칭 정밀도는 연·월 단위이므로, 사용자 입력을 (year, month) 로 검증한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

# 한국 시간대 (UTC+9)
KST = timezone(timedelta(hours=9))

# 허용 범위: 폐쇄병동 입원 이력으로 현실적인 범위
MIN_YEAR = 1980


class PeriodError(ValueError):
    """입력 기간이 유효하지 않을 때 발생."""


@dataclass(frozen=True)
class Period:
    """입원 기간 (연·월)."""

    start_year: int
    start_month: int
    end_year: int | None
    end_month: int | None
    is_ongoing: bool

    def start_ym(self) -> int:
        return self.start_year * 12 + (self.start_month - 1)

    def end_ym(self) -> int | None:
        if self.is_ongoing or self.end_year is None or self.end_month is None:
            return None
        return self.end_year * 12 + (self.end_month - 1)

    def text(self) -> str:
        start = f"{self.start_year}년 {self.start_month}월"
        if self.is_ongoing:
            return f"{start} ~ 현재 입원 중"
        return f"{start} ~ {self.end_year}년 {self.end_month}월"


def current_year() -> int:
    """현재 KST 기준 연도."""
    return datetime.now(KST).year


def current_ym() -> int:
    """현재 KST 기준 연·월 정수."""
    now = datetime.now(KST)
    return now.year * 12 + (now.month - 1)


def validate_period(
    start_year: int,
    start_month: int,
    end_year: int | None,
    end_month: int | None,
    is_ongoing: bool,
) -> Period:
    """입원 기간 입력을 검증하고 Period 객체를 반환한다.

    Raises:
        PeriodError: 값이 범위를 벗어나거나 시작이 종료보다 늦을 때.
    """
    this_year = current_year()

    if not (MIN_YEAR <= start_year <= this_year):
        raise PeriodError(
            f"입원 연도는 {MIN_YEAR}년 ~ {this_year}년 사이여야 합니다."
        )
    if not (1 <= start_month <= 12):
        raise PeriodError("입원 월은 1 ~ 12 사이여야 합니다.")

    if is_ongoing:
        return Period(start_year, start_month, None, None, True)

    if end_year is None or end_month is None:
        raise PeriodError(
            "퇴원 연·월을 입력하거나 '현재 입원 중'을 선택해주세요."
        )
    if not (MIN_YEAR <= end_year <= this_year):
        raise PeriodError(
            f"퇴원 연도는 {MIN_YEAR}년 ~ {this_year}년 사이여야 합니다."
        )
    if not (1 <= end_month <= 12):
        raise PeriodError("퇴원 월은 1 ~ 12 사이여야 합니다.")

    start_ym = start_year * 12 + (start_month - 1)
    end_ym = end_year * 12 + (end_month - 1)
    if start_ym > end_ym:
        raise PeriodError("입원 시기가 퇴원 시기보다 늦을 수 없습니다.")

    now_ym = current_ym()
    if start_ym > now_ym:
        raise PeriodError("입원 시기가 미래일 수 없습니다.")

    return Period(start_year, start_month, end_year, end_month, False)


def format_ym(ym_value: int) -> str:
    """연·월 정수를 사람이 읽는 문자열로 변환한다."""
    year = ym_value // 12
    month = ym_value % 12 + 1
    return f"{year}년 {month}월"
