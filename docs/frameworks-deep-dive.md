# Frameworks deep-dive

Every framework / runtime in the stack, why it's here, what it does
for the firm, and where to look in the code. Read this before drilling
in on any one piece.

---

## Python 3.12 (runtime)

Picked for:
* Native `asyncio` improvements (TaskGroup, cleaner exception
  groups) — matters when the worker fans out scheduler tasks.
* Pattern matching for graph node dispatch.
* `Pathlib`-everywhere ergonomics.

Pinned in the Dockerfile (`ARG PYTHON_VERSION=3.12`) and in CI
(`python-version: "3.12"`). Pyproject declares `requires-python =
">=3.11"` so the eval harness can run on slightly older interpreters
when needed.

---

## FastAPI 0.110+ (HTTP layer)

### Where it lives
* App composition: `app/api/app_factory.py`
* Legacy UI routes: `app/main.py`
* Ops routes (`/healthz`, `/readyz`, `/version`, `/metrics`):
  `app/api/routes/health.py`
* Daily report routes: `app/api/routes/reports.py`
* Templates: `app/templates/*.html` (Jinja2)

### Why FastAPI
* **Lifespan context** — one place to wire bootstrap + shutdown.
  We use it for DB init, RAG seed, HITL queue repair, and background
  tasks (`app/core/lifecycle.py`).
* **Pydantic v2 first class** — same library powering settings and
  agent I/O.
* **Async support** when we move toward async LLM calls.
* **OpenAPI auto-gen** for the ops surface, free.
* **Starlette under the hood** — small surface area, middleware is
  easy to compose.

### Middleware chain (request order)

```
client → RequestIdMiddleware (sets x-request-id)
       → TrustedHostMiddleware (if ALLOWED_HOSTS != "*")
       → CORSMiddleware (if CORS_ORIGINS set)
       → router → handler
       → response → headers["x-request-id"]
```

### App factory pattern
We deliberately do **not** put `app = FastAPI(...)` at module top
level inside the legacy `main.py` as the only composition point.
Instead, `app/api/app_factory.py::create_app()` is the single root.
The legacy app object is *reused* (so the rich UI keeps working) but
its lifespan and middleware are replaced.

---

## Uvicorn (ASGI server)

Run command (compose):
```bash
uvicorn app.api.app_factory:app \
  --host 0.0.0.0 --port 8000 \
  --workers 2 --proxy-headers --forwarded-allow-ips '*'
```

`--proxy-headers` honors `X-Forwarded-*` from nginx so request logs
have the real client IP. `--forwarded-allow-ips '*'` trusts the proxy
in-cluster (only nginx can reach the web pod).

For dev: `--reload` reads code changes; not enabled in compose
because we want the same image across envs.

---

## LangGraph (orchestration)

### Where
* Graph definition: `app/graph.py` (~1.3k lines)
* State schema: `app/firm_state.py`
* Checkpointer: SQLite via `langgraph-checkpoint-sqlite`
* Checkpoint store: `artifacts/checkpoints.sqlite`

### Why
The brief calls for "be explicit about how state flows between agents
and how the firm behaves under partial failure." Hand-rolling that
state machine means:
* Serializing graph state to a durable store every transition.
* Resuming from a node after a process crash.
* Routing HITL pauses without losing context.

LangGraph gives all three out of the box.

### Mental model
* **State** is one dict (`FirmState`). Each node reads it and writes
  back a delta.
* **Nodes** are agent functions. Each one is a contract: typed input
  fields, typed output fields.
* **Edges** are conditional functions on the state — `if risk_required_hitl
  then HITL_NODE else EXECUTE_NODE`.
* **Checkpoints** save the state at every transition, keyed by `thread_id`.

### HITL pattern
```python
# When risk_officer returns hitl_required=True:
hitl_enqueue(state)            # row in firm.sqlite
graph.checkpointer.put(state)  # snapshot in checkpoints.sqlite
return interrupt()             # LangGraph pauses

# Later, when operator clicks Approve:
state = graph.checkpointer.get(thread_id)
state["hitl_approved"] = True
graph.resume(thread_id, state)
```

The brief explicitly mentions "Graph state persists across the wait."
This is how.

---

## Pydantic + pydantic-settings (validation & config)

### Where
* `app/core/settings.py` — the firm's only settings reader
* `app/firm_state.py` — agent state schema
* Per-agent contracts use Pydantic models for input/output validation

