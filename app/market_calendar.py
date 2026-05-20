"""US equity session calendar — wall-clock cadence in America/New_York.

Implements operating-cadence §1 (daily schedule) for the scheduler worker.
Weekends are non-trading; US exchange holidays are not modelled yet (extend
with an exchange calendar when needed).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Regular session (NYSE cash equities)
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)

# Wall-clock jobs (ET) — see data/policies/04-operating-cadence.md
CADENCE_JOBS: dict[str, time] = {
    "pre_open": time(7, 30),       # integrity / manager prep — no trades
    "market_open": time(9, 35),    # route manager tasks + optional execution
    "mid_morning": time(10, 0),    # drift supervision
    "pre_close": time(15, 30),     # health check — no new risk
    "eod": time(16, 35),            # reconciliation + report + next-day brief
}


@dataclass(frozen=True)
class CadenceJob:
    job_id: str
    label: str
    at_et: time
    acts: bool          # may spawn pipelines / execute
    executes: bool      # may auto-execute approved plans (HITL still applies)


JOB_SPECS: dict[str, CadenceJob] = {
    "pre_open": CadenceJob(
        "pre_open",
        "Pre-market integrity & manager prep",
        CADENCE_JOBS["pre_open"],
        acts=False,
        executes=False,
    ),
    "market_open": CadenceJob(
        "market_open",
        "Open — route manager tasks & trading window",
        CADENCE_JOBS["market_open"],
        acts=True,
        executes=True,
    ),
    "mid_morning": CadenceJob(
        "mid_morning",
        "Mid-morning drift check",
        CADENCE_JOBS["mid_morning"],
        acts=True,
        executes=False,
    ),
    "pre_close": CadenceJob(
        "pre_close",
        "Pre-close health check",
        CADENCE_JOBS["pre_close"],
        acts=False,
        executes=False,
    ),
    "eod": CadenceJob(
        "eod",
        "EOD reconciliation & daily report",
        CADENCE_JOBS["eod"],
        acts=False,
        executes=False,
    ),
}


def now_utc(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def to_et(now: Optional[datetime] = None) -> datetime:
    return now_utc(now).astimezone(ET)


def trading_date(now: Optional[datetime] = None) -> date:
    return to_et(now).date()


def is_trading_day(now: Optional[datetime] = None) -> bool:
    """Weekday in ET (no holiday calendar yet)."""
    return to_et(now).weekday() < 5


def is_equity_session_open(now: Optional[datetime] = None) -> bool:
    """True during regular US cash session (09:30–16:00 ET, weekdays)."""
    if not is_trading_day(now):
        return False
    t = to_et(now).time()
    return SESSION_OPEN <= t <= SESSION_CLOSE


def job_at_et(job_id: str, on_date: date) -> datetime:
    spec = JOB_SPECS[job_id]
    local = datetime.combine(on_date, spec.at_et, tzinfo=ET)
    return local.astimezone(timezone.utc)


def jobs_for_day(on_date: date) -> list[tuple[str, datetime]]:
    return [(jid, job_at_et(jid, on_date)) for jid in JOB_SPECS]


def due_jobs(
    now: Optional[datetime] = None,
    *,
    completed: Optional[set[str]] = None,
    grace_sec: int = 90,
) -> list[str]:
    """Job IDs whose scheduled ET time has passed today and are not completed."""
    if not is_trading_day(now):
        return []
    completed = completed or set()
    et_now = to_et(now)
    out: list[str] = []
    for jid, spec in JOB_SPECS.items():
        if jid in completed:
            continue
        scheduled = datetime.combine(et_now.date(), spec.at_et, tzinfo=ET)
        if et_now >= scheduled - timedelta(seconds=grace_sec):
            out.append(jid)
    return out


def next_job_utc(
    now: Optional[datetime] = None,
    *,
    completed: Optional[set[str]] = None,
) -> Optional[tuple[str, datetime]]:
    """Next not-yet-fired job today, or first job on the next trading day."""
    completed = completed or set()
    et_now = to_et(now)
    today = et_now.date()

    def _iter(from_date: date) -> Iterator[tuple[str, datetime]]:
        d = from_date
        while True:
            if d.weekday() < 5:
                for jid, at in jobs_for_day(d):
                    if d == today and jid in completed:
                        continue
                    yield jid, at
            d += timedelta(days=1)
            if (d - today).days > 14:
                break

    now_u = now_utc(now)
    for jid, at in _iter(today):
        if at > now_u:
            return jid, at
    return None


def seconds_until_next_event(
    now: Optional[datetime] = None,
    *,
    completed: Optional[set[str]] = None,
) -> float:
    nxt = next_job_utc(now, completed=completed)
    if nxt is None:
        return 3600.0
    _, at = nxt
    return max(1.0, (at - now_utc(now)).total_seconds())


def build_day_schedule(
    on_date: Optional[date] = None,
    *,
    completed: Optional[set[str]] = None,
) -> list[dict]:  # noqa: D103 — dashboard rows
    """Serializable schedule rows for the operations dashboard."""
    done = completed or set()
    on_date = on_date or trading_date()
    rows: list[dict] = []
    for jid, spec in JOB_SPECS.items():
        at_utc = job_at_et(jid, on_date)
        rows.append({
            "job_id": jid,
            "label": spec.label,
            "at_et": spec.at_et.strftime("%H:%M"),
            "at_utc": at_utc.isoformat(),
            "acts": spec.acts,
            "executes": spec.executes,
            "status": "done" if jid in done else "pending",
        })
    return rows
