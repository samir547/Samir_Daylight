"""
Which WEEK windows and which MONTH row the weekly sales/inventory run requests
on a given day (data-ingestion-spec.md section 9.1, decisions D1 and D8).

Pure date logic, no I/O, so it can be unit-tested and reused by the report
scripts (to be folded into sp_api_common.py in T3).

  - WEEK rows for every week whose Saturday falls in the CURRENT month and is
    published (re-pulled every run, to catch restatements).
  - "Completion" WEEK rows: weeks whose Saturday falls in the PREVIOUS month,
    are published, and are not yet stored. Decided from stored data, not from
    the date. Without a database to ask (--sink csv, --dry-run) all of them
    are assumed missing, and the plan says so in `notes`.
  - The previous month's MONTH row, on every run once past MONTH_SAFETY_DAY.

A week belongs to the month of its Saturday (D8): Sep 27 - Oct 3 is October.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

# A week is requested once its Saturday is at least this many days old (UTC).
# Probe T0, 2026-10-15: 5-day-old weeks were available at ~14:00 UTC; not yet
# checked at the 06:00 UTC cron time.
WEEK_PUBLISH_LAG_DAYS = 5
# Amazon refuses WEEK requests longer than this (probe T0, 2026-10-15, US).
WEEK_MAX_WINDOW_WEEKS = 7
# On day 1..MONTH_SAFETY_DAY the previous month's MONTH row is not requested (section 9.2).
MONTH_SAFETY_DAY = 5


def previous_month_first(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def saturdays_in_month(first: date) -> list[date]:
    """Every Saturday in the month starting on `first`."""
    d = first + timedelta(days=(5 - first.weekday()) % 7)
    out = []
    while d.month == first.month:
        out.append(d)
        d += timedelta(days=7)
    return out


def latest_published_saturday(today: date, lag_days: int = WEEK_PUBLISH_LAG_DAYS) -> date:
    """Latest Saturday that is at least `lag_days` days before today."""
    d = today - timedelta(days=lag_days)
    return d - timedelta(days=(d.weekday() - 5) % 7)


def week_windows(saturdays, max_weeks: int = WEEK_MAX_WINDOW_WEEKS) -> list[tuple[date, date]]:
    """Group week-ending Saturdays into contiguous Sunday..Saturday windows of at most
    `max_weeks` weeks. Non-contiguous weeks go into separate windows."""
    windows, run = [], []
    for s in sorted(set(saturdays)):
        if run and (s - run[-1]).days == 7 and len(run) < max_weeks:
            run.append(s)
            continue
        if run:
            windows.append((run[0] - timedelta(days=6), run[-1]))
        run = [s]
    if run:
        windows.append((run[0] - timedelta(days=6), run[-1]))
    return windows


@dataclass
class WeeklyPlan:
    today: date
    current_weeks: list[date]        # Saturdays in the current month, published
    completion_weeks: list[date]     # Saturdays in the previous month, published, not stored
    completion_assumed: bool         # True when there was no database to check
    week_windows: list[tuple[date, date]]
    month: date | None               # first day of the MONTH row to request, or None
    notes: list[str] = field(default_factory=list)

    @property
    def no_data(self) -> bool:
        """Nothing to request at all: the run is `no_data` (D10), exit 0."""
        return not self.week_windows and self.month is None


def plan_weekly_run(today: date, stored_saturdays=None, *,
                    lag_days: int = WEEK_PUBLISH_LAG_DAYS,
                    max_weeks: int = WEEK_MAX_WINDOW_WEEKS,
                    month_safety_day: int = MONTH_SAFETY_DAY) -> WeeklyPlan:
    """Plan one marketplace and report type. `stored_saturdays` holds the week-ending
    dates already in the weekly table for the previous month (extra dates are
    ignored), or None when there is no database to ask."""
    published = latest_published_saturday(today, lag_days)
    current = [s for s in saturdays_in_month(today.replace(day=1)) if s <= published]
    prev_first = previous_month_first(today)
    prev = [s for s in saturdays_in_month(prev_first) if s <= published]

    notes = []
    if stored_saturdays is None:
        completion = prev
        notes.append(f"No database to check stored weeks: assuming all {len(prev)} week(s) "
                     f"of {prev_first:%Y-%m} are missing.")
    else:
        stored = set(stored_saturdays)
        completion = [s for s in prev if s not in stored]

    month = prev_first if today.day > month_safety_day else None
    if month is None:
        notes.append(f"MONTH {prev_first:%Y-%m} not requested: day {today.day} is within "
                     f"MONTH_SAFETY_DAY ({month_safety_day}).")
    if not current:
        notes.append(f"No closed week in {today:%Y-%m} yet.")

    return WeeklyPlan(today=today, current_weeks=current, completion_weeks=completion,
                      completion_assumed=stored_saturdays is None,
                      week_windows=week_windows(current + completion, max_weeks),
                      month=month, notes=notes)
