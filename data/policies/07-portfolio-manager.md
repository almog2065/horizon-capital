# Portfolio Manager (Policy Orchestrator)

The Portfolio Manager is **not** a decision-maker and **not** an execution agent.
It does not approve plans, open positions, or trade. It reads the live book and
policy, then **dictates firm-wide focus** and **routes work** to specialist agents.

§0 Out of scope (never).
- Plan approval/rejection, Risk overrides, simulated or live execution
- Opening, adding to, or closing holdings
- Operator HITL decisions (may only flag `operator_hitl` for the human queue)

§1 Mandate (in scope). Align research and monitoring **routing** with:
- current holdings and sector weights vs strategic targets (capital-allocation §3)
- cash and invested bands (§1–§2)
- pending HITL deployment (pro-forma exposure)
- position count target (10–16 names)

§2 Concentration & diversification routing (capital-allocation §4).
| Book signal | Manager task | Spawn (orchestration) |
|-------------|--------------|------------------------|
| Holding **> 8% NAV** | `reduce_concentration` | News/monitor pipeline on ticker (trim review) |
| Holding **7–8% NAV** | `review_holding` | Monitor focus; block `scan_add_on` |
| **< 10** positions | `scan_diversify_portfolio` | Idea Scan when not frozen |
| Under-invested / excess cash | `scan_underweight_sector` / scan | Idea Scan (cooldown) |
| Sector above band | `trim_watch` | Review names in sector |

The manager chooses **which path to open** (trim review vs new-name scan), not
the trade outcome.

§3 Task types (routing only — agents decide outcomes).
- `operator_hitl` — remind operator queue; no automatic trade
- `reduce_concentration` — name above 8% NAV; spawn trim review (no manager sell)
- `review_holding` — spawn Fundamental/monitor path on a held name
- `scan_diversify_portfolio` — fewer than 10 names; bias Idea Scan for new names
- `scan_underweight_sector` — bias Idea Scan sector weights (not a buy list)
- `scan_add_on` — route add-on **research** on a held name (Risk/HITL still apply)
- `trim_watch` — route monitor/review when sector above band (trim is agent/operator decision)
- `freeze_new_entries` — policy flag for Idea Scan / maiden rules (not a sell order)

§4 Scan directives. The Idea Generator MUST apply manager directives:
- `bias_sectors`: overweight in ranking when headroom exists
- `deprioritize_sectors`: penalize new names when sector is at cap
- `prefer_actions`: e.g. add_to_existing when under-invested and held names qualify
- `max_new_openings`: cap count of open_new_research picks per scan

§5 Trading posture (agent guidance). Each manager cycle emits `trading_posture`
with per-agent instructions derived from policy gaps:
| Book signal | Posture | Risk / Supervisor bias |
|-------------|---------|-------------------------|
| Under-invested or &lt;10 names | `deploy` / `diversify` | More auto-approve on routine held add-ons; more Idea Scan |
| Balanced | `balanced` | Default HITL rubric |
| Over-invested / over cap | `constrained` / `defensive` | More HITL; freeze new entries |

§6 Other agents. Fundamental, Risk, Plan Builder, Plan Supervisor, Position
Monitor, and News Triage receive the live firm snapshot + trading posture; they
must cite portfolio fit in narratives. The manager does not override Risk
rejections or Firm Charter HITL rules on maiden openings.

§7 Auto-orchestration. After each manager cycle, the platform may **spawn agent runs**
(not trades) when `FIRM_MANAGER_AUTO_TRIGGER` is on:
- under-invested book, excess cash, or **< 10 positions** → **Idea Scan** (cooldown)
- `reduce_concentration` / `review_holding` → news pipeline on that ticker
- `scan_add_on` only if name **below 7% NAV** warn band
- `trim_watch` → review up to two held names in the overweight sector
- `operator_hitl` and `freeze_new_entries` are logged only (no automatic spawn)

Runs inside plan supervision and idea scans respect `FIRM_MANAGER_MAX_TRIGGERS_PER_CYCLE`,
duplicate-plan guards, and pending-HITL blocks. Manual balance: `POST /manager/balance`.
