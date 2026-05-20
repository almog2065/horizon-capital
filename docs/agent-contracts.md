# Agent contracts

Every agent has a typed contract: what it consumes, what it produces,
which tools it can call, and how it fails. This doc is the cheat
sheet — the source of truth is `app/agents/<name>.py` + `app/firm_state.py`.

The brief: *"Each agent must have a clear contract: typed inputs,
typed outputs, defined tools, defined failure modes."*

---

## Shared shape

All agents export a `run(...)` function that:

* Takes structured kwargs (no `**kwargs` catch-all).
* Returns a plain dict that matches the per-agent shape below.
* Records every LLM call via `app.llm.chat_json` (which writes to the
  trace + LangSmith).
* Has a deterministic `_mock_output(...)` fallback used when no
  OpenAI key is configured or when `USE_MOCK_LLM=1`.

The orchestrator (LangGraph in `app/graph.py`) wraps these calls,
inserts retries, validates the output against the expected schema,
and writes the result back into `FirmState`.

---

## §1 — `news_triage`

**Purpose**: Decide whether a piece of news is firm-relevant. The
cheapest gate in the pipeline.

**Module**: `app/agents/news_triage.py`

**Entry**: `run(news, holdings_tickers, watchlist_tickers, ...)`

**Input**:
| field               | type      | meaning                          |
|---------------------|-----------|----------------------------------|
| `news`              | `dict`    | `{ts, ticker, headline, source, raw}` |
| `holdings_tickers`  | `list[str]` | tickers currently held         |
| `watchlist_tickers` | `list[str]` | tickers being monitored        |

**Output**:
```python
{
  "relevant": bool,
  "reasons": list[str],
  "downstream": list[str],   # ["fundamental"] or [] or ["position_monitor"]
  "confidence": float,       # 0..1
}
```

**Tools**: RAG retrieval over `policy` corpus (small k).

**Failure modes**:
* Bad input shape → schema reject, no LLM call.
* LLM error → falls back to `_mock_output` (declares irrelevant if
  ticker not in holdings∪watchlist, relevant otherwise).
* Policy retrieval empty → relevant=true with confidence 0.4 (safe
  default — sends to fundamental for the real decision).

---

## §2 — `idea_generator`

**Purpose**: Score tickers in the firm universe and propose top-K
candidates for fundamental work. Runs both ad-hoc and on the
scheduled firm-balance loop.

**Module**: `app/agents/idea_generator.py`

**Entry**: `run(as_of, firm_state, ..., top_k)`

**Output**: list of candidate dicts:
```python
{
  "ticker": str,
  "sector": str,
  "scores": {
    "quality": float,
    "valuation": float,
    "precedent": float,
    "fit": float,
    "composite": float,
  },
  "fundamental_snapshot": {...},
  "in_universe": bool,
  "eligible_for_plan": bool,
}
```

**Tools**:
* SEC EDGAR + yfinance via `app/market_providers.py` for fresh
  data (with mock fallback when offline).
* RAG over `past_plans` for precedent scoring.

**Failure modes**:
* yfinance rate-limit → fall back to `data/candidates.json` seeded
  pool.
* EDGAR UA missing → uses default UA with a warning.

---

## §3 — `fundamental`

**Purpose**: Build a thesis for a single ticker, grounded in retrieved
evidence. The most expensive LLM call in a normal trade.

**Module**: `app/agents/fundamental.py`

**Entry**: `run(ticker, mode="new_research", as_of, ...)`

**Input**:
* `mode`: `"new_research"` | `"refresh"` | `"event_driven"`.
* The orchestrator pre-loads top-k retrievals from filings, news,
  past_plans for `ticker`.

**Output**:
```python
{
  "ticker": str,
  "thesis": str,
  "claims": [
    {"text": str, "citation": str, "weight": float},
    ...
  ],
  "confidence": float,
  "refuse": bool,
  "refuse_reason": str | None,
}
```

