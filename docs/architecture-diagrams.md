# Architecture diagrams

Mermaid sources for the diagrams the brief asks for: **logical view**
(agents, orchestration, RAG, state, HITL, observability) and
**deployment view** (where everything lives in dev, K8s, AWS).

If your reader doesn't have a Mermaid-aware viewer (most IDEs and
GitHub do), the PNG/SVG pair at the repo root is the canonical static
copy.

---

## 1. Logical view — agent pipeline

```mermaid
flowchart LR
    EVT[News event / Scheduled tick]
    TRG[/trigger or scheduler/]
    EVT --> TRG

    subgraph FIRM["Firm graph (LangGraph)"]
      direction LR
      NT[news_triage]
      IG[idea_generator]
      FUND[fundamental]
      PB[plan_builder]
      PS[plan_supervisor]
      RO[risk_officer]
      PM[position_monitor]
      AUD[auditor]
      MGR[firm_manager]
    end

    RAG[(RAG corpora<br/>policy &middot; news<br/>filings &middot; past_plans)]
    HITL{Risk Committee<br/>HITL queue}
    EX[portfolio.execute_trade<br/>slippage + commission]
    DB[(firm.sqlite<br/>checkpoints.sqlite)]
    REP[Reports:<br/>UI + Excel + JSON logs]

    TRG --> NT
    NT -- relevant --> IG
    IG --> FUND
    FUND --> PB
    PB --> PS
    PS --> RO
    RO -- under threshold --> EX
    RO -- over threshold --> HITL
    HITL -- approve --> EX
    HITL -- reject --> DB
    EX --> DB
    EX --> REP

    PM -.event-driven.-> FUND
    MGR -.policy / cadence.-> IG

    FUND -. retrieves .-> RAG
    PB -. cites .-> RAG
    PS -. consistency .-> RAG
    RO -. policy lookup .-> RAG
    AUD -. counter-evidence .-> RAG

    AUD -. side-channel writes .-> DB
    NT --> AUD
    FUND --> AUD
    PB --> AUD
    RO --> AUD
```

---

## 2. State flow during a HITL pause

```mermaid
sequenceDiagram
    autonumber
    participant E as Event source
    participant G as LangGraph
    participant S as SqliteSaver (checkpoints)
    participant Q as HITL queue (firm.sqlite)
    participant U as Operator UI
    participant X as portfolio.execute_trade

    E->>G: trigger run
    G->>G: news_triage → fundamental → plan_builder → plan_supervisor
    G->>G: risk_officer.decision = require_hitl
    G->>S: checkpoint(state)
    G->>Q: enqueue(plan, citations, thread_id)
    G-->>E: 202 Accepted, run_paused
    Note over G: process can crash here<br/>state survives in S + Q
    U->>Q: GET pending items
    U->>Q: POST approve(item_id)
    Q->>G: resume(thread_id)
    G->>S: load(thread_id)
    G->>X: execute(plan)
    X-->>G: filled
    G-->>U: 200 OK (UI refresh)
```

---

## 3. Process model — what runs where

```mermaid
flowchart TB
    subgraph Web["web container (FastAPI, x2 in HPA)"]
      UV[uvicorn workers]
      API[/health, ready, metrics<br/>UI routes<br/>reports endpoints/]
      LIFE[lifespan boot:<br/>db init, RAG seed,<br/>HITL repair]
    end

    subgraph Worker["worker container (x1, single replica)"]
      SCH[scheduler_worker.py:<br/>plan_supervision_loop<br/>firm_balance_loop]
      LIFE2[bootstrap_only]
    end

    subgraph BackingServices["backing services"]
      PG[(postgres:16)]
      RD[(redis:7)]
    end

    subgraph Edge["edge"]
      NG[nginx:1.27<br/>gzip, X-Forwarded-*]
    end

    Client((client)) --> NG
    NG --> UV
    UV --> API
    LIFE -.boot.-> PG
    LIFE2 -.boot.-> PG
    SCH -. ticks .-> PG
    API -. read/write .-> PG
    API -. cache/locks .-> RD
    SCH -. cache/locks .-> RD
```

---

## 4. Container topology (Docker Compose)

```mermaid
flowchart LR
    Internet([Internet]) -- :8080 --> nginx
    subgraph compose["docker compose (horizon network)"]
        nginx[nginx<br/>1.27-alpine] --> web[web<br/>FastAPI<br/>2 replicas]
        web --> postgres[(postgres<br/>16-alpine)]
        web --> redis[(redis<br/>7-alpine)]
        worker[worker<br/>scheduler] --> postgres
        worker --> redis
    end
    migrate[migrate<br/>one-shot] -.profile=migrate.-> postgres

    subgraph obs["observability overlay"]
      prom[prometheus<br/>v2.55] -- scrape /metrics --> web
      graf[grafana<br/>11.2] --> prom
    end
```

---

## 5. Deployment view — Kubernetes (Kustomize)

