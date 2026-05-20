# Docs index

Suggested reading order for a reviewer or new contributor.

| #   | File                                                                      | Time   | Purpose                                   |
|-----|---------------------------------------------------------------------------|--------|-------------------------------------------|
| 1   | [`how-it-works-simple.md`](how-it-works-simple.md)                        | 3 min  | Plain-English tour of the firm            |
| 2   | [`business-case.md`](business-case.md)                                    | 4 min  | Firm concept + value                      |
| 3   | [`architecture-diagrams.md`](architecture-diagrams.md)                    | 5 min  | Mermaid: logical + deployment views       |
| 3a  | [`system-flow-diagrams.html`](system-flow-diagrams.html)                  | 5 min  | SVG flow diagrams: scheduler, RAG, per-agent (×9), full interaction map |
| 4   | [`walkthrough-one-trade.md`](walkthrough-one-trade.md)                    | 8 min  | End-to-end one trade, narrated            |
| 5   | [`agent-contracts.md`](agent-contracts.md)                                | 6 min  | Typed I/O per agent                       |
| 6   | [`frameworks-deep-dive.md`](frameworks-deep-dive.md)                      | 10 min | FastAPI, LangGraph, Pydantic, K8s, …      |
| 7   | [`technical-overview.md`](technical-overview.md)                          | 8 min  | One-stop senior-level summary             |
| 8   | [`eval-results.md`](eval-results.md)                                      | 4 min  | What the eval harness reports             |
| 9   | [`demo-script.md`](demo-script.md)                                        | 5 min  | Choreography for the 10-min live demo     |
| 10  | [`guardrails.md`](guardrails.md)                                          | 4 min  | Input validation, schema checks, refusals |
| 11  | [`mcp-market-data.md`](mcp-market-data.md)                                | 4 min  | Asset-class providers, keyless where possible |

## Decisions & runbooks

| File                                                                                | Purpose                              |
|-------------------------------------------------------------------------------------|--------------------------------------|
| [`adr/0001-multi-container-deployment.md`](adr/0001-multi-container-deployment.md) | Why web/worker/migrate split         |
| [`adr/0002-state-and-iac.md`](adr/0002-state-and-iac.md)                           | SQLite → Postgres path + IaC tooling |
| [`runbooks/firm-operations.md`](runbooks/firm-operations.md)                        | Day-to-day operations                |
| [`runbooks/incident-response.md`](runbooks/incident-response.md)                    | Triage + postmortem template         |

## Fast pointers

* "Where does state live?" → `walkthrough-one-trade.md` §HITL + `frameworks-deep-dive.md` §LangGraph
* "How is config done?" → `frameworks-deep-dive.md` §Pydantic
* "What's the deploy story?" → `frameworks-deep-dive.md` §Terraform + §GitHub Actions + `demo-script.md` Step 8
* "How are agents tested?" → `eval-results.md` + tests under `../tests/`
* "What would break in prod, and what's next?" → README §"Next three"
