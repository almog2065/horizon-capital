"""Eval harness entry point.

Usage:
    python -m evals.run --window sample --out evals/output/run.json
    python -m evals.run --fail-on grounded_ratio:0.80
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.core.logging import get_logger, setup_logging

from .metrics import compute_cost, compute_portfolio, compute_process
from .replay import replay

log = get_logger("horizon.eval")


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    p = argparse.ArgumentParser(description="Horizon Capital eval harness")
    p.add_argument("--window", default="sample", help="scenario name under evals/data/")
    p.add_argument("--out", default="evals/output/eval.json", help="path to write report JSON")
    p.add_argument(
        "--fail-on",
        action="append",
        default=[],
        help="threshold like grounded_ratio:0.8 or pnl_pct:-5  (repeatable)",
    )
    args = p.parse_args(argv)

    result = replay(args.window)
    pm = compute_portfolio(
        starting_nav=result.starting_nav,
        equity_curve=result.equity_curve,
        benchmark_pct=result.benchmark_pct,
        trades=result.trades,
    )
    proc = compute_process(result.traces)
    cost = compute_cost(result.traces)

    report = {
        "window": result.window,
        "reproducible": True,
        "mock_llm": True,
        "benchmark": {
            "symbol": result.benchmark_symbol,
            "return_pct": result.benchmark_pct,
            "source": result.benchmark_source,
        },
        "portfolio": pm.as_dict(),
        "process": proc.as_dict(),
        "cost": cost.as_dict(),
        "equity_curve": result.equity_curve,
        "trades": result.trades,
        "trace_summary": {
            "n_events": len(result.traces),
            "kinds": sorted({t.get("kind") for t in result.traces if t.get("kind")}),
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    log.info("eval-report-written path=%s", out_path)

    # Print a human-readable summary
    print(f"\nEval report ({result.window})")
    print(f"  P&L:        ${pm.pnl_absolute:>12,.2f}  ({pm.pnl_pct:+.2f}%)")
    print(
        f"  Benchmark:  {result.benchmark_symbol} {pm.benchmark_pct:+.2f}%"
        f"   excess={pm.excess_return_pct:+.2f}%"
    )
    print(f"  Max DD:     {pm.max_drawdown_pct:.2f}%   hit_rate={pm.hit_rate:.1f}%   n_trades={pm.n_trades}")
    print(f"  Grounded:   {proc.grounded_ratio*100:.1f}%   citations/decision={proc.citations_per_decision:.2f}")
    print(f"  Decision:   quality={proc.decision_quality*100:.1f}%   HITL discipline={proc.hitl_discipline*100:.1f}%")
    print(
        f"  Guardrails: {proc.guardrail_passed}/{proc.guardrail_checks} passed"
        f"   effectiveness={proc.guardrail_effectiveness*100:.1f}%"
        f"   breaches={proc.guardrail_breaches}"
    )
    print(f"  Est. cost:  ${cost.total_usd:.4f}  ({cost.n_llm_calls} LLM calls, {cost.prompt_tokens + cost.completion_tokens:,} tokens)")
    print(f"  Report:     {out_path}")

    # Threshold checks (cause non-zero exit in CI)
    failures: list[str] = []
    for spec in args.fail_on:
        try:
            key, val = spec.split(":")
            threshold = float(val)
        except ValueError:
            log.warning("ignoring malformed --fail-on=%s", spec)
            continue
        merged = {**report["portfolio"], **report["process"]}
        if key not in merged:
            failures.append(f"{spec}: unknown metric")
            continue
        actual = float(merged[key])
        if key.endswith("_count") or key in ("guardrail_breaches", "refusal_count"):
            if actual > threshold:
                failures.append(f"{spec}: actual={actual} > threshold={threshold}")
        else:
            if actual < threshold:
                failures.append(f"{spec}: actual={actual} < threshold={threshold}")

    if failures:
        print("\nFAILED thresholds:")
        for f in failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
