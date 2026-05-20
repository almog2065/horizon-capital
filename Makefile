#  Horizon Capital — convenience targets.
#  Self-documenting: `make help`.

SHELL := /bin/bash
PYTHON ?= python3
COMPOSE := docker compose
COMPOSE_OBS := docker compose -f docker-compose.yml -f docker-compose.observability.yml
TF := terraform
KUSTOMIZE := kubectl

.DEFAULT_GOAL := help

.PHONY: help build up down restart logs ps shell test smoke clean migrate \
        web-logs worker-logs nginx-logs db-shell redis-shell pull \
        obs-up obs-down eval eval-strict tf-init tf-plan tf-apply tf-destroy \
        k8s-check k8s-build-image k8s-load-image k8s-dev k8s-dev-local k8s-prod \
        k8s-status k8s-status-dev k8s-status-prod lint demo report

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n\nTargets:\n"} \
		/^[a-zA-Z_-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ----- core stack -----
build: ## Build all images.
	$(COMPOSE) build

pull: ## Pull base images (postgres, redis, nginx).
	$(COMPOSE) pull postgres redis nginx

up: ## Start the full stack in detached mode.
	$(COMPOSE) up -d

down: ## Stop the stack (volumes persist).
	$(COMPOSE) down

restart: ## Restart web + worker only.
	$(COMPOSE) restart web worker

logs: ## Tail logs from all services.
	$(COMPOSE) logs -f --tail=200

web-logs: ## Tail web logs.
	$(COMPOSE) logs -f --tail=200 web

worker-logs: ## Tail worker logs.
	$(COMPOSE) logs -f --tail=200 worker

nginx-logs: ## Tail nginx logs.
	$(COMPOSE) logs -f --tail=200 nginx

ps: ## List running services.
	$(COMPOSE) ps

shell: ## Open a bash shell in the web container.
	$(COMPOSE) exec web /bin/bash

db-shell: ## Postgres psql shell.
	$(COMPOSE) exec postgres psql -U horizon -d horizon

redis-shell: ## Redis CLI shell.
	$(COMPOSE) exec redis redis-cli

migrate: ## Run the one-shot migration job.
	$(COMPOSE) --profile migrate run --rm migrate

smoke: ## Run the smoke test against a running stack.
	$(COMPOSE) exec web python smoke_test.py

test: ## Run pytest inside the web container.
	$(COMPOSE) exec web pytest -q tests/

lint: ## Run ruff against the source tree.
	ruff check app tests evals

clean: ## Stop stack and remove volumes (destructive).
	$(COMPOSE) down -v
	rm -rf artifacts/* || true

# ----- observability (Prometheus + Grafana) -----
obs-up: ## Bring up app + observability stack.
	$(COMPOSE_OBS) up -d

obs-down: ## Tear down app + observability stack.
	$(COMPOSE_OBS) down

# ----- eval harness -----
eval: ## Run eval harness against the sample window.
	$(PYTHON) -m evals.run --window sample --out evals/output/run.json

eval-strict: ## Run eval with CI thresholds (non-zero on regression).
	$(PYTHON) -m evals.run --window sample \
		--fail-on grounded_ratio:0.75 \
		--fail-on decision_quality:0.70 \
		--fail-on guardrail_breaches:0

# ----- live demo -----
demo: ## End-to-end demo runner (assumes stack is up; brings it up if not).
	./scripts/demo.sh

report: ## Render today's Excel report (in the web container).
	$(COMPOSE) exec web python -m app.reports

report-demo: ## Render an Excel report with synthetic data (no firm state needed).
	$(COMPOSE) exec web python -m app.reports --demo

# ----- Terraform (AWS) -----
tf-init: ## terraform init (run once)
	cd infra/terraform && $(TF) init

tf-plan: ## terraform plan for dev (override ENV=prod)
	cd infra/terraform && $(TF) workspace select $${ENV:-dev} || $(TF) -chdir=. workspace new $${ENV:-dev}
	cd infra/terraform && $(TF) plan -var-file=envs/$${ENV:-dev}.tfvars

tf-apply: ## terraform apply for dev (override ENV=prod)
	cd infra/terraform && $(TF) workspace select $${ENV:-dev}
	cd infra/terraform && $(TF) apply -var-file=envs/$${ENV:-dev}.tfvars

tf-destroy: ## terraform destroy for dev (override ENV=prod)
	cd infra/terraform && $(TF) workspace select $${ENV:-dev}
	cd infra/terraform && $(TF) destroy -var-file=envs/$${ENV:-dev}.tfvars

# ----- Kubernetes (Kustomize) -----
k8s-check: ## Verify kubectl can reach a cluster (run before k8s-dev).
	@ctx="$$(kubectl config current-context 2>/dev/null)"; \
	if [ -z "$$ctx" ]; then \
	  echo "No kubectl context. Enable Kubernetes in Docker Desktop (Settings → Kubernetes),"; \
	  echo "or install kind: brew install kind && kind create cluster --name horizon"; \
	  exit 1; \
	fi; \
	echo "context=$$ctx"; \
	kubectl cluster-info >/dev/null || (echo "Cluster unreachable. Is the API server running?"; exit 1)

KIND_CLUSTER ?= horizon
K8S_IMAGE ?= horizon-capital:latest

k8s-build-image: ## Build app image (same as compose).
	$(COMPOSE) build web

k8s-load-image: k8s-build-image ## Load horizon-capital:latest into kind (required before pods start).
	kind load docker-image $(K8S_IMAGE) --name $(KIND_CLUSTER)

k8s-dev: k8s-check ## Apply k8s dev overlay.
	$(KUSTOMIZE) apply -k infra/k8s/overlays/dev

k8s-dev-local: k8s-load-image k8s-dev ## Build, load image into kind, apply dev manifests.
	@echo "Restarting pods so they pick up the newly loaded image (tag unchanged)…"
	$(KUSTOMIZE) -n $(K8S_NS_DEV) rollout restart deployment/web deployment/worker
	$(KUSTOMIZE) -n $(K8S_NS_DEV) rollout status deployment/web --timeout=180s

k8s-prod: k8s-check ## Apply k8s prod overlay.
	$(KUSTOMIZE) apply -k infra/k8s/overlays/prod

K8S_NS_DEV ?= horizon-capital-dev
K8S_NS_PROD ?= horizon-capital

k8s-status: ## Pods/svcs in dev namespace (NS=horizon-capital for prod resources).
	kubectl -n $${NS:-$(K8S_NS_DEV)} get pods,svc,ingress,hpa,pdb

k8s-status-dev: ## Status for k8s-dev overlay (horizon-capital-dev).
	kubectl -n $(K8S_NS_DEV) get pods,svc,ingress,hpa,pdb

k8s-status-prod: ## Status for k8s-prod overlay (horizon-capital).
	kubectl -n $(K8S_NS_PROD) get pods,svc,ingress,hpa,pdb
