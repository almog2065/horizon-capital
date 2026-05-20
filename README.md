# Horizon Capital — Production Build (Submission)

A multi-agent **AI Investment Firm** running as a containerised stack
with full **IaC**, **CI/CD**, **observability**, and a **reproducible
eval harness** — the senior-level production refactor of the original
`horizon_capital` demo, built to the spec in the home-task brief.

> **👋 Non-technical reader?** Start with **[`docs/how-it-works-simple.md`](docs/how-it-works-simple.md)** —
> a 3-minute, no-jargon tour of the firm with one big picture (`architecture_simple.png`).
>
> **🎯 Reviewer?** Jump to the **[Deliverables map](#deliverables-map-spec--file)**
> at the bottom of this file, then walk [`docs/README.md`](docs/README.md)
> for the structured reading order.

## Architecture diagrams (at the repo root)

| File | Audience | What it shows |
|------|----------|---------------|
| `architecture_simple.{png,svg,dot}` | non-technical | The firm as a team of AI workers, in plain English |
| `architecture.{png,svg,dot}` | engineers | Logical view: agents, RAG, HITL, audit, tools |
| `architecture_deployment.{png,svg,dot}` | operators | Containers, cloud, observability, CI/CD |
| [`docs/system-flow-diagrams.html`](docs/system-flow-diagrams.html) | engineers | Detailed SVG flow diagrams — scheduler, RAG, each of the 9 agents (inputs/outputs/consumers), and a full agent-interaction map. Hebrew annotations; renders standalone in any browser, dark-mode aware. |

```
        ┌──────────┐         ┌──────────┐          ┌────────────┐
internet│  nginx   │──http──►│   web    │ ────────►│  postgres  │
       ►│  (80/443)│         │ FastAPI  │          └────────────┘
        └──────────┘         │  Uvicorn │          ┌────────────┐
                             └────┬─────┘ ────────►│   redis    │
                                  │                └────────────┘
                                  │                ┌────────────┐
                       ┌──────────┴────┐           │ prometheus │
                       │    worker     │           │  + grafana │
                       │  (scheduler)  │           └────────────┘
                       └───────────────┘
```

## What's in the box

| Layer            | What                                                                |
|------------------|---------------------------------------------------------------------|
| **App core**     | Pydantic settings, JSON logging, lifespan, `/healthz`+`/readyz`+`/metrics`|
| **Web**          | FastAPI / Uvicorn, request-id middleware, CORS, TrustedHost, gzip   |
| **Worker**       | Dedicated container — plan supervision + balance loops               |
| **Agents**       | 9 specialised agents (news_triage, fundamental, risk_officer, …)    |
| **Asset classes**| 4 — equity (yfinance/EDGAR), crypto (CoinGecko), commodity proxies, rates/FX (Frankfurter) — all keyless |
| **RAG**          | 4 corpora (policy, news, filings, past_plans), citation discipline  |
| **HITL**         | LangGraph checkpoints survive restart; risk-committee approval flow |
| **Output channels** | **3** — Web UI, Excel daily report (stdlib-only), JSON log stream |
| **Persistence**  | `firm.sqlite` + `vectors.sqlite` + `ops.sqlite` on a shared volume (Compose) — see [`docs/persistence.md`](docs/persistence.md) |
| **Backing**      | Postgres 16, Redis 7, Nginx 1.27                                    |
| **Docker**       | Multi-stage, non-root, tini PID 1, healthchecks, resource limits    |
| **Compose**      | 6 services + observability overlay                                  |
| **IaC**          | **Terraform** (AWS: VPC/RDS/Redis/ECR/ECS/ALB) + **Kubernetes** (Kustomize, base + dev/prod overlays) |
| **CI/CD**        | GitHub Actions: lint, pytest, eval, docker build, terraform, ECR/ECS deploy via OIDC |
| **Observability**| Prometheus scrape, baseline alerts, pre-provisioned Grafana dashboard |
| **Evals**        | Deterministic replay; portfolio AND process metrics; CI thresholds  |
| **Docs**         | README, technical overview, ADRs, runbooks                          |

## Quick start (local)

Requirements: Docker 24+ and Docker Compose v2.

```bash
cp .env.example .env
make build                 # build the app image
make up                    # bring up: nginx, web×2, worker, postgres, redis
open http://127.0.0.1:8080
```

Add Prometheus + Grafana:
```bash
make obs-up
# Grafana: http://127.0.0.1:3000  (admin / admin)
```

Run the eval harness:
```bash
make eval          # writes evals/output/run.json + prints summary
make eval-strict   # exits non-zero on regression (used in CI)
```

Render the daily Excel report (channel #2):
```bash
make report-demo   # synthetic data
make report        # live firm state (requires stack running)
# or directly:
python -m app.reports --demo
```

Run the full demo flow (idempotent):
```bash
make demo          # ./scripts/demo.sh
```

## Common operations

```bash
make help             # show all targets
make web-logs         # tail FastAPI logs
make worker-logs      # tail scheduler logs
make smoke            # smoke test inside the container
make test             # pytest inside the container
make db-shell         # psql
make migrate          # idempotent bootstrap (init_db, RAG seed)
make k8s-check        # verify kubectl before k8s-dev / k8s-dev-local
make down             # stop (volumes persist)
make clean            # stop and wipe volumes (destructive)
```

Runtime data (holdings, RAG, ops alerts, daily plans) lives in Docker volume
`horizon-capital_artifacts`, **not** under `./artifacts/` in the repo when using
Compose. Bootstrap seed only: `data/` + `data/bootstrap/`. Wipe alerts without
full reset: see `docs/persistence.md`.

## Deploy to AWS (Terraform)

```bash
make tf-init
ENV=dev make tf-plan
ENV=dev make tf-apply         # provisions VPC, RDS, Redis, ECR, ECS, ALB
# CI/CD pushes images to ECR and forces a new ECS deployment.
```

See `infra/terraform/README.md` for details.

## Deploy to Kubernetes

Requires a cluster and kubectl context (`make k8s-check`).

**Local kind (recommended for trying K8s manifests):**

```bash
brew install kind
kind create cluster --name horizon
kubectl config use-context kind-horizon

make k8s-dev-local    # build image, kind load, apply dev overlay, wait for web
make k8s-status-dev   # namespace horizon-capital-dev (not horizon-capital)
kubectl -n horizon-capital-dev port-forward svc/web 8080:80
open http://127.0.0.1:8080
```

`kind` does not see your local Docker images until you load them
(`horizon-capital:latest`). The dev overlay uses `imagePullPolicy: Never` so
pods do not pull from Docker Hub. After code changes you must **reload the image
and restart pods** — `make k8s-dev-local` does both; a plain `kubectl apply`
with the same tag does not replace running containers.

**Manifests only (image already on nodes / registry):**

```bash
make k8s-dev
make k8s-status-dev
```

| Target | Purpose |
|--------|---------|
| `make k8s-check` | Verify kubectl context + API |
| `make k8s-build-image` | `docker compose build` → `horizon-capital:latest` |
| `make k8s-load-image` | `kind load docker-image` into cluster `horizon` |
| `make k8s-dev-local` | build + load + apply + rollout wait |
| `make k8s-status-dev` | Pods/svc in `horizon-capital-dev` |
| `make k8s-prod` | Prod overlay (`horizon-capital` namespace) |

**Notes:** Dev uses namespace `horizon-capital-dev`, in-cluster Postgres/Redis,
and `emptyDir` for `/app/artifacts` (firm state resets if the web pod is
recreated). Ingress needs an controller in kind; `port-forward` is simplest for
demos. If web stays `0/1 Ready`, check redis/postgres pods and `/readyz` logs.

See [`infra/k8s/README.md`](infra/k8s/README.md). Terraform (AWS) and K8s reuse
the same application image.

## Layout

```
horizon_capital_prod/
├── app/
│   ├── core/             # settings, logging, lifecycle, health
│   ├── api/              # app_factory + /healthz /readyz /version /metrics
│   ├── workers/          # scheduler_worker.py
│   ├── agents/           # 9 firm agents (preserved)
│   ├── main.py           # legacy FastAPI UI (preserved, wired to new lifecycle)
│   └── …                 # graph, portfolio, rag, traces, …
├── docker/               # nginx + postgres init
├── docker-compose.yml
├── docker-compose.observability.yml
├── Dockerfile            # multi-stage, non-root, tini
├── Makefile
├── infra/
│   ├── terraform/        # AWS: VPC, RDS, Redis, ECR, ECS Fargate, ALB
│   └── k8s/              # Kustomize: base + overlays/{dev,prod}
├── observability/
│   ├── prometheus/       # config + alert rules
│   └── grafana/          # provisioned datasource + dashboards
├── evals/                # reproducible replay harness
├── docs/
│   ├── technical-overview.md
│   ├── persistence.md    # where runtime data lives (Compose volume vs repo)
│   ├── adr/              # architecture decision records
│   └── runbooks/         # operational playbooks
├── tests/                # pytest
└── .github/workflows/    # ci.yml, release.yml, terraform.yml
```

## Configuration

Single source of truth: `app/core/settings.py` (Pydantic Settings).
Every knob is an env var. See `.env.example` for defaults and the
technical overview for the full list. Production should use Docker
secrets / Vault / External Secrets — the settings layer reads
`/run/secrets/<name>` automatically.

## Production checklist

Covered out of the box:

- [x] Non-root container user (uid 1000), tini PID 1
- [x] Multi-stage Dockerfile, no compiler in final image
- [x] HEALTHCHECK on every long-running service
- [x] Per-container resource limits + log rotation
- [x] Internal network — only nginx is public
- [x] Pydantic-validated config from env / `.env` / Docker secrets
- [x] Structured JSON logs with request-id correlation
- [x] Liveness `/healthz`, readiness `/readyz`, version `/version`, Prometheus `/metrics`
- [x] Crash recovery on boot (stale runs, HITL queue, balance loops)
- [x] Reverse proxy with `X-Forwarded-*` and trusted-proxy whitelist
- [x] Terraform for the cloud footprint (VPC, RDS, Redis, ECS, ALB, IAM, SSM)
- [x] Kubernetes manifests with HPA, PDB, NetworkPolicies, PSA `restricted`
- [x] GitHub Actions CI (lint, test, eval, docker) + release pipeline via OIDC
- [x] Prometheus + Grafana baseline (dashboard + alerts)
- [x] Reproducible eval harness with portfolio AND process metrics
- [x] Runbooks for operations and incidents; ADRs for design choices

Next three (per the brief's "next three things"):

1. **Leader election** in the worker — enables `replicas: >1`.
2. **pgvector** migration — retire SQLite for the vector store.
3. **Cost-aware model routing** — by `purpose` in `app/llm.py`.

## Task brief

The original assignment lives at the repo root:
`Agentic_AI_Engineer_Home_Task__284_29.docx`. The architecture diagrams
at the repo root (`architecture_simple.*`, `architecture.*`,
`architecture_deployment.*`) cover the brief's "logical view" and
"deployment view" requirements respectively. The plain-English explainer
is in `docs/how-it-works-simple.md`.

## Deliverables map (spec → file)

| Spec item | Lives at |
|-----------|----------|
| A. Runnable repo, tests, eval harness, Dockerfile, sub-10-min README | This repo · `Makefile` · `make up` |
| B. Architecture diagram (logical + deployment) | `architecture.png` · `architecture_deployment.png` · `architecture_simple.png` |
| C. README, technical overview, runbook, eval report | `README.md` · `docs/technical-overview.md` · `docs/runbooks/*` · `docs/eval-results.md` |
| D. Sample run with reports + traces | `artifacts/reports/2026-05-18/daily.{json,xlsx}` (sample in repo) · `evals/output/` · live data on Docker volume / `ops.sqlite` |
| 6. Output channels (≥2) | **3:** Web UI (live dashboard), Excel daily report, JSON log stream — justified in `docs/technical-overview.md` |
# horizon-capital
