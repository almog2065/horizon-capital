# Technical Overview

Senior-level walkthrough of the system. Pair with `architecture.svg`
(logical view) and the runbooks under `docs/runbooks/`.

## System contract

> Build a multi-agent system that operates an AI-run investment firm,
> with persistent paper portfolio, multi-agent design, RAG-grounded
> decisions, HITL risk committee, observability, and reproducible
> evals.  — Cato Networks home task brief

What we ship:

* **4+ specialized agents** (news_triage, fundamental, idea_generator,
  plan_builder, plan_supervisor, risk_officer, position_monitor,
  auditor, firm_manager) with typed I/O contracts.
* **LangGraph orchestration** with sqlite checkpointer so HITL pauses
  survive process restarts.
* **RAG** over four corpora (policy, news, filings, past_plans) with
  citation discipline and refusal-on-insufficient-evidence.
* **HITL Risk Committee** that pauses trades over configurable risk
  thresholds.
* **Observability**: every LLM call and tool call recorded locally and
  optionally shipped to LangSmith. Structured JSON logs.
* **Reproducible eval harness** with portfolio AND process metrics.
* **Production infra**: container per role, Postgres + Redis, Nginx,
  Terraform (AWS), Kubernetes (Kustomize), GitHub Actions CI/CD,
  Prometheus + Grafana.

## Process model

```
┌─────────────────────────────────────────────────────────────┐
│   Container topology (compose)                              │
│                                                             │
│  nginx :80/443 ──► web (FastAPI, x2) ──► postgres + redis   │
│                          ▲                                  │
│   external scheduler ◄── worker (1)  ── shares image/deps   │
└─────────────────────────────────────────────────────────────┘
```

* **web** — request/response only. Stateless after the lifespan boot.
  Horizontally scalable. `RUN_SCHEDULER_IN_API=false`.
* **worker** — owns the firm's clocks. `plan_supervision_loop` +
  `firm_balance_loop`. Single replica until leader election lands
  (see ADR 0001).
* **migrate** — one-shot bootstrap (DB init, RAG corpus seed, HITL
  queue repair). Runs as a profile or a K8s Job.

## Code layout

```
app/
├── core/                # production primitives — settings, logging,
│   │                    # lifecycle, health. NO upward deps.
│   ├── settings.py      # Pydantic Settings, Docker secrets, single source of truth
│   ├── logging.py       # JSON + text formatters, uvicorn bridge
│   ├── lifecycle.py     # FastAPI lifespan, shared with worker
│   └── health.py        # liveness / readiness probes
├── api/                 # HTTP composition
│   ├── app_factory.py   # create_app() — request_id mw, CORS, TrustedHost
│   └── routes/
│       └── health.py    # /healthz /readyz /version /metrics
├── workers/
│   └── scheduler_worker.py   # python -m app.workers.scheduler_worker
├── agents/              # the firm's roles — preserved
├── main.py              # legacy FastAPI UI routes — preserved
├── config.py            # back-compat shim over core.settings
└── ...                  # graph.py, portfolio.py, rag.py, traces.py, ...
```

## Configuration

12-factor. All knobs are env vars. `app/core/settings.py` is the only
place that reads from env / `.env` / `/run/secrets/*`. Other modules
get values via `get_settings()`. Defaults are tuned so a clone +
`make up` works without any env at all (mock LLM, sqlite, single AZ).

## Persistence

| Concern              | Local (dev)              | Cloud (prod)                |
|----------------------|--------------------------|-----------------------------|
| Firm state           | `firm.sqlite` volume     | RDS Postgres (planned cutover) |
| RAG vector store     | `vectors.sqlite` volume  | (same; will migrate to pgvector)|
| Ops (alerts, plans, dossiers) | `ops.sqlite` volume | (same)                      |
| Bootstrap seed only  | `data/`, `data/bootstrap/` | read at first boot          |
| HITL queue           | `firm.sqlite` table      | (same)                      |
| Traces               | `firm.sqlite` + LangSmith | LangSmith primary, sqlite mirror|

See [`persistence.md`](persistence.md) for the full map.

Crash recovery on boot:
* `db.recover_stale_running_runs(max_age_sec=900)`
* `firm_orchestration.recover_stale_balance_runs()`
* `hitl_sync.repair_hitl_queue()`

## Guardrails

* Input validation: Pydantic models at every agent boundary.
* Output validation: structured JSON via `chat_json(... response_format=...)`.
* Hallucination check: citations required for any non-mock LLM output;
  the eval harness measures `grounded_ratio`.
* Trading limits: HITL pauses any trade above the configured threshold;
  `HITL_MAIDEN_ONLY`, `BLOCK_DUPLICATE_PIPELINE`, etc.

## Observability

* **Logs** — JSON to stdout. Docker `json-file`, rotated 10MB × 5. Ship
  to Loki / CloudWatch with no transformation.
* **Metrics** — `/metrics` Prometheus text. `horizon_runs_total`,
  `horizon_hitl_pending` shipped by default. Extend in
  `app/api/routes/health.py`.
* **Traces** — `app/traces.py` records every agent + LLM call to
  sqlite, optionally mirrored to LangSmith.
* **Dashboards** — pre-provisioned `Horizon Capital — Overview` under
  `observability/grafana/dashboards/`.
* **Alerts** — baseline rules in `observability/prometheus/rules.yml`.

## CI/CD

GitHub Actions workflows under `.github/workflows/`:

| Workflow      | When                       | Does                          |
|---------------|----------------------------|-------------------------------|
| `ci.yml`      | every push / PR            | lint → pytest → eval → docker build |
| `terraform.yml` | infra PRs / manual        | plan / apply (per env)        |
| `release.yml` | tag `v*.*.*` / dispatch    | build & push to ECR, deploy ECS|

All workflows use OIDC into AWS — no long-lived secrets.

## What we'd build next (per the brief's "next three things")

1. **Leader election for the worker** — enables `replicas: >1`.
2. **`pgvector` migration** — retire SQLite for the vector store.
3. **Cost-aware model routing** — pick `gpt-4o-mini` vs `gpt-4o` per
   `purpose` in `app/llm.py::chat_json`.