**Tools**: RAG over `filings`, `news`, `past_plans`.

**Failure modes**:
* Insufficient retrievals → returns `refuse=true` with reason.
* LLM returns no citations → orchestrator counts as guardrail
  breach and routes to auditor.
* LLM error → mock fallback returns a low-confidence stub thesis.

---

## §4 — `plan_builder`

**Purpose**: Convert a thesis into a sized trade plan that respects
the firm's capital allocation policy.

**Module**: `app/agents/plan_builder.py`

**Entry**: `run(ticker, fundamental, as_of, ...)`

**Output**:
```python
{
  "ticker": str,
  "action": "buy" | "sell" | "hold",
  "qty": int,
  "estimated_price": float,
  "notional": float,
  "rationale": str,
  "citations": list[str],   # references thesis citations + policy
  "expected_hitl": bool,
  "horizon_days": int,
}
```

**Tools**: RAG over `policy` (allocation rules); firm state for cash + existing position.

**Failure modes**:
* Cash insufficient → returns `action="hold"` with reason.
* Mechanical-gate failure (e.g. position too small to add) →
  `action="hold"`.

---

## §5 — `plan_supervisor`

**Purpose**: Plan-level sanity check. Prevents the firm from acting
on stale or contradictory plans.

**Module**: `app/agents/plan_supervisor.py`

**Entry**: `run(plan, recent_history, ...)`

**Output**:
```python
{
  "approved": bool,
  "edits": list[dict],          # any forced modifications
  "rejection_reason": str | None,
  "freshness_ok": bool,         # thesis < 24h?
  "consistency_ok": bool,       # not contradicting open position?
  "policy_ok": bool,            # not violating policy?
}
```

**Tools**: RAG over `past_plans` for consistency check.

**Failure modes**:
* Inconsistent → emits `approved=false`, traced.
* Stale thesis → forces refresh by re-routing through `fundamental`.

---

## §6 — `risk_officer`

**Purpose**: Compute notional and concentration exposure, decide
whether HITL is required. The bouncer.

**Module**: `app/agents/risk_officer.py`

**Entry**: `run(plan, firm_state, policy, ...)`

**Output**:
```python
{
  "decision": "auto_execute" | "require_hitl" | "block",
  "thresholds_checked": list[str],
  "exposure_after": {
    "<ticker>_pct_nav": float,
    "sector_pct_nav": float,
    "gross_exposure_pct": float,
  },
  "citations": list[str],   # always cites the risk policy clauses
  "hitl_reasons": list[str],
}
```

**Tools**: RAG over `policy` (specifically the risk policy).

**Failure modes**:
* If any threshold computation fails → decision defaults to
  `require_hitl`. We prefer false positives.

---

## §7 — `position_monitor`

**Purpose**: Watch open positions for exit triggers. Runs on the
scheduled loop and on news events tagged to a held ticker.

**Module**: `app/agents/position_monitor.py`

**Entry**: `run(holding, plan, ...)`

**Output**:
```python
{
  "ticker": str,
  "action": "hold" | "trim" | "exit",
  "reasons": list[str],       # one of: price_drift, news_materiality, filing_relevance, earnings_proximity
  "breaches": list[dict],     # which guardrails fired
  "hitl_required": bool,
}
```

**Tools**: Fresh price (`market_data.py`), RAG news, fresh filings.

**Failure modes**:
* Stale price → defers (returns `hold` with reason).
* Unable to compute → falls back to mock evaluation.

---

## §8 — `auditor`

**Purpose**: Side-channel that records what happened, what was cited,
and any guardrail breaches. The auditor never *prevents* a trade — it
witnesses.

**Module**: `app/agents/auditor.py`

**Entry**: `run(agent_name, agent_output, journal_id)`

**Output**:
```python
{
  "audit_id": str,
  "verdict": "ok" | "concern" | "breach",
  "notes": list[str],
  "counter_evidence": list[str],   # citations that contradict the claim
}
```

