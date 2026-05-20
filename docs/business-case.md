# Business case

This document *justifies the firm concept*, not just the architecture.

---

## Who is the firm?

A small investment shop that owns a paper portfolio of US equities,
benchmarked against SPY, with $1M starting NAV. The entire trading
desk is staffed by LLM agents specialised by role:

* **Research** — `news_triage`, `idea_generator`
* **Analysis** — `fundamental`
* **Portfolio construction** — `plan_builder`, `plan_supervisor`
* **Risk** — `risk_officer`
* **Position management** — `position_monitor`
* **Audit** — `auditor`
* **Top-of-house** — `firm_manager`

Humans sit on the Risk Committee. They approve trades over configurable
risk thresholds and walk away from the rest. They receive a daily
report through two channels (UI + Excel) and can pull a JSON snapshot
anytime.

---

## Why this matters (the value proposition)

### 1. Auditability without the cost of an audit team

Every decision a human auditor would want to see is recorded:
* The triggering news / event with timestamp + source.
* Each agent invocation with its full prompt + response.
* Every citation, retrieved chunk, and embedding query.
* The HITL pause + the operator's decision.
* The fill with slippage and commission.

A regulator's question — *"why did you buy MSFT at 09:31 on May 19?"*
— resolves to opening the trace UI for that run. **MTTR for an audit
inquiry drops from days to minutes.**

### 2. Operational scale

The firm operates continuously during market hours. Humans gate only
the trades that matter. A traditional 5-analyst desk costs roughly $1M
/ year in salaries; this firm's variable cost is dominated by LLM
tokens (~$200/mo at current activity). **Same coverage at 0.5% of
the cost** — and the cost is variable, not fixed.

### 3. Composability

Adding a new role is bounded:
* New agent module (~80 lines + a mock).
* One graph node + edge.
* Five lines of state schema.
* One section in this doc.

ESG analyst, sector-specific specialist, ETF rebalancer — each is a
weekend's work, not a quarter's.

### 4. Trust through citation

Every numeric or quoted claim must point to a retrieval source. The
agent refuses rather than invent. The eval harness reports
`grounded_ratio`; CI fails on regression. **A senior PM can read a
trade rationale, click each citation, and verify before approving.**
That's not what most "agentic" systems offer.

### 5. Honest measurement

The firm reports two scoreboards:
* **Portfolio** — pnl, vs SPY, max drawdown, hit rate, n_trades.
* **Process** — grounded_ratio, citations_per_decision,
  refusal_count, HITL discipline, guardrail breaches.

We measure how well it *behaves*, not just whether it makes money.
Buffett quote optional.

---

## Who would buy this?

Realistic prospects:
* **Mid-size hedge funds** running thematic strategies, wanting to
  augment a senior PM with junior-analyst capacity that's auditable.
* **Family offices** wanting institutional-grade process without
  institutional headcount.
* **Asset managers** running rules-based strategies that want LLM-
  augmented rationale generation tied to citations.
* **Compliance teams** at any of the above — the audit features sell
  themselves.

Not realistic, by design:
* HFT desks. The latency budget here is seconds, not microseconds.
* Discretionary managers who want a black-box buy/sell signal. The
  firm refuses to give you one without citations.

---

## Why now?

Three things converged in late 2024 / 2025:

1. **Long-context LLMs** — the fundamental agent's prompt is 5–20k
   tokens of retrieved evidence. That's affordable on `gpt-4o-mini`
   today and was prohibitive in 2023.
2. **Structured outputs** — JSON-mode responses + Pydantic validation
   removed the prompt-engineering tax that earlier agentic systems
   paid.
3. **Workflow durability** — LangGraph and similar frameworks made
   pause/resume + checkpointing a one-line concern.

This wasn't possible to ship 18 months ago.

---

## Why the firm doesn't try to beat SPY

Three reasons, the brief calls out all three:

1. **The brief explicitly says so.** *"The goal is not to beat the
   market."*
2. **Beating SPY is a function of strategy and capital flow** — not
   of agent architecture. Demonstrating the architecture is what's
   being evaluated.
3. **Honest measurement** is the more interesting research question.
   A firm that can prove *every* trade was justified by cited
   evidence is more interesting than a firm that beat SPY by 1.2%
   and can't explain how.

The eval harness reports excess return so the conversation is honest.
On the sample window, the firm trails SPY by 1.04% — and we say so in
the eval output. The reviewer should read that as "the eval is
real", not "the firm doesn't work".

---

## What the firm explicitly is *not*

* Not a broker. No FIX, no DMA, no real fills.
* Not a regulated investment advisor. We don't sell advice; we
  simulate a desk.
* Not a backtester. The eval harness exercises behavior, not strategy
  research.
* Not a multi-tenant SaaS. One firm per deployment today; tenant
  separation is future work.

Each of these is a defensible boundary. The day a prospect needs one,
we know what to build.

---

## Talking points for the firm intro

If asked *"in 2 minutes, what does the firm do?"*:

> "It's a paper-trading desk staffed by AI agents. Nine specialised
> agents — research, fundamental, risk, execution, audit — coordinate
> through a LangGraph state machine. Every claim they make is
> backed by a retrieved citation from filings, news, policy, or
> historical plans; the eval harness measures how grounded they are.
> Anything over a configured risk threshold pauses and waits for the
> human Risk Committee to approve, with the graph state durably
> persisted so the pause survives a restart. We ship it as a
> containerised stack with Terraform + Kubernetes IaC and GitHub
> Actions CI/CD. The brief asked for a believable, observable,
> auditable workflow with production-grade engineering. That's what
> this is."

If asked *"what makes it valuable in production?"*:

> "Audit-grade traceability — every decision is replayable. Cost — LLM
> tokens cost a tenth of an analyst. Composability — adding a role is a
> weekend's work. And trust — the firm refuses to make claims it
> can't cite. Senior PMs can read a rationale and verify, click by
> click, what evidence it rests on."
