# Risk Policy

§1 Portfolio limits. Drawdown alert at -10% from 30d high freeze new entries.
Drawdown halt at -15% suspends autonomy. Beta target 0.8-1.1. Single-name
VaR 95%/1d under 1.5% of NAV. Sector exposure cap 25%. Cash floor 5%.

§2 Per-position limits. Single position cap 8% NAV auto-trim (equities and
commodity ETFs). **Crypto single-name cap 5% NAV**; **Digital Assets aggregate
10% NAV** (multi-asset policy §8). Loss from cost basis with intact thesis -25%
triggers re-review. Loss with flagged thesis -10% triggers HITL exit decision.

§3 Information limits. No trade on news older than 48h. No trade last 30 min
of session. Earnings blackout: 7 days before, 24h after.

§4 Override path. Auditor severity high freezes plan. Two consecutive
deviations escalate to operator. Risk Officer rejection is final in
autonomous loop; only operator overrides via HITL.

§5 Audit trail. Every agent action records policy_section_cited,
retrieval_log, inputs_hash, outputs_schema_version, as_of.

§6 Hard limits enforced in tool layer, not LLM. Max order size 5% NAV per
call. No order without active approved plan. No trade in suspended
autonomy. Cash floor check on every order.