### Why
* **One source of truth for config** — Pydantic Settings reads env,
  .env, Docker secrets, defaults — in priority order.
* **Startup-time validation** — bad config fails readiness, not a
  midnight customer request.
* **Same model on the wire and in storage** — the agent state we
  validate is the same shape we persist to LangGraph checkpoints.

### Settings precedence
1. `os.environ`
2. `.env` (location via `ENV_FILE`)
3. `/run/secrets/<key>` (Docker secrets)
4. Defaults in `Settings` class

### Docker secrets bridge
```python
@field_validator("OPENAI_API_KEY", mode="before")
@classmethod
def _maybe_docker_secret_openai(cls, v):
    if not v:
        return _read_docker_secret("openai_api_key") or ""
    return v
```

This is why production deployments mount the secret at
`/run/secrets/openai_api_key` and *don't* need to set the env var.

---

## OpenAI Python SDK (LLM access)

### Where
* `app/llm.py::chat_json` — the only place we call OpenAI.

### Why a wrapper
* **Structured JSON** — `response_format={"type": "json_object"}` is
  set every call; downstream agents assume JSON.
* **Mock fallback** — when no API key OR `USE_MOCK_LLM=1`, returns a
  deterministic dict that matches the expected schema. This is what
  makes the eval harness reproducible.
* **Trace recording** — every call writes a row to `traces.sqlite`
  with `purpose`, `model`, `mode` (live/mock/live_error),
  `system`/`user` prompts, `response`, `tokens`, `duration_ms`,
  `citations` — and optionally pushes to LangSmith.

### Cost knobs
* `OPENAI_MODEL=gpt-4o-mini` is the default. ~10x cheaper than `gpt-4o`.
* Per-agent prompts are kept short; retrieved chunks are truncated.
* `temperature=0.2` — deterministic-ish without losing all variety.

### Future: cost-aware routing
The wrapper accepts a `purpose` parameter. The cleanest extension is
a routing function `pick_model(purpose) -> str` that gets called at
the top of `chat_json`. Listed in the "next three things".

---

## LangSmith (trace forwarding)

* When `LANGSMITH_API_KEY` is set, every `chat_json` call also
  emits a span to LangSmith.
* Local SQLite is still written — LangSmith is the **viewer**, not the
  store of record.
* When the key is missing, we silently skip the LangSmith side. No
  errors, no exceptions, no test breakage in CI.

→ `app/traces.py::configure_langsmith`.

---

## SQLite + Postgres (persistence)

### Today (default)
Three SQLite files under `artifacts/`:
* `firm.sqlite` — runs, holdings, trades, HITL queue.
* `vectors.sqlite` — embeddings for RAG corpora.
* `checkpoints.sqlite` — LangGraph graph state.

Why: `make up` works on a fresh clone with zero DB setup. The brief
asks for "clone to running demo in under 10 minutes."

### Tomorrow (Postgres-ready)
* `DATABASE_URL` is set in compose/K8s/Terraform.
* `app/db.py` is currently SQLite-only. The cutover is a small
  refactor — same SQL, swap drivers. Already provisioned RDS in
  Terraform.
* ADR 0002 lists this as a planned migration.

---

## Redis (cache + future broker)

Today: idle but healthchecked. Why ship it?
* The day we add the second worker replica, we need a distributed
  lock (Redis `SET NX EX`).
* Hot RAG retrievals (same ticker, same window) are a 50% win on
  token cost.
* A future event bus (Redis Streams) is one configuration away.

→ `app/core/health.py::check_redis`.

---

## Nginx (reverse proxy)

### Where
* Container: `nginx:1.27-alpine`
* Config: `docker/nginx/nginx.conf` + `docker/nginx/conf.d/horizon.conf`

### What it does
* Terminates HTTP at port 80 (TLS at 443 when wired).
* `gzip` for text content.
* Forwards `X-Forwarded-*` headers (uvicorn honors them).
* **Restricts `/metrics`** by source IP — internal RFC1918 only.
* **Static `/healthz`** short-circuit at nginx (no app hop).

### Production hardening
* Add a TLS listener block via cert-manager / Certbot.
* Rate-limit by IP at the proxy.
* Add a WAF in front (AWS WAFv2 baseline is in Terraform comments).

---

## Docker / Docker Compose

### Dockerfile (multi-stage)
* **builder** stage builds wheels with a full toolchain.
* **runtime** stage installs from wheels; no compiler in the final
  image.
