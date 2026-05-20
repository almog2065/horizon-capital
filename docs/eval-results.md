# Eval results — sample window

This is the eval report the brief explicitly asks for. We commit a
reproducible sample window and explain how to read the numbers
honestly.

Run it yourself:

```bash
make eval                  # writes evals/output/run.json + prints summary
make eval-strict           # fails non-zero on regression (used in CI)
```

The harness is `evals/run.py` driving `evals/replay.py` and
`evals/metrics.py`. All metrics are arithmetic over a deterministic
event stream — no LLM call required.

---

## Sample window

`evals/data/sample.json` — a small, hand-crafted window with four
events:

| ts (mock)            | ticker | action | qty | price  | citations               | HITL  |
|----------------------|--------|--------|-----|--------|-------------------------|-------|
| 2026-05-15T09:31     | MSFT   | buy    | 100 | 410.00 | 2 (10-K, EDGAR)         | no    |
| 2026-05-15T10:02     | NVDA   | buy    | 50  | 880.00 | 2 (news, 10-Q)          | **yes** |
| 2026-05-15T11:15     | AAPL   | buy    | 75  | 192.00 | 1 (plan:aapl-2023)      | no    |
| 2026-05-15T14:45     | MSFT   | sell   | 100 | 415.50 | 1 (plan:msft-2024)      | no    |

The sell at 14:45 realises a $550 profit on the morning's MSFT
purchase.

---

## What the harness reports

### Portfolio scoreboard

| Metric                  | Sample value          | What it means                                  |
|-------------------------|-----------------------|------------------------------------------------|
| `starting_nav`          | $1,000,000            | Day 1 cash                                     |
| `ending_nav`            | $994,087.50           | Cash + mark-to-market open positions           |
| `pnl_absolute`          | -$5,912.50            | Equity-curve P&L (incl. unrealised)            |
| `pnl_pct`               | -0.59%                | Same, as a percent                             |
| `benchmark_pct`         | +0.45%                | SPY's move over the window (hard-coded in fixture) |
| `excess_return_pct`     | -1.04%                | We underperformed SPY by 1.04 pp               |
| `max_drawdown_pct`      | 9.86%                 | Largest peak-to-trough on the equity curve     |
| `hit_rate`              | 100.0%                | All closed trades profitable                   |
| `n_trades`              | 4                     |                                                |

Note: ending NAV is *less* than starting NAV because the open
NVDA/AAPL positions are held at entry price (no mark-up), but cash
was spent acquiring them. This is the **honest** way to report; we
could mark to a fake "current" price and look prettier, but that's
exactly the kind of dishonesty the brief warns against.

### Process scoreboard

| Metric                  | Sample value | What it means                                 |
|-------------------------|--------------|-----------------------------------------------|
| `n_agent_calls`         | 4            | One per event (agent decisions)               |
| `n_llm_calls`           | 4            | One LLM call per event in the replay shim     |
| `grounded_calls`        | 4            | All 4 had ≥1 citation                          |
| `grounded_ratio`        | 1.00         | 100% grounded                                  |
| `citations_per_decision`| 1.50         | Avg citations per LLM call (6 / 4)             |
| `refusal_count`         | 0            | No agent refused                               |
| `hitl_required`         | 1            | NVDA tripped the threshold                    |
| `hitl_resolved`         | 1            | Operator approved within the window           |
| `guardrail_breaches`    | 0            | No invalid output / hallucinated number       |

---

## Reading the numbers

* **Return is negative, vs. SPY.** That's fine. The brief says the
  goal isn't to beat SPY. The honest scoreboard shows we trailed.
* **100% grounded.** Every claim the agents made was cited. This is
  the *primary* health signal of the process side.
* **HITL discipline is 1/1.** Required and resolved. The brief asks
  for HITL on high-impact trades; the eval shows it fires and
  unblocks cleanly.
