# Data persistence map

Runtime state is stored in **separate SQLite databases** on the artifacts volume
(Docker: `horizon-capital_artifacts` → `/app/artifacts/`). The repo only ships
**bootstrap seed files** under `data/` — not live firm state.

## Databases (runtime)

| File | Module | Contents |
|------|--------|----------|
| `firm.sqlite` | `app/db.py`, `app/traces.py` | Runs, plans, holdings, HITL, journal, trades, trace events |
| `vectors.sqlite` | `app/rag.py` | RAG chunk embeddings |
| `ops.sqlite` | `app/ops_db.py` | Ops alerts, daily plans, runtime dossiers |

Postgres and Redis in Compose are for **health / future cutover**; firm state
today is SQLite on the shared volume.

## Bootstrap only (repo, read at setup)

| Path | Loaded into |
|------|-------------|
| `data/policies/*.md` | `vectors.sqlite` (corpus `policy`) |
| `data/past_plans/*.json` | `vectors.sqlite` (`past_plans`) |
| `data/dossiers/*.json` | `ops.sqlite` (`dossiers`, once) |
| `data/news_samples/*.json` | RAG `news` (via seed) |
| `data/candidates.json` | In-memory universe metadata |
| `data/bootstrap/initial_holdings.json` | `firm.sqlite` holdings (first boot) |
| `data/bootstrap/synthetic_filings.json` | RAG `filings` |
| `data/bootstrap/news_seeds.json` | RAG `news` |

After bootstrap, edits (new dossiers, alerts, plans, positions) go to the DBs only.

## Reports (files)

Daily Excel/JSON reports remain under `artifacts/reports/<date>/` on the volume.

## Legacy migration

On startup, `ops_db.migrate_legacy_json_artifacts()` imports old
`artifacts/operations/ops_alerts.json` and `daily_plan_*.json` into
`ops.sqlite` and renames them to `*.migrated`.

## Inspect in Docker

```bash
docker exec horizon-web sqlite3 /app/artifacts/ops.sqlite ".tables"
docker exec horizon-web sqlite3 /app/artifacts/firm.sqlite ".tables"
```
