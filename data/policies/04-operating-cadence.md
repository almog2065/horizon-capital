# Operating Cadence

§1 Scheduled daily. 07:30 ET pre-market integrity surfaces but does not act.
10:00 mid-morning drift check. 15:30 pre-close health check, no action.
16:30 EOD reconciliation.

§2 Weekly. Monday 08:00 thesis review and watchlist refresh. Friday 16:30
weekly summary.

§3 Monthly. 1st of month, 09:00 monthly rebalance: drift trims executed,
sector exposure check, watchlist pruning.

§4 Quarterly. Post-earnings season full re-underwriting of each holding,
refresh valuation targets, policy review.

§5 Event-driven triggers async: SEC 8-K on holding -> Filing Triage within 4h.
News on holding -> News Triage within 1h. Earnings release -> Fundamental
Analyst within 24h. Management change -> immediate. Materiality score >= 0.7
-> Fundamental Analyst within 24h.

§6 Manual triggers. operator can request manual_ticker_review, manual_watchlist_add,
replay_run.

§7 Contention rules. Event triggers preempt scheduled triggers within 60s.
Two event triggers on same ticker within 5 min collapse to one. Suspended
autonomy: all triggers run read-only.
