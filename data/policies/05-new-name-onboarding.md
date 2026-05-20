# New-Name Onboarding Policy

Applies when Horizon Capital considers a US equity the firm does **not** yet
know: no dossier on file, not currently held, and not on the approved
watchlist with completed seasoning. Discovery via scan does not by itself
approve trading.

§1 Definitions.
- **Firm-known**: dossier exists OR ticker is on the approved watchlist with
  `watchlist_seasoned=true` OR we have held the name within the last 24 months.
- **New-to-firm**: passes universe filters (Investment Policy §1) but is not
  firm-known. Scan/API discovery candidates are new-to-firm until onboarding
  completes.
- **Maiden position**: first opening trade in a ticker we have never held.

§2 Scan layer (Idea Generator). The scan may surface new-to-firm names for
research only. To appear on the ranked shortlist with
`recommended_action = open_new_research`, ALL must be true:
- Real fundamentals and quote from live market data (not mock, not error stub).
- Market cap ≥ 5 billion USD (verified from fundamentals or profile).
- At least two independent sources, including at least one of:
  (a) SEC filing retrieved in the last 90 days, or
  (b) EDGAR discovery trigger (8-K / 10-Q) that surfaced the name.
- Composite score ≥ 0.65 (stricter than Investment Policy §4 for known names).
- Sector headroom and cash floor per Investment Policy §2.
Names that fail any gate receive `watch` or `skip`, never silent downgrade.

§3 Onboarding stages (mandatory before plan). New-to-firm names progress:
1. **Discovery** — Idea Generator scan or operator manual review.
2. **Research brief** — structured brief with `recommended_research_depth`
   of `deep_dive` (not `quick_scan`).
3. **Dossier draft** — Fundamental Analyst produces dossier JSON covering
   business description, segments, peer set, known risks, market cap; filed
   under `data/dossiers/{TICKER}.json`.
4. **Watchlist seasoning** — minimum 4 weeks on watchlist per Investment
   Policy §3 before Plan Builder may draft an entry plan.
5. **Plan + HITL** — Plan Builder drafts; operator sign-off required per
   Firm Charter §4. Autonomous execution is prohibited for maiden positions.

§4 Maiden position limits. Entry size 3% of NAV maximum (not the 3–5% band
for firm-known names). Max two concurrent maiden positions in the portfolio.
Max one new-to-firm name entering onboarding stage 3+ per calendar week.
Third maiden position requires operator pre-approval regardless of scores.

§5 Evidence and data integrity. No plan on placeholder or zeroed
fundamentals. No trade on news older than 48 hours (Risk Policy §3). If
yfinance or EDGAR is unavailable for a new-to-firm name, status must be
`blocked_on_data` until sources recover.

§6 Exclusions (hard). Do not onboard: excluded sectors (Investment Policy §1);
market cap under 5 billion; late filers; tickers under SEC investigation;
names with only a single non-SEC source; SPACs without de-SPAC 10-K.

§7 Operator override. Operator may add a ticker to the approved watchlist via
`manual_watchlist_add` (Operating Cadence §6), which starts seasoning but does
not waive dossier or HITL requirements for a maiden position.
