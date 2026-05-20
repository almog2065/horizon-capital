"""Cadence job idempotency and plan persistence."""
from __future__ import annotations

import pytest

from app import config, daily_plan


@pytest.fixture
def isolated_plan(tmp_path, monkeypatch):
    from app import ops_db

    db_path = tmp_path / "ops.sqlite"
    monkeypatch.setattr(config, "OPS_DB", db_path)
    monkeypatch.setattr(config, "ARTIFACTS", tmp_path)
    ops_db.init_db()
    return tmp_path


def test_mark_job_done_persists(isolated_plan):
    daily_plan.load()
    daily_plan.mark_job_done("pre_open", result={"summary": "ok"})
    again = daily_plan.load()
    assert "pre_open" in again["completed_jobs"]


def test_run_job_skips_duplicate(isolated_plan, monkeypatch):
    from app import daily_cadence

    daily_plan.mark_job_done("pre_open", result={"summary": "ok"})
    monkeypatch.setattr(daily_cadence, "load", daily_plan.load)
    monkeypatch.setattr(daily_cadence, "mark_job_done", daily_plan.mark_job_done)
    out = daily_cadence.run_job("pre_open")
    assert out.get("skipped") is True
