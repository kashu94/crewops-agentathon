"""Duty arithmetic.

Two conventions in this dataset silently produce plausible-but-wrong answers,
and both live here.

1. Windows are **calendar-day** based — UTC dates inclusive of the duty date,
   not rolling 168/672-hour windows from an instant.
2. The FDP limit **shrinks with sector count**: 13h minus half an hour per
   sector beyond the second. A four-sector duty caps at 12.0h, not 13.

Canary: C-1042 accrues 20.93 duty hours over the seven calendar days ending
2026-09-14, leaving 39.07h of headroom. If that is right, the window maths is
right.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# From rules.json. Kept as named constants so a rule change is one edit.
BASE_FDP_HOURS = 13.0
REDUCTION_PER_EXTRA_SECTOR = 0.5
FREE_SECTORS = 2
MAX_DUTY_HOURS_7D = 60.0
MAX_FLIGHT_HOURS_28D = 100.0
MIN_REST_HOURS = 12.0
DUTY_WINDOW_DAYS = 7
FLIGHT_WINDOW_DAYS = 28

REPORT_BEFORE_FIRST_DEP = timedelta(minutes=60)
RELEASE_AFTER_LAST_ARR = timedelta(minutes=30)


def fdp_limit(n_sectors: int) -> float:
    """Max flight duty period for a duty of `n_sectors` legs.

    >>> [fdp_limit(n) for n in (1, 2, 3, 4, 6)]
    [13.0, 13.0, 12.5, 12.0, 11.0]
    """
    return BASE_FDP_HOURS - REDUCTION_PER_EXTRA_SECTOR * max(0, n_sectors - FREE_SECTORS)


def hours_between(start: datetime, end: datetime) -> float:
    """Elapsed hours, rounded the way the answer keys report them."""
    return round((end - start).total_seconds() / 3600.0, 2)


def report_time(first_departure: datetime) -> datetime:
    return first_departure - REPORT_BEFORE_FIRST_DEP


def release_time(last_arrival: datetime) -> datetime:
    return last_arrival + RELEASE_AFTER_LAST_ARR


def calendar_window(end: date, days: int) -> tuple[date, date]:
    """The inclusive calendar-day window of `days` ending on `end`.

    Seven days ending the 14th is the 8th through the 14th — not the 7th.
    Off-by-one here is the single most likely source of a wrong legality
    verdict, which is why it is one named function rather than inline maths.

    >>> calendar_window(date(2026, 9, 14), 7)
    (datetime.date(2026, 9, 8), datetime.date(2026, 9, 14))
    """
    return end - timedelta(days=days - 1), end


def format_hours(value: float) -> str:
    """1.33 -> '1h20m'. Matches how the answer keys phrase breaches."""
    whole = int(abs(value))
    minutes = round((abs(value) - whole) * 60)
    if minutes == 60:
        whole, minutes = whole + 1, 0
    return f"{whole}h{minutes:02d}m"


@dataclass(frozen=True, slots=True)
class DutyDay:
    """One day of a pairing: when it starts, ends, and how many legs it flies."""

    date: date
    report_utc: datetime
    release_utc: datetime
    n_sectors: int
    block_hours: float
    aircraft_type: str
    dep_station: str

    @property
    def fdp_hours(self) -> float:
        return hours_between(self.report_utc, self.release_utc)

    @property
    def fdp_limit(self) -> float:
        return fdp_limit(self.n_sectors)

    @property
    def fdp_headroom(self) -> float:
        return round(self.fdp_limit - self.fdp_hours, 2)

    def delayed(self, hours: float) -> "DutyDay":
        """The same duty with its start pushed back — the what-if shape.

        A delay extends the duty period, so it can push an otherwise legal
        crew past their FDP limit on the tail legs. The delay itself creates
        the crewing problem.
        """
        from dataclasses import replace

        shift = timedelta(hours=hours)
        return replace(self, report_utc=self.report_utc + shift,
                       release_utc=self.release_utc + shift + shift * 0)
