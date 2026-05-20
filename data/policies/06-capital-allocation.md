# Capital Allocation & Portfolio Construction Policy

Horizon Capital runs a long-only US equity book. Agents must read the live
firm portfolio snapshot (positions, cash, sector weights, pending HITL,
**liquidity budget**) before recommending entries, sizing, or routing.
Decisions that ignore current exposure are non-compliant.

## §1 Liquidity (cash reserves)

| Band | % NAV | Role |
|------|-------|------|
| **Hard floor** | 5% | Never breached by a simulated buy (`simulate_order`). |
| **Operating target** | 8% | Minimum spare cash the book should maintain day-to-day. |
| **Soft ceiling** | 20% | Above this, bias toward deployment on approved setups. |

**Deployable cash** (mechanical, `app/allocation.py::liquidity_budget`):

```
pro_forma_cash = cash_usd − pending_HITL_notional
deployable     = max(0, pro_forma_cash − NAV × reserve_pct)
```

| Entry type | `reserve_pct` | Meaning |
|------------|---------------|---------|
| **New name (maiden)** | 8% (target) | Cannot deploy if post-trade cash would fall below 8% NAV. |
| **Add-on (already held)** | 5% (floor) | May use cash down to the hard floor only. |

**Agent obligations**

- **Idea Generator / Manager:** `max_new_openings` and `max_deploy_usd` in
  `scan_directives` are capped by deployable cash (e.g. at 3% per maiden name:
  `floor(deployable / (NAV × 3%))` slots).
- **Plan Builder:** `target_size_pct_nav = min(requested, sector headroom,
  max_maiden_entry_pct_nav)`; refuse draft if `can_open_new_name` is false.
- **Risk Officer:** reject if `simulate_order` cites `capital-allocation §1`
  (deployable cash or cash floor).
- **Execution:** `submit_order_sim` re-runs the same checks; no bypass.

**Bootstrap:** initial book targets ~83–85% invested (~15–17% cash), then
`_trim_seed_book_to_cash_target` ensures seed marks do not imply over-investment.

## §2 Invested capital band

Target **75–90%** of NAV in long positions over a full market cycle
(operating point **~85%** invested / **~15% cash**).

| Condition | Action |
|-----------|--------|
| Below 70% invested 30+ days | Bias Idea Scan toward `eligible_for_plan`. |
| Above 92% invested | No new maiden entries; add-ons ≤1% NAV only. |
| Cash below 8% target | **Freeze maiden entries** until reserve restored. |

## §3 Sector strategic weights (target ± tolerance band)

Bands are advisory for the LLM and enforced as a hard **25% NAV cap** per GICS
sector (investment-policy §2).

| Sector | Target % NAV | Band (±) |
| Information Technology | 28% | 5% |
| Health Care | 18% | 5% |
| Financials | 12% | 5% |
| Consumer Discretionary | 10% | 4% |
| Industrials | 10% | 4% |
| Communication Services | 8% | 3% |
| Energy | 5% | 3% |
| Materials | 4% | 2% |
| Real Estate | 3% | 2% |
| Utilities | 2% | 2% |
| Digital Assets (satellite) | 3% | 2% |
| Commodities (ETF proxy) | 4% | 2% |

Unlisted / Other: residual; hard cap 25%. Digital Assets **aggregate hard cap
10% NAV**; single crypto name **5% NAV**.

## §4 Diversification & single-name concentration

- **Position count:** target 10–16 simultaneous equity positions.
- **Single-name cap:** **8% NAV** (hard); warn at **7% NAV**.
- **Per-order cap:** 5% NAV per simulated order; maiden entries **3% NAV** default.

## §5 Sizing vs portfolio context

Before `eligible_for_plan`, agents must confirm:

(a) post-trade sector ≤ 25% NAV;  
(b) order ≤ **deployable cash** and preserves §1 reserve;  
(c) proposed size fits sector band headroom;  
(d) pending HITL included in pro-forma when material (>2% NAV).

Each Idea Scan pick should expose `suggested_notional_usd` and
`estimated_entry_pct_nav` clipped to liquidity.

## §6 Agent obligations (summary)

| Agent | Liquidity duty |
|-------|----------------|
| Portfolio Manager | Emit `scan_directives.max_deploy_usd`, freeze when cash < target |
| Idea Generator | Rank only names with `ok_to_add`; respect deploy cap |
| Fundamental | Cite cash / deployable in narrative |
| Plan Builder | Clip size; refuse when no deployable cash |
| Risk / Supervisor | Honor `simulate_order.liquidity` |
| Execution | No fill without feasible sim |

## §7 Rebalance philosophy

Quarterly tilt toward strategic weights; no forced intraday trades. Monthly
trim of names above 8% NAV or 1.5× sector band high via HITL only.