```mermaid
flowchart TB
    subgraph cluster["kubernetes cluster"]
        subgraph ns["namespace: horizon-capital (PSA: restricted)"]
            ingress[nginx-ingress<br/>Ingress: web] --> websvc[Service<br/>web :80→8000]
            websvc --> webdeploy[Deployment<br/>web replicas=2-8 HPA]
            workerdeploy[Deployment<br/>worker replicas=1, Recreate]

            webdeploy --> pgsvc[Service<br/>postgres :5432]
            workerdeploy --> pgsvc
            webdeploy --> redissvc[Service<br/>redis :6379]
            workerdeploy --> redissvc

            pgsvc --> pgss[StatefulSet<br/>postgres + PVC 10Gi]
            redissvc --> rdep[Deployment<br/>redis]

            np[NetworkPolicy<br/>default-deny + targeted allow]
            pdb[PodDisruptionBudget<br/>web minAvailable=1]
            cm[ConfigMap horizon-config]
            sec[Secret horizon-secrets<br/>via ExternalSecrets]
        end
    end
```

---

## 6. Deployment view — AWS (Terraform)

```mermaid
flowchart TB
    Internet([Internet]) --> R53[Route53<br/>optional]
    R53 --> ALB[ALB<br/>health: /healthz]

    subgraph VPC["VPC 10.20.0.0/16 (2 AZs)"]
        subgraph publicSubnets["public subnets"]
            ALB
            NAT[NAT gateway]
        end

        subgraph privateSubnets["private subnets"]
            subgraph ECS["ECS Fargate cluster"]
                webtask[Service: web<br/>desired_count=3]
                workertask[Service: worker<br/>desired_count=2]
            end

            RDS[(RDS Postgres 16<br/>multi-AZ in prod)]
            REDIS[(ElastiCache Redis 7)]
        end
    end

    ALB --> webtask
    webtask --> RDS
    webtask --> REDIS
    workertask --> RDS
    workertask --> REDIS
    webtask -. egress .-> NAT
    workertask -. egress .-> NAT
    NAT --> Internet

    SSM[SSM Parameter Store<br/>SecureString:<br/>openai_api_key, db_password] --> webtask
    SSM --> workertask
    CW[CloudWatch Logs<br/>/ecs/web, /ecs/worker] --- webtask
    CW --- workertask
    ECR[(ECR<br/>horizon-capital:tag)]
    ECR --> webtask
    ECR --> workertask
```

---

## 7. CI/CD pipeline

```mermaid
flowchart LR
    dev([dev]) -- push --> repo[(GitHub)]
    repo --> ci{ci.yml}
    ci --> lint[ruff lint]
    lint --> test[pytest + services]
    test --> eval[make eval]
    eval --> dockerbuild[docker build]
    dockerbuild --> done([CI green])

    repo -- tag v*.*.* --> rel{release.yml}
    rel -- OIDC --> aws[(AWS)]
    rel --> push[docker push ECR]
    push --> deploy[ecs update-service<br/>--force-new-deployment]
    deploy --> wait[aws ecs wait services-stable]
    wait --> live([live on env])

    repo -- PR on infra/ --> tf{terraform.yml}
    tf --> plan[terraform plan]
    plan --> review([review on PR])
```

---

## 8. Trace data model

```mermaid
erDiagram
    RUNS ||--o{ AGENT_CALLS : "spans"
    AGENT_CALLS ||--o{ LLM_CALLS : "produces"
    AGENT_CALLS ||--o{ TOOL_CALLS : "issues"
    RUNS ||--o| HITL_ITEMS : "may pause"
    RUNS ||--o{ TRADES : "produces"
    TRADES ||--o{ CITATIONS : "supported by"

    RUNS {
      int id PK
      string ticker
      string event_kind
      timestamp started_at
      timestamp finished_at
      string status
    }
    AGENT_CALLS {
      int id PK
      int run_id FK
      string agent
      json input
      json output
      int duration_ms
    }
    LLM_CALLS {
      int id PK
      int agent_call_id FK
      string model
      string mode
      string purpose
      text system
      text user
      json response
      json tokens
      int duration_ms
    }
    HITL_ITEMS {
      int id PK
      int run_id FK
      string status
      string thread_id
      json proposed_trade
    }
    TRADES {
      int id PK
      int run_id FK
      string side
      int qty
      real price
      real realized_pnl
      bool hitl
    }
    CITATIONS {
      int id PK
      int trade_id FK
      string key
      string corpus
    }
```

---

## 9. Configuration & secrets flow

```mermaid
flowchart LR
    subgraph sources["sources, by precedence"]
        env[OS env]
        envfile[.env file]
        secrets["/run/secrets/* (Docker secrets)"]
        defaults[Settings defaults]
    end

    sources --> SET[app/core/settings.py<br/>Pydantic Settings]
    SET --> SHIM[app/config.py<br/>back-compat shim]
    SET --> APP[App code:<br/>get_settings()]

    subgraph cloudSecrets["cloud secret stores"]
        SSM[AWS SSM<br/>SecureString]
        K8sExt[ExternalSecrets<br/>Vault / AWS SM]
    end

    SSM -. mounts as env .-> env
    K8sExt -. mounts as /run/secrets/* .-> secrets
```

---

## How these diagrams are used

* **README** points readers here.
* **demo-script.md** says to open §1 (the agent pipeline) during the
  architecture intro.
* **walkthrough-one-trade.md** is the narration for §1 + §2.
* The repo root carries the static `architecture.{dot,svg,png}` (logical),
  `architecture_simple.{dot,svg,png}` (plain English) and
  `architecture_deployment.{dot,svg,png}` (deployment view) as fallbacks
  for viewers that don't render Mermaid.
