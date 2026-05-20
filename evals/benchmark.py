"""SPY (or scenario) benchmark return for eval windows — offline, CI-safe."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_BENCHMARKS_PATH = Path(__file__).resolve().parent / "data" / "spy_benchmarks.json"
_DEFAULT_SYMBOL = "SPY"


def _load_table() -> dict[str, Any]:
    if not _BENCHMARKS_PATH.exists():
        return {"windows": {}, "default_pct": 0.0}
    with _BENCHMARKS_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def resolve_benchmark(scenario: dict) -> dict[str, Any]:
    """
    Return benchmark metadata for a replay scenario.

    Priority:
      1. Explicit ``benchmark_pct`` in scenario (documented SPY return for window)
      2. ``evals/data/spy_benchmarks.json`` keyed by ``window`` id
      3. ``default_pct`` from table or 0.0
    """
    symbol = str(scenario.get("benchmark_symbol") or _DEFAULT_SYMBOL)
    if "benchmark_pct" in scenario:
        pct = float(scenario["benchmark_pct"])
        source = "scenario"
    else:
        table = _load_table()
        windows = table.get("windows") or {}
        window = str(scenario.get("window") or "")
        if window in windows:
            entry = windows[window]
            pct = float(entry.get("return_pct", entry) if isinstance(entry, dict) else entry)
            source = "spy_benchmarks.json"
        else:
            pct = float(table.get("default_pct", 0.0))
            source = "default"
    return {
        "symbol": symbol,
        "return_pct": pct,
        "source": source,
        "window": scenario.get("window"),
    }
