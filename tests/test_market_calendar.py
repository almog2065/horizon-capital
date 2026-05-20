"""Market calendar and cadence scheduling."""
from __future__ import annotations

from datetime import datetime, timezone

from app.market_calendar import (
    due_jobs,
    is_equity_session_open,
    is_trading_day,
    job_at_et,
    next_job_utc,
)


def test_trading_day_weekday():
    wed = datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc)
    assert is_trading_day(wed) is True


def test_trading_day_weekend():
    sat = datetime(2026, 5, 23, 15, 0, tzinfo=timezone.utc)
    assert is_trading_day(sat) is False


def test_session_open_midday_et():
    # 15:00 UTC on 2026-05-20 ≈ 11:00 ET (EDT) — inside session
    t = datetime(2026, 5, 20, 15, 0, tzinfo=timezone.utc)
    assert is_equity_session_open(t) is True


def test_session_closed_after_hours_et():
    t = datetime(2026, 5, 20, 22, 0, tzinfo=timezone.utc)
    assert is_equity_session_open(t) is False


def test_due_jobs_after_pre_open():
    # 12:00 UTC ≈ 08:00 ET — pre_open (07:30) should be due
    t = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    due = due_jobs(t, completed=set())
    assert "pre_open" in due
    assert "market_open" not in due


def test_next_job_skips_completed():
    t = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    nxt = next_job_utc(t, completed={"pre_open"})
    assert nxt is not None
    assert nxt[0] != "pre_open"


def test_job_at_et_is_utc_aware():
    at = job_at_et("pre_open", datetime(2026, 5, 20).date())
    assert at.tzinfo is not None
