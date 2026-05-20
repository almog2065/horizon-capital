# Runbook — Firm Operations

Day-to-day operational playbook for the Horizon Capital firm.

## TL;DR

| Need                       | Command                                                  |
|----------------------------|----------------------------------------------------------|
| Bring stack up             | `make up`                                                |
| Stop stack                 | `make down`                                              |
| Tail web logs              | `make web-logs`                                          |
| Tail worker logs           | `make worker-logs`                                       |
| Check readiness            | `curl http://127.0.0.1:8080/readyz`                      |
| Run eval                   | `python -m evals.run --window sample`                    |
| Open psql                  | `make db-shell`                                          |
| Force scheduler tick       | `make shell` → `python -c "from app.scheduler import ..."` |

## 1. Start / stop

```bash
# fresh boot
cp .env.example .env
make build
make up
make migrate                # one-shot bootstrap (idempotent)

# verify
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/readyz | jq .
```

If `web` or `worker` is unhealthy, see [Recovery](#3-recovery).

## 2. Operating cadence

The worker container runs two background loops:

| Loop                       | Default interval | Toggle env                  |
|----------------------------|------------------|-----------------------------|
| `plan_supervision_loop`    | 30 min           | `AUTO_PLAN_SUPERVISION`     |
| `firm_balance_loop`        | disabled (0)     | `FIRM_BALANCE_INTERVAL_SEC` |

Both are restartable: kill the worker container, the next start picks
up from persisted state (`firm.sqlite` or Postgres).

## 3. Recovery

### 3a. Web pod stuck unhealthy
```bash
make web-logs                            # diagnose root cause
docker compose restart web               # quickest fix
curl -fsS http://127.0.0.1:8080/readyz   # confirm healthy
```

### 3b. Stale runs after crash
On boot, `lifecycle._bootstrap` runs:
* `db.recover_stale_running_runs(max_age_sec=900)`
* `firm_orchestration.recover_stale_balance_runs()`
* `hitl_sync.repair_hitl_queue()`

If you suspect leftover state, restart `web` — recovery is idempotent.

### 3c. DB connection failures
```bash
make db-shell
\l                            # list dbs
\dn                           # schemas
SELECT pg_is_in_recovery();   # replica?
```

If RDS endpoint changed, re-apply terraform and bounce ECS services
(see [ADR 0001](../adr/0001-multi-container-deployment.md) and
[ADR 0002](../adr/0002-state-and-iac.md)).

## 4. HITL operator workflow

1. UI surfaces queue at `/` (top of the home page).
2. Operator clicks **Approve** or **Reject** on each item.
3. On Approve, the original execution pipeline resumes from the saved
   LangGraph checkpoint.
4. On Reject, the plan is marked `rejected`; no trade is placed.

`HITL_ONE_PER_TICKER=1` prevents duplicate items for the same ticker.

## 5. Token-cost guard

The eval harness reports LLM call counts (see `process.n_llm_calls` in
the report). To keep token spend predictable in prod:

* Default `OPENAI_MODEL=gpt-4o-mini` (cheap)
* The brief calls out "cost-aware model routing" as a bonus — the place
  to do this is `app/llm.py::chat_json(..., model=...)`. Pick model by
  `purpose` parameter.

## 6. Failure modes & circuit breakers

| Component  | Failure                         | Behavior                                 |
|------------|---------------------------------|------------------------------------------|
| OpenAI     | API error / no key              | Falls back to deterministic mock         |
| yfinance   | rate-limit / network            | Falls back to seeded sample data         |
| LangSmith  | network error                   | Local trace still recorded               |
| Postgres   | down                            | `/readyz` returns 503, ALB rotates out   |
| Redis      | down                            | `check_redis` returns `fail` in /readyz  |

## 7. Rollback

ECS task definitions are immutable; rollback means pointing the service
back at the previous revision:

```bash
aws ecs update-service \
  --cluster horizon-prod-cluster \
  --service horizon-prod-web \
  --task-definition horizon-prod-web:<PREV_REV> \
  --force-new-deployment
```

The deployment circuit breaker (`deployment_circuit_breaker.rollback =
true` in terraform) does this automatically on health-check failures.

## 8. Useful URLs (local)

| URL                              | What                          |
|----------------------------------|-------------------------------|
| http://127.0.0.1:8080            | Firm UI (via nginx)           |
| http://127.0.0.1:8080/healthz    | Liveness (terminated at nginx)|
| http://127.0.0.1:8080/readyz     | Readiness (deep check)        |
| http://127.0.0.1:8080/metrics    | Prometheus text               |
| http://127.0.0.1:9090            | Prometheus (with obs stack)   |
| http://127.0.0.1:3000            | Grafana (admin / admin)       |