* Non-root user `horizon` (uid 1000).
* `tini` as PID 1 (signal forwarding + zombie reaping).
* `HEALTHCHECK` that curls `/healthz`.

### Compose
Two files:
* `docker-compose.yml` — web, worker, postgres, redis, nginx, migrate.
* `docker-compose.observability.yml` — adds prometheus, grafana.

Layered with: `docker compose -f a -f b up`.

### Resource limits
Set per service via the v3 `deploy.resources` block. Compose only
honors these in swarm mode by default; production-scale settings live
in K8s manifests instead. We keep them in compose as documentation.

---

## Kubernetes (Kustomize)

### Layout
```
infra/k8s/
├── base/                          # cluster-agnostic
│   ├── namespace.yaml             # PSA restricted
│   ├── configmap.yaml             # APP_ENV, LOG_LEVEL, ...
│   ├── secret.yaml                # placeholder, replaced by ExternalSecrets
│   ├── postgres.yaml              # StatefulSet (dev only — RDS in prod)
│   ├── redis.yaml
│   ├── web-deployment.yaml        # replicas: 2, full probe set
│   ├── web-service.yaml
│   ├── worker-deployment.yaml     # replicas: 1, strategy: Recreate
│   ├── ingress.yaml               # nginx-ingress
│   ├── hpa.yaml                   # 2–8 web replicas on CPU+memory
│   ├── pdb.yaml                   # minAvailable: 1 web
│   └── networkpolicy.yaml         # default-deny + targeted allow
└── overlays/
    ├── dev/    # 1 replica, DEBUG, text logs
    └── prod/   # 3 replicas, larger resources
```

### Probe strategy
Three probes per container:
* `startupProbe` — `/healthz`, generous initial period.
* `readinessProbe` — `/readyz`, rotated out of service on fail.
* `livenessProbe` — `/healthz`, restarts pod on fail.

The `/readyz` endpoint does deep checks (DB + Redis); `/healthz` is
cheap. This is the standard 3-probe pattern.

### NetworkPolicies
Default-deny ingress on everything. Targeted allows:
* `ingress-nginx → web`
* `web + worker → postgres`
* `web + worker → redis`

No pod can ping any other pod unless it's explicitly allowed.

---

## Terraform (AWS IaC)

### Layout
```
infra/terraform/
├── main.tf                # composition root — VPC/RDS/Redis/ECR/ECS/ALB/IAM/SSM
├── envs/
│   ├── dev.tfvars
│   └── prod.tfvars
└── README.md
```

### Workspaces
One workspace per environment (`dev`, `prod`). Variables file per
env. State backend (commented in `main.tf`) is S3 + DynamoDB lock.

### Why Terraform over Pulumi/CDK
* HCL is portable across cloud providers and across teams.
* State model is mature (S3 + DynamoDB lock is the standard pattern).
* Smaller surface area than CDK — fewer ways to mis-loop.

### Resources highlights
* `aws_vpc.main` with public + private subnets per AZ.
* `aws_db_instance.postgres` with multi-AZ in prod.
* `aws_elasticache_cluster.redis` (cluster mode toggleable).
* `aws_ecr_repository.app` with **`image_tag_mutability = IMMUTABLE`**
  and `scan_on_push = true`.
* `aws_ecs_cluster.main` with container insights enabled.
* `aws_ecs_task_definition.web` and `.worker` reusing the same image.
* `aws_ecs_service.web` with `deployment_circuit_breaker.rollback`.
* `aws_lb.web` (ALB) + `aws_lb_target_group.web` health-checked at
  `/healthz`.
* `aws_ssm_parameter.{db_password,openai_key}` (SecureString).

### Outputs
`alb_dns`, `ecr_repository_url`, `rds_endpoint` (sensitive),
`redis_endpoint`, `cluster_name`.

---

## GitHub Actions (CI/CD)

### Three workflows
* `ci.yml` — push & PR: lint → pytest → eval → docker build.
* `release.yml` — tag `v*.*.*` or dispatch: build & push to ECR via
  OIDC, then `ecs update-service --force-new-deployment`.
* `terraform.yml` — PRs touching infra/: `terraform fmt` check,
  `init`, `plan`. Manual dispatch for `apply`.

### OIDC into AWS
No long-lived AWS access keys in repo secrets. The GitHub Actions
job assumes `arn:aws:iam::ACCOUNT:role/horizon-capital-ci` via OIDC.
The trust policy on that role is the standard GitHub OIDC pattern.

