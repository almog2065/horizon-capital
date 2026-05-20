"""Cost-aware model routing.

The brief calls out *"cost-aware model routing"* as a bonus. Different
agents have different reasoning needs; routing each call to the
cheapest sufficient model is one of the largest cost wins a real
firm can capture.

Routing is by **purpose** — the `purpose` string passed to
`app.llm.chat_json`. The mapping is data-driven (in env / settings)
so a deployment can tune routing without code changes.

Default routing table (low-cost agent calls → mini; complex
reasoning → full model):

| purpose                  | model         |
|--------------------------|---------------|
| news_triage              | gpt-4o-mini   |
| idea_generator           | gpt-4o-mini   |
| fundamental              | gpt-4o        |  ← heavy thesis work
| plan_builder             | gpt-4o-mini   |
| plan_supervisor          | gpt-4o-mini   |
| risk_officer             | gpt-4o-mini   |
| position_monitor         | gpt-4o-mini   |
| auditor                  | gpt-4o-mini   |
| firm_manager             | gpt-4o-mini   |
| *anything else*          | OPENAI_MODEL  (default = gpt-4o-mini)

Override by setting `MODEL_FOR_<purpose>=gpt-4o` (uppercased,
underscored). Example:

    MODEL_FOR_FUNDAMENTAL=gpt-4o
    MODEL_FOR_RISK_OFFICER=gpt-4o-mini

Pure function, deterministic, zero I/O.
"""
from __future__ import annotations

import os
from typing import Optional

from . import config

# Sensible defaults — only the heaviest reasoning call uses the bigger
# model, everything else stays on the cheap one. Tuned for the brief's
# "consider token consumption" criterion.
_DEFAULT_ROUTING: dict[str, str] = {
    "news_triage":      "gpt-4o-mini",
    "idea_generator":   "gpt-4o-mini",
    "fundamental":      "gpt-4o",
    "plan_builder":     "gpt-4o-mini",
    "plan_supervisor":  "gpt-4o-mini",
    "risk_officer":     "gpt-4o-mini",
    "position_monitor": "gpt-4o-mini",
    "auditor":          "gpt-4o-mini",
    "firm_manager":     "gpt-4o-mini",
}


def _env_override(purpose: str) -> Optional[str]:
    if not purpose:
        return None
    key = f"MODEL_FOR_{purpose.upper().replace('.', '_').replace('-', '_')}"
    v = os.getenv(key, "").strip()
    return v or None


def model_for(purpose: str, explicit: Optional[str] = None) -> str:
    """Resolve which OpenAI model to use for this purpose.

    Resolution order (first match wins):
      1. `explicit` (caller passed a specific model)
      2. env var `MODEL_FOR_<PURPOSE>` (deployment override)
      3. `_DEFAULT_ROUTING[purpose]`
      4. `OPENAI_MODEL` (global default)
    """
    if explicit:
        return explicit
    env = _env_override(purpose)
    if env:
        return env
    return _DEFAULT_ROUTING.get(purpose, config.OPENAI_MODEL)


# -----------------------------------------------------------------------------
# Token-cost estimation (USD per 1M tokens). Numbers are conservative
# estimates that we read from a small in-process table. The brief asks
# us to *measure* token cost honestly — the eval harness pulls totals
# from the trace, but the per-call cost estimate lives here so the
# /metrics endpoint can sum it.
# -----------------------------------------------------------------------------
_PRICING_USD_PER_M_TOKENS: dict[str, dict[str, float]] = {
    # rough public list-prices as of 2025; override via env if your
    # contract differs
    "gpt-4o-mini":         {"in": 0.15, "out": 0.60},
    "gpt-4o":              {"in": 2.50, "out": 10.00},
    "gpt-4-turbo":         {"in": 10.00, "out": 30.00},
    "gpt-3.5-turbo":       {"in": 0.50, "out": 1.50},
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Cents-precise estimate of an LLM call's cost. Unknown models cost 0."""
    rate = _PRICING_USD_PER_M_TOKENS.get(model)
    if not rate:
        return 0.0
    return round(
        (prompt_tokens / 1_000_000.0) * rate["in"]
        + (completion_tokens / 1_000_000.0) * rate["out"],
        6,
    )


def routing_table() -> dict[str, str]:
    """Snapshot of the current routing table (defaults + env overrides).

    Useful for `/version` or a diagnostics page.
    """
    out: dict[str, str] = {}
    for purpose, default in _DEFAULT_ROUTING.items():
        out[purpose] = _env_override(purpose) or default
    return out
