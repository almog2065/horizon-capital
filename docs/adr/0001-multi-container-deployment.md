# ADR 0001 — Multi-container deployment over single process

* Status: Accepted
* Date: 2026-05-19
* Deciders: platform / engineering

## Context

The original `horizon_capital` is a single FastAPI process: API,
background schedulers, RAG bootstrap, HITL queue, and trade replay all
live in the same Python interpreter. That works for a demo but couples
scaling and failure modes.

The home task explicitly calls out "production readiness, high
availability and standardization" and "scalability".

## Decision

Split the firm into three deployable units that share an image:

1. **`web`** — FastAPI / Uvicorn. Stateless. Horizontally scalable.
2. **`worker`** — Background loops (plan supervision, balance loops).
   Single replica until leader election is added.
3. **`migrate`** — One-shot bootstrap (init_db, RAG seed). Runs via a
   compose profile or a K8s Job.

Backing services run as their own containers / managed services:

* Postgres (RDS in cloud, `postgres:16-alpine` locally)
* Redis (ElastiCache in cloud, `redis:7-alpine` locally)
* Nginx ingress (ALB in cloud)

## Consequences

### Positive
* Web pods can be replaced or scaled independently of schedulers.
* A crashing scheduler doesn't take down the API.
* Each unit has its own resource limits, log stream, and healthcheck.
* The same image is used everywhere — fewer surprises.

### Negative
* Background loops are not yet leader-elected. **Worker stays at
  `replicas: 1`** until that's fixed. (Trade-off accepted for now.)
* Operators must learn two containers instead of one.

## Alternatives considered

* **Sidecar scheduler in the same pod** — keeps a single deployable but
  forces the API to be co-tenanted with the scheduler. Rejected.
* **Cron/EventBridge external scheduler** — solid for pure ticks but
  complicates HITL resumption (LangGraph checkpoint state). Punted for
  later.

## Migration path

`RUN_SCHEDULER_IN_API=true` lets you collapse back to single-process
in dev. Setting it `false` (default in compose / K8s) requires the
worker container.