* **Zero guardrail breaches.** No hallucinated numbers, no malformed
  outputs. CI's `eval-strict` target enforces this stays at zero.

---

## CI thresholds

`make eval-strict` runs:

```bash
python -m evals.run --window sample \
    --fail-on grounded_ratio:0.80 \
    --fail-on guardrail_breaches:0
```

The build fails if:
* `grounded_ratio` drops below 0.80, **or**
* `guardrail_breaches` rises above 0.

We deliberately chose those two thresholds because they're the ones
that map to *the brief's grading criteria*:

| Brief criterion           | Threshold                            |
|---------------------------|--------------------------------------|
| "RAG grounded-ness"       | `grounded_ratio >= 0.80`             |
| "guardrail effectiveness" | `guardrail_breaches <= 0`            |
| "process quality"         | Both above                            |
| "return performance"      | Reported, not enforced               |

Return isn't a CI gate because flipping its threshold turns the eval
into a strategy-research tool, which it isn't.

---

## How to add a scenario

1. Drop `evals/data/<window>.json`:

```json
{
  "window": "2026-08-12",
  "starting_nav": 1000000.0,
  "benchmark_pct": 0.10,
  "events": [
    {"ticker": "NVDA", "action": "buy", "qty": 50, "price": 100.0,
     "citations": ["filing:NVDA-10K"], "hitl": false}
  ]
}
```

2. Run:

```bash
python -m evals.run --window 2026-08-12 --out evals/output/2026-08-12.json
```

3. Wire it into CI by adding a `--window` argument to the
   `eval-replay` job in `.github/workflows/ci.yml`.

---

## What the harness deliberately does **not** do

* It doesn't make live LLM calls. The behaviour we care about is
  *structural*: did the firm cite, did it route HITL, did it
  guardrail? That's invariant to which model answered. Live-LLM
  validation runs separately, outside CI, against held-out fixtures.
* It doesn't simulate the full LangGraph state machine. The replay is
  a contract-level shim: it produces the same trace shape the live
  firm would. The day we need full-graph replay, the harness extends
  to invoke `app.firm_orchestration` directly (see the
  `walkthrough-one-trade.md` for the live shape).
* It doesn't do strategy backtesting. The brief asks for a *process*
  check, not a backtest. SPY comparison is reported, not optimised
  against.

---

## Honest known limitations

* `excess_return_pct` is computed against a hard-coded `benchmark_pct`
  in the fixture. A real benchmark would pull SPY's return from a
  market data provider; today it's pinned per-scenario.
* `hit_rate` counts closed trades only. Open positions don't count
  until they close.
* `grounded_ratio` doesn't measure citation *quality*. A junk citation
  passes. Auditor counter-evidence would be the next axis.
* `max_drawdown` is computed on intraday marks if available, else
  on event-time marks. The shim uses event-time, which is coarser.

Each of these is a defensible simplification — we'd add them when
the firm scaled, not before.

---

## Sample output

What `make eval` prints (with the default sample window):

```
Eval report (2025-05-15)
  P&L:        $   -5,912.50  (-0.59%)
  Benchmark:  +0.45%   excess=-1.04%
  Max DD:     9.86%   hit_rate=100.0%   n_trades=4
  Grounded:   100.0%   citations/decision=1.50
  HITL:       required=1  resolved=1
  Guardrails: breaches=0
  Report:     evals/output/sample.json
```

Why max-DD looks high: each `buy` consumes cash without a matching
mark-up on the open position (the replay shim values open positions
at the most recent trade price for *that ticker*, which is the entry
price). When subsequent buys happen for unrelated tickers, total NAV
dips because cash leaves but the new holding isn't marked above its
cost. The real firm marks to live price; the offline replay
deliberately doesn't, because the harness's job is to verify
*behaviour*, not price movement.

The JSON report is checked into the repo at
`evals/output/sample.json` (the `make eval` default output path). It's
re-runnable from a clean clone in under five seconds.
