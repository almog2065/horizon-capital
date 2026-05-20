# Multi-Asset Data & Satellite Sleeves

Horizon Capital is primarily a **US long-only equity** firm. A small **satellite**
book may use other liquid instruments for diversification when policy and HITL allow.

§1 Asset classes & data providers (free APIs, no keys in demo).

In-app routing uses `app/mcp_market` (MCP-equivalent, in-process). Optional
Cursor MCP servers: see `docs/mcp-market-data.md` and `.cursor/mcp.json.example`.

| Class | Examples | Provider | Notes |
|-------|----------|----------|-------|
| US equity | MSFT, JPM | yfinance + SEC EDGAR | Universe §1: $5B+ mcap |
| Digital assets | BTC, ETH | CoinGecko public API | Spot USD; not GAAP fundamentals |
| Commodity proxy | GLD, USO, SLV, DBC | yfinance on listed ETFs | Underlying exposure via ETF |
| Rates proxy | TLT, SHY | yfinance on listed ETFs | Duration / cash-alternative sleeve |
| FX proxy | UUP, FXE | yfinance + Frankfurter context | Listed currency ETFs |
| BTC listed proxy | IBIT | yfinance | Complements spot BTC/ETH |

§2 Digital assets sleeve.
- **Aggregate cap:** Digital Assets sector ≤ **10% NAV** (hard).
- **Single-name cap:** **5% NAV** per crypto ticker (stricter than equity 8%).
- **No EDGAR / earnings gates** — agents use network/market metrics only.
- **Plans:** maiden crypto still requires dossier + HITL; Fundamental read must cite
  `asset_class=crypto` and refuse GAAP-only framing.
- **Discovery:** Idea Scan includes pool names BTC/ETH; EDGAR discovery is equity-only.

§3 Commodity proxy sleeve.
- Treated as **listed ETFs** (equity-like compliance): $5B+ issuer AUM where applicable;
  sector **Commodities**; same sector 25% hard cap as equities.
- Agents describe **underlying** (gold, oil, etc.) in thesis, not futures roll mechanics
  unless operator requests deep dive.

§4 Agent obligations.
- **Idea Generator:** route `data_provider` per ticker; skip crypto when CoinGecko fails.
- **Fundamental / Plan Builder:** apply class-specific gates (§2–§3); never invent PE/FCF
  for crypto.
- **Risk / simulate_order:** enforce per-class position caps from asset registry.
- **Portfolio Manager:** may bias Idea Scan toward underweight **Digital Assets** or
  **Commodities** when deploy mode and policy allow.

§5 Operator & HITL. Satellite entries default to **HITL required** until the firm
has two approved crypto/commodity plans on file (same discipline as new equity names).

§6 Macro context. Frankfurter (free) supplies USD FX rates for agent prompts;
does not replace ETF pricing. Portfolio Manager may set **deploy_urgency** and
**priority_tickers** when the book is under-invested — Idea Generator must weight
these above raw quality scores.
