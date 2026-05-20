# Live demo script

Choreography for the 10-minute live demo the brief calls for. Pair
this with `walkthrough-one-trade.md` (which is the narration of one
trade end-to-end).

Time budget: 10 minutes total. If you go over, drop steps 7 and 8.

---

## Before the meeting

**T-30 minutes** (do this *before* the call):

```bash
cd new-cato/horizon_capital_prod
cp .env.example .env
# If you have an OpenAI key, put it in .env. Otherwise the firm runs in mock mode.
make build && make up
make obs-up                # adds Prom + Grafana
sleep 20                   # wait for services to settle
./scripts/demo.sh          # idempotent — runs the full demo flow once
```

Verify:

```bash
curl -fsS http://127.0.0.1:8080/readyz | python3 -m json.tool
```

If `status: ok`, you're ready.

Open these tabs in the browser:

1. http://127.0.0.1:8080 — firm UI
2. http://127.0.0.1:8080/readyz — health
3. http://127.0.0.1:8080/metrics — Prometheus text (read-only)
4. http://127.0.0.1:3000 — Grafana (admin/admin) — pre-pin the
   `Horizon Capital — Overview` dashboard
5. `infra/terraform/main.tf` in the IDE
6. `docs/walkthrough-one-trade.md` in the IDE

---

## Demo flow (10 minutes)

### Step 0 — Open (0:00–0:30)

Show the firm UI tab. One sentence:

> "Horizon Capital is a paper-trading desk run by nine AI agents,
> with humans on the Risk Committee. Let me walk you through one
> trade end to end."

### Step 1 — Architecture (0:30–1:30)

Switch to `architecture.svg` (or `docs/architecture-diagrams.md`).
30 seconds:

> "Three deployable units: a web tier, a scheduler worker, and a
> migrate job — sharing one image. Postgres for state, Redis for
> cache and locks, nginx in front for TLS and gzip. Same image
> deploys to Compose, Kubernetes, or AWS ECS."

### Step 2 — Trigger a news event (1:30–2:30)

Back to the firm UI. Go to `/trigger`. Submit a sample news event
for MSFT (or whichever ticker is in the demo seed).

Narrate what's happening in real time:

> "News arrives. The triage agent decides it's relevant.
> Idea_generator confirms MSFT is in our universe. Fundamental does
> the heavy lift — pulls top-k from filings, news, past plans, and
> grounds a thesis with citations…"

The home page should now show the new run row at the top of "Recent
runs". Click into it.

### Step 3 — Trace replay (2:30–4:00)

In the run detail page, walk through the agent tree:

> "Here's every agent invocation. Each row is one LLM call: model,
> mode (mock/live), prompt, response, citations. A reviewer can replay
> any decision from this trace alone."

Click into the `fundamental` node specifically:

> "Look at this — the thesis claims revenue grew 31%, and it points
> to filing:MSFT-2025-Q1:p3. That's the eval harness's
> `grounded_ratio` metric: percent of LLM calls that cite something.
> If a claim isn't cited, the agent refuses."

### Step 4 — HITL approval (4:00–5:30)

Go back to the home page. Trigger a second news event that the seed
data is rigged to produce a high-notional plan (or just point to the
HITL queue widget at the top).

> "The risk officer is the bouncer. If a proposed trade exceeds the
> notional or concentration threshold from the risk policy, the
> graph pauses, persists its state via LangGraph's SqliteSaver, and
> queues this item for the Risk Committee."

Show the queue item. Click **Approve**.

> "The graph resumes from the checkpoint. The fill happens. If we
> killed the container during the pause and restarted, the operator
> would still see this exact item — that's the brief's 'graph state
> persists across the wait'."

### Step 5 — Observability (5:30–6:30)

Switch to the Grafana tab.

> "Two metrics today, deliberately: `horizon_runs_total` (counter)
> and `horizon_hitl_pending` (gauge). The Prometheus rules file ships
> two baseline alerts — one for the web tier being unreachable, one
> for the HITL backlog growing. Receiver is configured per env."

Click around the dashboard for 30 seconds. Show the `up` graph
spiking when you bring the web down (don't actually take it down
during a demo — show a screenshot if you have one).

### Step 6 — Daily report (6:30–7:30)

Back to the terminal:

```bash
make report-demo
ls -la artifacts/reports/
```

Open the resulting `daily.xlsx` (or run `python -m app.reports --demo` from your host).

> "Brief says deliver reports through at least two channels. UI is
> one. This Excel sheet — generated stdlib-only, no openpyxl — is
> two. The JSON sidecar next to it is three. Anything that ingests
> JSON lines can be channel four."

### Step 7 — Eval (7:30–8:30)

```bash
make eval
```

Read the output:

> "Portfolio scoreboard: pnl, vs SPY, max drawdown, hit rate. Process
> scoreboard: grounded_ratio, citations_per_decision, refusals,
> HITL discipline, guardrail breaches. CI runs this on every push;
> `make eval-strict` fails the build when grounded_ratio drops
> below 80% or any breach is recorded."

### Step 8 — IaC + CI/CD (8:30–9:30)

Open `infra/terraform/main.tf`. Don't read it line by line — point
at the structure:

> "VPC with public + private subnets, RDS Postgres with multi-AZ in
> prod, ElastiCache Redis, ECR with immutable tags and scan-on-push,
> ECS Fargate for web + worker, ALB with deployment circuit breaker
> auto-rollback. About thirty resources. Per-env workspaces, dev and
> prod, separate tfvars."

Then `.github/workflows/release.yml`:

> "Three workflows. CI on every push: lint, pytest, eval harness,
> docker build. Release on tagged: build, push to ECR via OIDC, force
> new ECS deployment, wait for stability. Terraform PRs run a plan;
> manual dispatch for apply. No long-lived AWS keys."

### Step 9 — Close: what would you build next (9:30–10:00)

> "Three things would unlock the next 10x of scale. One: leader
> election for the worker, so I can run more than one. Two: pgvector
> migration to retire the SQLite vector store. Three: cost-aware
> model routing — pick gpt-4o-mini vs gpt-4o per agent purpose.
> Each is small. Each is bounded."

End there.

---

## Recovery plays

If the demo breaks live, you have options.

### The web pod is unhealthy
```bash
make web-logs       # show the cause
make restart        # bounce it
curl -fsS http://127.0.0.1:8080/readyz
```

If it doesn't recover, pivot: open `tests/test_excel_reporter.py`
and `tests/test_eval_metrics.py` — the 20 tests demonstrate
behaviour without needing the stack.

### OpenAI is down
You're already in mock mode. Show that the deterministic mock makes
the demo possible. The brief's "production readiness" includes
graceful degradation; this is graceful degradation.

### Docker isn't installed on the demo machine
Pivot to the eval harness:
```bash
python -m evals.run --window sample
python -m app.reports --demo
```
Both run without the stack and produce real artifacts.

---

## What to *not* do

* Don't apologise. Walk it back to a confident "this is what we
  ship today."
* Don't list features. Show **the one trade**. Point at the trace.
* Don't promise more agents. Show that adding one is bounded.
* Don't open `app/graph.py` — it's 1.3k lines and will swallow
  your remaining time. Open `docs/walkthrough-one-trade.md` if you
  need to point at the orchestration.
* Don't show every Grafana panel. The point isn't dashboards; it's
  that the metrics exist and the system is observable.
