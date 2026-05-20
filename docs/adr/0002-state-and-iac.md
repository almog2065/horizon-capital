# ADR 0002 — State management & IaC

* Status: Accepted
* Date: 2026-05-19

## Context

The firm holds positions, cost basis, P&L history, agent traces, HITL
state, and RAG corpora. The brief: "must survive restart and reconcile
cleanly after a crash."

## Decision

* **Local dev**: SQLite files under `./artifacts/`, mounted as a named
  volume in compose. Fast, zero-config.
* **Cloud**: Postgres via RDS (Terraform), reachable as `DATABASE_URL`.
  The legacy `app/db.py` keeps SQLite primary; cutover is staged so we
  can carry both backends during migration.
* **IaC tool**: Terraform 1.6+, AWS provider 5.x. State backend = S3 +
  DynamoDB lock (template included, commented; populate before init).
* **Per-env workspaces**: `dev` and `prod`. Variable files under
  `infra/terraform/envs/`. Per-env image tag, instance class,
  multi-AZ.
* **Kubernetes**: Kustomize base + overlays mirror the same dev/prod
  split. Same image, different config map / replica count.

## Consequences

### Positive
* One source of truth per environment — no console-clickops drift.
* Cheap dev (single-AZ RDS, t4g.micro, 1 replica), strict prod
  (multi-AZ, 3 replicas).
* Switching cloud accounts is a `terraform workspace` away.

### Negative
* Two storage backends to test (sqlite and Postgres). Acceptable while
  the project is in transition; a future ADR will retire SQLite.
* RDS endpoint sits in private subnets — bastion/SSM session is
  required for direct psql.

## Alternatives considered

* **CDK** — full programmability but locks the project to TS or
  Python-specific decorators. Terraform's HCL is more portable across
  teams.
* **Pulumi** — same trade-off as CDK plus a paid SaaS for state by
  default.
