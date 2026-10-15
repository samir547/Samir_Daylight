"""Unit tests for week_plan.py (spec section 9.1, D1, D8). Run in this folder:
    python -m pytest tests
Calendar used: Saturdays in Sep 2026 are 5, 12, 19, 26; Oct 2026: 3, 10, 17, 24, 31
(five Saturdays); Nov 2026: 7, 14, 21, 28. All runs are Thursdays unless noted.
"""
from datetime import date

from week_plan import plan_weekly_run, saturdays_in_month, week_windows

SEP = [date(2026, 9, d) for d in (5, 12, 19, 26)]
OCT = [date(2026, 10, d) for d in (3, 10, 17, 24, 31)]


# ---- first run of a month (spec worked example, Thursday 2026-11-05)

def test_first_run_of_month_pulls_completion_week_only():
    # The 2026-10-29 run loaded October through Oct 24; Oct 31 is now 5 days old.
    plan = plan_weekly_run(date(2026, 11, 5), stored_saturdays=OCT[:4])
    assert plan.current_weeks == []              # Nov 7 has not closed
    assert plan.completion_weeks == [date(2026, 10, 31)]
    assert plan.week_windows == [(date(2026, 10, 25), date(2026, 10, 31))]
    assert plan.month is None                    # day 5 is inside MONTH_SAFETY_DAY
    assert not plan.no_data


def test_first_run_of_month_with_nothing_missing_is_no_data():
    plan = plan_weekly_run(date(2026, 11, 5), stored_saturdays=OCT)
    assert plan.week_windows == []
    assert plan.month is None
    assert plan.no_data


def test_without_database_all_previous_month_weeks_are_assumed_missing():
    plan = plan_weekly_run(date(2026, 11, 5), stored_saturdays=None)
    assert plan.completion_assumed
    assert plan.completion_weeks == OCT
    assert plan.week_windows == [(date(2026, 9, 27), date(2026, 10, 31))]
    assert any("assuming all 5 week(s) of 2026-10 are missing" in n for n in plan.notes)


def test_second_run_of_month_pulls_current_week_and_month_row():
    # Spec worked example, Thursday 2026-11-12.
    plan = plan_weekly_run(date(2026, 11, 12), stored_saturdays=OCT)
    assert plan.current_weeks == [date(2026, 11, 7)]
    assert plan.completion_weeks == []
    assert plan.week_windows == [(date(2026, 11, 1), date(2026, 11, 7))]
    assert plan.month == date(2026, 10, 1)


def test_completion_respects_publish_lag():
    # Tuesday 2026-11-03: Oct 31 is only 3 days old, so it is neither stored nor requested yet.
    plan = plan_weekly_run(date(2026, 11, 3), stored_saturdays=OCT[:4])
    assert plan.completion_weeks == []
    assert plan.no_data


# ---- a missed run followed by a catch-up run

def test_missed_runs_then_catch_up_pulls_the_gap_in_one_window():
    # Last good run 2026-10-22 (October through Oct 17). Oct 29 and Nov 5 were missed.
    plan = plan_weekly_run(date(2026, 11, 12), stored_saturdays=OCT[:3])
    assert plan.current_weeks == [date(2026, 11, 7)]
    assert plan.completion_weeks == [date(2026, 10, 24), date(2026, 10, 31)]
    assert plan.week_windows == [(date(2026, 10, 18), date(2026, 11, 7))]
    assert plan.month == date(2026, 10, 1)


def test_non_contiguous_missing_week_gets_its_own_window():
    stored = [s for s in OCT if s != date(2026, 10, 10)]
    plan = plan_weekly_run(date(2026, 11, 12), stored_saturdays=stored)
    assert plan.week_windows == [(date(2026, 10, 4), date(2026, 10, 10)),
                                 (date(2026, 11, 1), date(2026, 11, 7))]


# ---- a month with five Saturdays

def test_month_with_five_saturdays_last_run_inside_the_month():
    # Thursday 2026-10-29: Oct 24 is published, Oct 31 is not; Oct 31 is picked up on Nov 5.
    plan = plan_weekly_run(date(2026, 10, 29), stored_saturdays=SEP)
    assert saturdays_in_month(date(2026, 10, 1)) == OCT
    assert plan.current_weeks == OCT[:4]
    assert plan.week_windows == [(date(2026, 9, 27), date(2026, 10, 24))]


