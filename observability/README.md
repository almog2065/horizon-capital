# Observability

Prometheus + Grafana on top of the main Compose stack, plus rich in-app
traces and cost/eval metrics.

## Run

```bash
make obs-up
# or:
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

| URL | Purpose |
|-----|---------|
| http://127.0.0.1:9090 | Prometheus |
| http://127.0.0.1:3000 | Grafana (admin / admin) |
| http://127.0.0.1:8080/metrics | Prometheus text (scrape target) |
| http://127.0.0.1:8080/api/observability/summary | JSON ops snapshot |
| http://127.0.0.1:8080/traces | Trace explorer (cost + tokens) |
| http://127.0.0.1:8080/diagnostics | LLM cost breakdown + last eval |

## Metrics (`horizon_*`)

Exposed on `/metrics` via `app/metrics_registry.py`:

| Metric | Meaning |
|--------|---------|
| `horizon_llm_calls_total{purpose,model,mode}` | LLM calls since process start |
| `horizon_llm_cost_usd_total{purpose,model}` | Estimated spend (USD) |
| `horizon_llm_*_tokens_total` | Prompt / completion tokens |
| `horizon_llm_cost_usd_today` | Trace DB sum for today (ET) |
| `horizon_hitl_pending` | HITL queue depth |
| `horizon_runs_by_status` / `_by_trigger` | Run breakdown |
| `horizon_cadence_jobs_done_today` | Wall-clock cadence progress |
| `horizon_market_session_open` | 1 during 09:30–16:00 ET |
| `horizon_eval_*` | Last `evals/output/*.json` snapshot |

## Traces & cost

Every `llm_call` trace stores `tokens` and `estimated_cost_usd`
(`app/model_routing.py` pricing table). Aggregates power Grafana and
the Diagnostics page.

## Eval cost

```bash
make eval
```

Prints and writes `cost` block in the eval JSON:

```json
"cost": {
  "n_llm_calls": 3,
  "prompt_tokens": 7200,
  "completion_tokens": 2700,
  "total_usd": 0.042,
  "by_purpose": [...]
}
```

Grafana reads the latest eval file for `horizon_eval_*` gauges after
each `make eval`.

## Dashboard

`observability/grafana/dashboards/horizon-overview.json` — session,
HITL, LLM cost, cadence, eval quality, runs by trigger/status.

## Alerts when the web app is down

No extra container. Use the **existing observability stack**:

| URL | Purpose |
|-----|---------|
| http://127.0.0.1:3000 | Grafana — `Ops alerts (open)` panel, web/worker health |
| http://127.0.0.1:9090/alerts | Prometheus — firing rules (`HorizonWebDown`, `HorizonAlertsOpen`, …) |
| `artifacts/ops.sqlite` (`ops_alerts` table) | Full message + traceback (shared volume) |

The **worker** exposes `/metrics` on `:9091` (same gauges as web, including
`horizon_alerts_open`). Prometheus job `horizon-worker` keeps alert metrics
fresh if `horizon-web` is unreachable.

In-app UI (when web is up): http://127.0.0.1:8080/alerts

## Prometheus alerts (infra)

`observability/prometheus/rules.yml`:

* `HorizonWebDown`
* `HITLBacklogGrowing`
* `LLMCostTodayHigh` (> $25)
* `EvalGroundedRatioLow`
* `CadenceStalledOnTradingDay`

## Logs

Web/worker JSON logs → Docker `json-file`. Ship to Loki/CloudWatch in
prod; shape from `app/core/logging.py`.
