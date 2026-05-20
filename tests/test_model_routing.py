"""Cost-aware model routing tests."""
from __future__ import annotations

from app.model_routing import (
    estimate_cost_usd,
    model_for,
    routing_table,
)


def test_default_routing_fundamental_is_full_model():
    # Heavy reasoning routes to the bigger model.
    assert model_for("fundamental") == "gpt-4o"


def test_default_routing_news_triage_is_mini():
    assert model_for("news_triage") == "gpt-4o-mini"


def test_unknown_purpose_falls_back_to_config_default(monkeypatch):
    # Force the legacy config default to a sentinel value.
    from app import config
    monkeypatch.setattr(config, "OPENAI_MODEL", "test-default-model", raising=False)
    assert model_for("brand_new_purpose_not_in_table") == "test-default-model"


def test_env_override_wins_over_default(monkeypatch):
    monkeypatch.setenv("MODEL_FOR_FUNDAMENTAL", "gpt-3.5-turbo")
    assert model_for("fundamental") == "gpt-3.5-turbo"


def test_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("MODEL_FOR_FUNDAMENTAL", "gpt-3.5-turbo")
    assert model_for("fundamental", explicit="gpt-4o") == "gpt-4o"


def test_estimate_cost_known_model():
    # gpt-4o-mini: $0.15 in / $0.60 out per 1M tokens
    # 1000 in + 500 out = 0.00015 + 0.00030 = 0.00045
    c = estimate_cost_usd("gpt-4o-mini", 1000, 500)
    assert c == 0.00045


def test_estimate_cost_unknown_model_is_zero():
    assert estimate_cost_usd("definitely-not-a-real-model", 1000, 500) == 0.0


def test_estimate_cost_gpt4o_more_expensive():
    cheap = estimate_cost_usd("gpt-4o-mini", 1000, 1000)
    pricey = estimate_cost_usd("gpt-4o", 1000, 1000)
    assert pricey > cheap


def test_routing_table_snapshot_has_all_agents():
    t = routing_table()
    for agent in [
        "news_triage", "idea_generator", "fundamental", "plan_builder",
        "plan_supervisor", "risk_officer", "position_monitor", "auditor",
        "firm_manager",
    ]:
        assert agent in t


def test_routing_table_reflects_env(monkeypatch):
    monkeypatch.setenv("MODEL_FOR_RISK_OFFICER", "custom-risk-model")
    t = routing_table()
    assert t["risk_officer"] == "custom-risk-model"
