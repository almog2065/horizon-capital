"""Reproducible eval harness for the firm.

Two reports per run:
  * Portfolio metrics (return, vs. SPY benchmark, max drawdown, hit rate)
  * Process metrics  (grounded-ness, citation rate, refusal rate,
                       guardrail breaches, HITL rate)

See evals/run.py for the entry point and evals/README.md for usage.
"""