def test_five_saturday_month_plus_current_weeks_is_chunked_to_max_window():
    # 5 October weeks + Nov 7, 14, 21 = 8 contiguous weeks. The real 6-week lookback
    # would skip Oct 3 (see the lookback tests), so it is widened here to test chunking.
    plan = plan_weekly_run(date(2026, 11, 26), stored_saturdays=None, lookback_weeks=52)
    assert plan.week_windows == [(date(2026, 9, 27), date(2026, 11, 14)),
                                 (date(2026, 11, 15), date(2026, 11, 21))]


def test_week_windows_never_exceed_max_weeks():
    sats = SEP + OCT + [date(2026, 11, d) for d in (7, 14, 21, 28)]
    for start, end in week_windows(sats, max_weeks=7):
        assert ((end - start).days + 1) // 7 <= 7
        assert start.weekday() == 6 and end.weekday() == 5   # Sunday..Saturday


# ---- the straddling week (Sep 27 - Oct 3 belongs to October, D8)

def test_straddling_week_belongs_to_the_month_of_its_saturday():
    plan = plan_weekly_run(date(2026, 10, 8), stored_saturdays=SEP)
    assert plan.current_weeks == [date(2026, 10, 3)]
    assert plan.completion_weeks == []
    assert plan.week_windows == [(date(2026, 9, 27), date(2026, 10, 3))]


def test_straddling_week_is_not_a_completion_week_of_the_earlier_month():
    # Thursday 2026-10-01: Sep 26 is 5 days old and missing; Oct 3 has not closed.
    plan = plan_weekly_run(date(2026, 10, 1), stored_saturdays=SEP[:3])
    assert plan.completion_weeks == [date(2026, 9, 26)]
    assert plan.week_windows == [(date(2026, 9, 20), date(2026, 9, 26))]
    assert plan.month is None


def test_early_month_with_nothing_to_do_is_no_data():
    plan = plan_weekly_run(date(2026, 10, 1), stored_saturdays=SEP)
    assert plan.no_data


# ---- completion weeks outside Amazon's weekly lookback (WEEK_LOOKBACK_WEEKS = 6)

def test_catch_up_after_several_missed_runs_skips_weeks_outside_lookback():
    # Last good run 2026-10-01 (September only). Every October run was missed.
    # Thursday 2026-11-26: newest published Saturday Nov 21, oldest requestable Oct 10.
    plan = plan_weekly_run(date(2026, 11, 26), stored_saturdays=SEP)
    assert plan.skipped_weeks == [date(2026, 10, 3)]
    assert plan.completion_weeks == OCT[1:]
    assert plan.week_windows == [(date(2026, 10, 4), date(2026, 11, 21))]   # 7 weeks, one request
    assert plan.month == date(2026, 10, 1)
    assert "Week 2026-09-27..2026-10-03 skipped, outside lookback" in " ".join(plan.notes)
    assert not plan.no_data


def test_run_without_database_late_in_month_skips_weeks_outside_lookback():
    plan = plan_weekly_run(date(2026, 11, 26), stored_saturdays=None)
    assert plan.completion_assumed
    assert plan.skipped_weeks == [date(2026, 10, 3)]
    assert plan.week_windows == [(date(2026, 10, 4), date(2026, 11, 21))]
    notes = " ".join(plan.notes)
    assert "assuming all 5 week(s) of 2026-10 are missing" in notes
    assert "skipped, outside lookback" in notes


def test_weeks_inside_lookback_are_never_skipped():
    # Thursday 2026-11-05, nothing stored for October: Oct 3 is 4 weeks before Oct 31.
    plan = plan_weekly_run(date(2026, 11, 5), stored_saturdays=[])
    assert plan.skipped_weeks == []
    assert plan.completion_weeks == OCT


def test_run_with_only_skipped_weeks_is_no_data():
    # With the real constants a Thursday run cannot reach this state, so the
    # lookback is narrowed: only Oct 3 is missing, and it is outside the lookback.
    stored = [s for s in OCT if s != date(2026, 10, 3)]
    plan = plan_weekly_run(date(2026, 11, 5), stored_saturdays=stored, lookback_weeks=2)
    assert plan.skipped_weeks == [date(2026, 10, 3)]
    assert plan.week_windows == []
    assert plan.month is None
    assert plan.no_data
