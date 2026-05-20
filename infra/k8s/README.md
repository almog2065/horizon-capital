# Horizon Capital — Kubernetes Manifests (Kustomize)

Base + dev/prod overlays for the firm.

## Layout

```
infra/k8s/
├── base/                   # cluster-agnostic manifests
│   ├── namespace.yaml      # PSA: restricted enforce + audit
│   ├── configmap.yaml      # APP_ENV, LOG_LEVEL, scheduler knobs
│   ├── secret.yaml         # placeholder — wire ExternalSecrets in real clusters
│   ├── postgres.yaml       # in-cluster Postgres (dev) — swap for RDS in prod
│   ├── redis.yaml          # in-cluster Redis
│   ├── web-deployment.yaml # FastAPI tier, readiness/liveness/startup probes
│   ├── web-service.yaml    # ClusterIP
│   ├── worker-deployment.yaml  # single replica (no leader election yet)
│   ├── ingress.yaml        # nginx-ingress
│   ├── hpa.yaml            # 2-8 replicas on CPU/mem
│   ├── pdb.yaml            # PodDisruptionBudget — keep ≥1 web pod up
│   └── networkpolicy.yaml  # default-deny + targeted allow
└── overlays/
    ├── dev/                # 1 web replica, DEBUG logs, text format
    └── prod/               # 3 web replicas, larger resources
```

## Prerequisites

`kubectl apply` needs a **running cluster** and a **current context**:

```bash
kubectl config current-context   # must not be empty
kubectl cluster-info             # must succeed
make k8s-check                   # Makefile preflight
```

**macOS (Docker Desktop):** Settings → Kubernetes → Enable Kubernetes → Apply & Restart.
Context is usually `docker-desktop`.

**kind (alternative):**

```bash
brew install kind
kind create cluster --name horizon
kubectl config use-context kind-horizon
```

If you see `failed to download openapi: the server could not find the requested
resource`, there is no API server — fix the cluster first (not a manifest bug).

## Local kind workflow

Kind does **not** see images on your Mac until you load them. Dev overlay uses
`horizon-capital:latest` and `imagePullPolicy: Never` (no Docker Hub pull).

```bash
kind create cluster --name horizon
kubectl config use-context kind-horizon
make k8s-dev-local          # build + kind load + apply + wait for web
make k8s-status-dev
```

Step by step:

```bash
make k8s-build-image
kind load docker-image horizon-capital:latest --name horizon
make k8s-dev
kubectl -n horizon-capital-dev rollout restart deployment/web deployment/worker
```

`ImagePullBackOff` on `horizon-capital:dev` means an old overlay — re-apply after
pulling latest manifests (tag is `latest`).

## Apply (prod / registry)

```bash
make k8s-prod
# or: kubectl apply -k infra/k8s/overlays/prod
make k8s-status-prod
```

## Probes

| Probe        | Path        | Used by           |
|--------------|-------------|-------------------|
| `readinessProbe` | `/readyz`   | LB / Service rotation |
| `livenessProbe`  | `/healthz`  | Restart policy        |
| `startupProbe`   | `/healthz`  | Grace period for boot |

## Worker scaling

Worker is intentionally `replicas: 1` with `strategy: Recreate`. The
scheduler loops are not yet leader-elected. Before scaling, add either:

* Kubernetes Lease + custom leader election (sidecar or in-process)
* External scheduler (e.g., AWS EventBridge → ECS one-shot task)

## Secrets

The `horizon-secrets` placeholder is meant to be replaced by either:

* **External Secrets Operator** sourcing from AWS Secrets Manager / SSM
* **SealedSecrets** for GitOps-friendly storage in-repo
* **Vault Agent Injector** sidecar

Do **not** commit real values.
