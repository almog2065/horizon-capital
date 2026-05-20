# End-to-end walkthrough — one trade, from trigger to fill

The brief explicitly asks for this: *"an end-to-end walkthrough of one
trade from trigger to fill."* Read this with the UI open at
http://127.0.0.1:8080 so you can point at each step as you talk.

The scenario: a piece of MSFT news arrives mid-morning. The trade ends
up auto-executed (no HITL required). Then we'll re-run the same flow
where the proposed trade trips the risk threshold and goes to the
Risk Committee.

---

## The cast

| Player              | Module                              | Role in this trade                                 |
|---------------------|-------------------------------------|----------------------------------------------------|
| Event source        | `app/main.py::trigger`              | Receives the news event                            |
| news_triage         | `app/agents/news_triage.py`         | Decides this is relevant                           |
| idea_generator      | `app/agents/idea_generator.py`      | Confirms MSFT is in-universe                       |
| fundamental         | `app/agents/fundamental.py`         | Builds a thesis grounded in RAG                    |
| plan_builder        | `app/agents/plan_builder.py`        | Composes the trade plan with sizing + citations    |
| plan_supervisor     | `app/agents/plan_supervisor.py`     | Reviews consistency, freshness                     |
| risk_officer        | `app/agents/risk_officer.py`        | Computes exposure, decides HITL                    |
| portfolio           | `app/portfolio.py`                  | Executes the paper fill                            |
| auditor             | `app/agents/auditor.py`             | Records the run                                    |
| traces              | `app/traces.py`                     | Writes every step                                  |
| reports             | `app/reports/excel_reporter.py`     | Renders the daily snapshot                         |

The orchestration is `app/graph.py` (LangGraph). The orchestrator is
the only thing that calls agents directly — agents don't call each
other.

---

## Path A — auto-executed trade

### Step 1 — News arrives (T+0s)

A POST to `/trigger` (or a scheduled scan) delivers a news item:

```json
{
  "ts": "2026-05-19T09:31:00Z",
  "ticker": "MSFT",
  "headline": "Microsoft beats Q1 cloud revenue guidance, raises forecast",
  "source": "edgar:filing:8K",
  "raw": "Microsoft Corporation (NASDAQ: MSFT) today announced…"
}
```

→ Where: UI page `/trigger`. Code: `app/main.py::trigger`. Trace
event: `event=news_received`.

### Step 2 — `news_triage` filters (T+0.4s)

The triage agent reads the headline + a small RAG retrieval over the
**policy** corpus (firm charter, what does this firm cover?), then
returns:

```json
{
  "relevant": true,
  "reasons": ["mentions firm universe ticker MSFT", "earnings catalyst"],
  "downstream": ["fundamental"]
}
```

Cost: 1 LLM call. Citations: policy:01-firm-charter, policy:02-investment-policy.

→ Trace event: `agent=news_triage`, `kind=agent_call`.

### Step 3 — `idea_generator` confirms eligibility (T+0.6s)

For known firm names, this is a quick policy + universe lookup; for
maiden names it'd do a SEC EDGAR pull. MSFT is known, so:

```json
{
  "ticker": "MSFT",
  "in_universe": true,
  "eligible_for_plan": true,
  "policy_path": "policy:05-new-name-onboarding § 3 (existing position)",
  "current_holding": {"qty": 100, "avg_cost": 401.20}
}
```

→ Trace event: `agent=idea_generator`, retrieved 3 policy chunks.

### Step 4 — `fundamental` builds a thesis (T+0.6→3.1s)

This is the heaviest LLM step. The agent gets:

* The news payload.
* Top-k retrieved chunks from **filings** (last 10-K, 10-Q, latest 8-K).
* Top-k from **past_plans** for MSFT (memory).
* Top-k from **news** (recent MSFT items, last 7 days).

System prompt key constraint: *every numeric or quoted claim must be
tagged with a citation key from the retrieval payload; otherwise return
`refuse=true`.*

The agent returns:

```json
{
  "thesis": "Beat-and-raise reinforces cloud margin trajectory; consistent with our existing thesis from plan:msft-2024.",
  "claims": [
    {"text": "Q1 cloud revenue grew 31% YoY", "citation": "filing:MSFT-2025-Q1:p3"},
    {"text": "Guidance raised by ~$1.5B", "citation": "news:msft-2026-05-19-headline"}
  ],
  "confidence": 0.78,
  "refuse": false
}
```

→ Trace event: `agent=fundamental`, `kind=llm_call`, `citations=[…]`.
The eval harness will count this as **grounded** (≥1 citation).

### Step 5 — `plan_builder` proposes a trade (T+3.4s)

Takes the thesis, applies the **firm capital allocation policy**
(`data/policies/06-capital-allocation.md`), produces:

```json
{
  "ticker": "MSFT",
  "action": "buy",
  "qty": 50,
  "estimated_price": 415.50,
  "notional": 20775.00,
  "rationale": "Reinforce existing position by 50% within sector cap",
  "citations": ["plan:msft-2024", "policy:06-capital-allocation § sizing"],
  "expected_hitl": false
}
```

→ Trace event: `agent=plan_builder`, structured output.

### Step 6 — `plan_supervisor` reviews (T+3.7s)

Cross-checks: thesis is fresh (<24h), citations resolve, plan doesn't
contradict an open position or a recent rejection. Returns
`approved=true`.

→ Trace event: `agent=plan_supervisor`, `outcome=approved`.

### Step 7 — `risk_officer` decides routing (T+3.8s)

Computes notional, concentration, and gross exposure. The risk policy
in `data/policies/03-risk-policy.md` sets thresholds:

```text
HITL thresholds:
  - notional > $50,000 → HITL
  - concentration > 15% NAV → HITL
  - new ticker (maiden) → HITL
```

Our $20,775 buy is below threshold. The officer returns:

```json
{
  "decision": "auto_execute",
  "exposure_after": {"msft_pct_nav": 6.1},
  "thresholds_checked": ["notional", "concentration", "maiden"],
  "citations": ["policy:03-risk-policy § thresholds"]
}
```

→ Trace event: `agent=risk_officer`, `hitl_required=false`.

### Step 8 — `portfolio` executes the paper fill (T+3.9s)

Uses `app/portfolio.py::execute_trade`:

```python
slippage_bp = 5         # 5 basis points
commission = 1.00
last = 415.50
fill_price = last * (1 + slippage_bp/10000)  # 415.71
cash -= 50 * fill_price + commission
holdings["MSFT"]["qty"] += 50
holdings["MSFT"]["avg_cost"] = vwap(...)
```

The fill is written to `firm.sqlite` and the trade row goes to
`trade_history`. NAV is marked to market.

→ Trace event: `event=fill`, `realized_pnl=null` (still open).

### Step 9 — `auditor` records (T+3.95s)

Side-channel: writes a structured `audit:msft:2026-05-19T09:31:03Z`
record summarising the run with all decision points. Picks up any
guardrail breaches from earlier steps (none here).

### Step 10 — Reports fan out (T+3.95s, async)

* **UI** — the firm's home page now shows the new holding and the
  fill in the trade history widget.
* **Excel** — the next scheduled `python -m app.reports` (or
  manual `make report`) snapshots this state into
  `artifacts/reports/2026-05-19/daily.xlsx` with a JSON sidecar.
* **JSON logs** — `event=fill ticker=MSFT qty=50 price=415.71` lands
  in the JSON stream; Loki/CloudWatch/Slack can consume it.

→ Trace event: `event=report_rendered`.

### Step 11 — Trace replay

Open `/run/<run_id>` in the UI. The run is shown as a tree:

```
run_id=42  ticker=MSFT  duration=3.95s
├── news_triage          (0.4s, 1 LLM call, 2 citations)
├── idea_generator       (0.2s, 0 LLM calls, 3 retrievals)
├── fundamental          (2.5s, 1 LLM call, 4 citations)  ← biggest
├── plan_builder         (0.3s, 1 LLM call, 2 citations)
├── plan_supervisor      (0.3s, 1 LLM call, 0 retrievals)
├── risk_officer         (0.1s, 0 LLM calls, decision=auto_execute)
├── portfolio.execute    (0.05s, fill recorded)
└── auditor              (0.05s, audit recorded)
```

A reviewer can click any node and see prompt + response.

---

## Path B — HITL path (same flow, riskier trade)

Same news arrives, but the firm has zero MSFT exposure and the
allocation policy proposes a fresh **$120,000** position (above the
$50,000 notional threshold).

The flow is identical through Step 7. At Step 7:

```json
{
  "decision": "require_hitl",
  "reasons": ["notional $120,000 > threshold $50,000"],
  "exposure_after": {"msft_pct_nav": 12.0},
  "citations": ["policy:03-risk-policy § thresholds"]
}
```

The orchestrator does three things:
1. **Persists the LangGraph state** via the SqliteSaver at the current
   node.
2. **Enqueues a HITL item** in `firm.sqlite` with the proposed trade,
   citations, and a pointer to the saved graph state.
3. **Returns** — the run is paused, not crashed.

The UI's home page top section now shows:

```
HITL — pending review
─────────────────────
MSFT  BUY  qty=300  ~$120,000
  Thesis: Beat-and-raise … (cites filing:MSFT-2025-Q1:p3)
  Risk:   notional $120,000 > $50,000 threshold
  [ Approve ]  [ Reject ]
```

### Operator approves

Clicking **Approve** does three things:
1. Marks the HITL item resolved (`hitl_resolved` event in traces).
2. Loads the saved graph state from `checkpoints.sqlite`.
3. **Resumes execution from the same node** — risk_officer's decision
   is overridden to `auto_execute_with_approval`, and the graph
   proceeds to portfolio.execute as in Path A.

The trace shows the gap between `hitl_required` and `hitl_resolved`
events with the operator id in the resolved event.

### Operator rejects

Clicking **Reject** does:
1. Marks the HITL item rejected.
2. The graph resumes to the **plan_supervisor** node with a forced
   `rejected` outcome.
3. The plan is marked rejected in `firm.sqlite`; no fill.
4. The auditor records the reject reason if the operator supplied one.

### What if the process crashes during the pause?

The graph state is in `checkpoints.sqlite` on a mounted volume; the
HITL queue is in `firm.sqlite`. On boot the new lifecycle bootstrap
runs `hitl_sync.repair_hitl_queue()` to reconcile both. The operator's
queue item is still there when they refresh the page.

---

## Where to point during the live demo

| Phase                | Click / show                                                |
|----------------------|-------------------------------------------------------------|
| 1. Trigger           | UI `/trigger`, fill in a sample event, POST                 |
| 2–7. Agent pipeline  | Watch the home page; the run row appears at the top         |
| 7. HITL              | Top of home page; expand the queue item                     |
| 7→8. Resume          | Click **Approve**; observe the trade history row appear     |
| 11. Trace replay     | Click the run id; tour the per-agent tree                   |
| Observability        | `/metrics` (raw), Grafana panel                             |
| Daily report         | `make report` then open the produced `daily.xlsx`           |
| Eval                 | `make eval` and read the summary                            |

---

## Numbers a reviewer cares about

| Metric              | This run (Path A)        |
|---------------------|--------------------------|
| Agents invoked      | 8                        |
| LLM calls           | 4                        |
| Citations recorded  | 8 across 4 calls         |
| Grounded ratio      | 100% (4/4)               |
| HITL required       | 0                        |
| Guardrail breaches  | 0                        |
| End-to-end latency  | ≈4s (mock LLM)           |

In live mode, the latency is dominated by `gpt-4o-mini` (≈700ms per
call × 4 calls + retrievals ≈ 3–5s). Acceptable for an event-driven
firm; not acceptable for HFT.