**Tools**: RAG over all four corpora for counter-evidence.

**Failure modes**:
* If the auditor itself fails, the trace records an `auditor_error`;
  the trade is **not** blocked. This is deliberate — we don't want
  the auditor to be a single point of trade failure.

---

## Cross-cutting: `asset_universe` + `manager_scoring`

Two helper modules the agents depend on but don't extend.

### `app/asset_universe.py` — multi-asset registry
A frozen `AssetMeta` dataclass per candidate. Fields:

| field             | meaning                                              |
|-------------------|------------------------------------------------------|
| `ticker`          | display symbol                                       |
| `asset_class`     | `equity` / `crypto` / `commodity_proxy` / `rates_proxy` / `fx_proxy` |
| `sector`          | reporting sector                                     |
| `data_provider`   | `yfinance` / `coingecko` / `yfinance_etf`            |
| `coingecko_id`    | (crypto only) CoinGecko id                            |
| `yahoo_symbol`    | (equity / ETF) yfinance symbol                        |
| `underlying`      | (proxy ETFs) what the ETF tracks                      |
| `min_market_cap_usd` / `max_position_pct_nav` | sizing guardrails per asset class |

Agents read this via `asset_universe.get_meta(ticker)` and branch on
`is_crypto` / `is_commodity_proxy` etc. The registry is loaded once,
LRU-cached, from `data/candidates.json`.

### `app/manager_scoring.py` — composite-score helper
A pure-function module factored out of the previous `firm_manager`
inline logic. Computes the manager-signal component of the
idea_generator's composite score. The settings knob
`MANAGER_BOOK_SCORE_WEIGHT` (default 0.12) tunes how much the
manager's view biases the composite.

Both modules are agent-class-agnostic; the agents pull from them, not
the other way around.

---

## §9 — `firm_manager`

**Purpose**: Top-level policy routing. Decides what scans to run, what
sectors are under/over-weight, when to freeze new positions.

**Module**: `app/agents/firm_manager.py`

**Entry**: `run(firm_state, as_of, ...)`

**Output**:
```python
{
  "as_of": str,
  "directives": list[dict],            # ordered tasks for this cycle
  "sector_directives": dict,           # sector → "increase" | "decrease" | "hold"
  "freeze_new_positions": bool,
  "notes": list[str],
}
```

**Tools**: RAG over `policy`, firm state, idea_generator snapshots.

**Failure modes**:
* Manager runs in a cooldown (`FIRM_MANAGER_SCAN_COOLDOWN_SEC`);
  early returns when last run was recent.

---

## Cross-agent invariants

These are properties the orchestrator enforces, regardless of which
agent runs:

* **Every LLM call produces JSON** (`response_format="json_object"`).
* **Every LLM output is schema-checked** before being written to
  `FirmState`. Schema failures route to auditor.
* **Every LLM output carries citations or refuses.** Naked numbers
  are a guardrail breach.
* **No agent calls another agent.** All transitions go through the
  graph.
* **No agent writes to the DB directly** except via `app/portfolio.py`
  and `app/db.py` helpers; the orchestrator controls timing.
* **Mock fallbacks exist for every LLM call.** No live API key
  required for the firm to operate (or for CI to run).

---

## How to add a new agent

1. Create `app/agents/<new_agent>.py` with:
   * A `run(...)` function with kwargs (no `**kwargs`).
   * A `_mock_output(...)` deterministic fallback.
   * Module-level docstring explaining responsibility + contract.
2. Add the node to `app/graph.py` with input/output mapping.
3. Add the typed I/O fields to `app/firm_state.py`.
4. Add a smoke test asserting the agent's shape (mock mode).
5. Update `docs/agent-contracts.md` (this file) with §N.

Total surface area for a no-op agent: ~80 lines + one graph edge.