### Caching
* `actions/setup-python@v5` with `cache: pip`.
* `docker/build-push-action@v6` with `cache-from/to: type=gha`.

---

## Prometheus + Grafana (observability)

### Prometheus
* Scrapes `web:8000/metrics` every 15s.
* Rules file ships two baseline alerts (`HorizonWebDown`,
  `HITLBacklogGrowing`).
* No persistent retention story for the demo — `prometheus-data`
  volume is local; in prod, push to Thanos/Mimir.

### Grafana
* Pre-provisioned `Prometheus` datasource.
* Pre-provisioned dashboard `Horizon Capital — Overview` (uid
  `horizon-overview`).
* Anonymous access disabled; default admin/admin (rotate in prod).

### Metrics exposed today
* `horizon_runs_total` (counter).
* `horizon_hitl_pending` (gauge).

The `/metrics` endpoint is hand-rolled (no `prometheus_client`
dependency yet) — adding more metrics is one line in
`app/api/routes/health.py`.

---

## Cross-cutting: the logging contract

JSON Lines to stdout. One event per line. Fields:

| Field      | Always present? | Meaning                                |
|------------|-----------------|----------------------------------------|
| `ts`       | ✓               | ISO-8601 UTC with milliseconds         |
| `level`    | ✓               | INFO / WARNING / ERROR / DEBUG         |
| `logger`   | ✓               | `horizon.<component>`                  |
| `msg`      | ✓               | human-readable summary                 |
| `event`    | conditional     | machine key (e.g. `fill`, `hitl_required`) |
| `trace_id` | conditional     | correlation across requests            |
| `run_id`   | conditional     | tie to a firm run                      |
| `ticker`   | conditional     | what we were trading                   |
| `agent`    | conditional     | which agent emitted                    |
| `exc`      | on exception    | stack trace                            |

Anything that consumes JSON lines — Loki, CloudWatch Logs Insights,
Slack via a webhook bridge — can ingest unchanged.

---

## Market-data providers (asset-class routed)

The firm trades across four asset classes. Each routes to its own
provider through `app/market_providers.py`:

| Asset class      | Provider               | Key needed | Notes                                  |
|------------------|------------------------|------------|----------------------------------------|
| Equity           | yfinance + SEC EDGAR   | none       | Existing path                          |
| Crypto           | CoinGecko (public API) | none       | Toggled by `ENABLE_COINGECKO`          |
| Commodity proxy  | yfinance (ETFs)        | none       | GLD / USO / etc. — `data_provider="yfinance_etf"` |
| Rates / FX proxy | Frankfurter (FX) + ETFs| none       | Toggled by `ENABLE_FX_CONTEXT`         |

All four providers are **keyless** by design — we wanted the demo to
clone-and-run without credential setup. Each provider has a
deterministic mock fallback (see `data/dossiers/*.json`).

### Asset-class routing
`app/asset_universe.py` is the registry. Each `AssetMeta` dataclass
carries `asset_class`, `data_provider`, plus the per-provider keys
(`coingecko_id`, `yahoo_symbol`, `underlying`). The market_data
module branches on `data_provider` to pick the right fetcher.

The agents themselves stay asset-class-agnostic — they read
`meta.is_crypto` / `meta.is_commodity_proxy` if they care, but mostly
they just consume the normalized fundamentals dict.

### Cost / rate-limit guardrails
* `SCAN_MAX_EVALUATE` caps mechanical-screen names per pass (default 24).
* `SCAN_API_DISCOVERY_COUNT` caps EDGAR pulls per scan (default 20).
* `FIRM_MANAGER_SCAN_COOLDOWN_SEC` (1h) gates the scheduler.
* CoinGecko public limit is ~50 req/min — comfortably above what we
  generate at default throttles.

---

## Anti-frameworks (things we deliberately don't use)

| Skipped         | Why                                                         |
|-----------------|-------------------------------------------------------------|
| Celery / RQ     | One periodic loop, both idempotent — not enough complexity. |
| ORM (SQLAlchemy)| `db.py` is ~100 lines of `sqlite3` against 6 tables.       |
| Streamlit       | We need real request handling.                              |
| openpyxl        | The Excel report is stdlib-only; smaller image.             |
| prometheus_client | `/metrics` is text — adding a client lib for 2 metrics is overkill |
| GraphQL         | The UI is rendered server-side; no client app yet.          |
| gRPC            | All cross-container traffic is HTTP today.                  |

Each of these is a defensible choice. The day they're not, swap them
in — the seams are there.
