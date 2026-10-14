"""Nigerian working days (WP-05).

Plain English
-------------
Four of the regulator's seven scam clocks are counted in *working days*, and a
working day is a calendar question, not a timer: fourteen working days from a
report made on the Thursday before Easter is not fourteen days of 24 hours.

This file answers one question — when does "N working days after this moment"
run out — and nothing else.

The reading, stated (D71b):

* Time is Lagos time, UTC+1 all year. Nigeria has no daylight saving, so a fixed
  offset is exact and needs no timezone database.
* Saturdays, Sundays and public holidays are not working days.
* Day one is the first working day **after** the day the clock started. A report
  on a Monday afternoon does not spend Monday.
* The clock runs out at the **end** of the Nth working day (midnight Lagos).

Public holidays are data, not code (``public_holidays``): the Federal Government
declares them, moves them to a Monday when they fall on a weekend, and fixes the
Islamic holidays only days before, on the sighting of the moon. Dates not yet
declared are carried as estimates and flagged, never silently trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

LAGOS = timezone(timedelta(hours=1), "WAT")


@dataclass(frozen=True)
class Holiday:
    day: date
    name: str
    confirmed: bool = True


class Calendar:
    """Working-day arithmetic over a set of declared public holidays."""

    def __init__(self, holidays: list[Holiday] | tuple[Holiday, ...] = ()) -> None:
        self._by_day = {h.day: h for h in holidays}
        self.years = frozenset(h.day.year for h in holidays)

    def is_working_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self._by_day

    def add_working_days(self, start: datetime, n: int) -> datetime:
        """The moment N working days after ``start`` run out (end of day N, Lagos)."""
        if n < 1:
            raise ValueError("a working-day clock needs at least one day")
        day = start.astimezone(LAGOS).date()
        counted = 0
        while counted < n:
            day += timedelta(days=1)
            if self.is_working_day(day):
                counted += 1
        return datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=LAGOS)

    def holidays_between(self, start: datetime, end: datetime) -> list[Holiday]:
        first, last = start.astimezone(LAGOS).date(), end.astimezone(LAGOS).date()
        return [h for d, h in sorted(self._by_day.items()) if first <= d <= last]

    def covers(self, start: datetime, end: datetime) -> bool:
        """Whether holidays are loaded for every year the span touches.

        A year with no rows would count every weekday as working, and the
        deadline would come out early without anything looking wrong.
        """
        first, last = start.astimezone(LAGOS).year, end.astimezone(LAGOS).year
        return all(y in self.years for y in range(first, last + 1))


def easter_sunday(year: int) -> date:
    """Western Easter, by the anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def observed(days: list[tuple[date, str]]) -> list[tuple[date, str]]:
    """Move holidays that fall on a weekend to the next free working day.

    This is the Federal Government's usual practice (Boxing Day on a Sunday
    after Christmas on a Saturday lands on Tuesday), applied in date order so
    two holidays never collapse onto one Monday.
    """
    taken: set[date] = {d for d, _ in days if d.weekday() < 5}
    out = []
    for day, name in sorted(days):
        if day.weekday() >= 5:
            moved = day
            while moved.weekday() >= 5 or moved in taken:
                moved += timedelta(days=1)
            taken.add(moved)
            out.append((moved, f"{name} (observed)"))
        else:
            out.append((day, name))
    return out
