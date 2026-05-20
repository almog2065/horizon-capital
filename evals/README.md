# Horizon Capital — Eval Harness

Reproducible historical replay. Reports **portfolio performance vs SPY**
**and** **process quality** (grounded-ness, citations, refusals, HITL,
guardrail breaches) — the brief calls out both axes explicitly.

## Quick start

```bash
python -m evals.run --window sample --out evals/output/run.json
```

Output (also written to `evals/output/run.json`):

```
Eval report (2025-05-15)
  P&L:        $       550.00   (+0.06%)
  Benchmark:  +0.45%   excess=-0.39%
  Max DD:     2.41%   hit_rate=100.0%   n_trades=4
  Grounded:   100.0%   citations/decision=1.50
  HITL:       required=1  resolved=1
  Guardrails: breaches=0
```

## CI thresholds

`--fail-on metric:threshold` makes the harness non-zero-exit when a
metric is below (or, for `_count` / `guardrail_breaches`, above) the
threshold. Wire it into CI to catch regressions:

```bash
python -m evals.run \
  --fail-on grounded_ratio:0.80 \
  --fail-on guardrail_breaches:0 \
  --fail-on hit_rate:50
```

## Adding a scenario

Drop a file under `evals/data/<window>.json` matching the schema in
`evals/data/sample.json`:

```json
{
  "window": "2025-05-15",
  "starting_nav": 1000000.0,
  "benchmark_pct": 0.45,
  "events": [ { "ticker": "MSFT", "action": "buy", "qty": 100, "price": 410, "citations": ["..."], "hitl": false } ]
}
```

Then `python -m evals.run --window 2025-05-15`.

## Architecture

```
evals/
├── run.py       CLI entry point + threshold checks
├── replay.py    deterministic event-driven replay engine
├── metrics.py   PortfolioMetrics + ProcessMetrics (pure funcs)
└── data/
    └── sample.json    minimal scenario (always present)
```

The harness deliberately runs in **mock-LLM mode** by default
(`USE_MOCK_LLM=1`) so CI runs are deterministic and free. To exercise
live LLM grounding instead, unset that env var and supply
`OPENAI_API_KEY`.

## Metrics catalogue

| Category   | Metric                    | Where                  |
|------------|---------------------------|------------------------|
| Portfolio  | `pnl_pct`                 | `PortfolioMetrics`     |
|            | `benchmark_pct`           |                        |
|            | `excess_return_pct`       |                        |
|            | `max_drawdown_pct`        |                        |
|            | `hit_rate`                |                        |
|            | `n_trades`                |                        |
| Process    | `grounded_ratio`          | `ProcessMetrics`       |
|            | `citations_per_decision`  |                        |
|            | `decision_quality`        | composite 0–1 score    |
|            | `guardrail_effectiveness` | checks passed / total  |
|            | `hitl_discipline`         | resolved / required    |
|            | `refusal_count`           |                        |
|            | `hitl_required`           |                        |
|            | `hitl_resolved`           |                        |
|            | `guardrail_breaches`      |                        |

**SPY benchmark:** each scenario sets `benchmark_symbol: "SPY"` and
`benchmark_pct` (offline return for the window, also in
`evals/data/spy_benchmarks.json`). Portfolio report includes
`excess_return_pct` vs that benchmark.
