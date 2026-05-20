# Market data — free MCP sources

Horizon uses **free, keyless** market-data sources. In production Docker,
data is fetched **in-process** via `app/mcp_market/` (same APIs as popular
MCP servers — no subprocess required).

For **Cursor IDE** development you can additionally attach external MCP
servers — copy `.cursor/mcp.json.example` to `.cursor/mcp.json`.

## In-app providers (always free)

| Provider | MCP equivalent | Horizon tools |
|----------|----------------|---------------|
| **yfinance** | [narumiruna/yfinance-mcp](https://github.com/narumiruna/yfinance-mcp) | quotes, fundamentals, news, OHLCV history |
| **SEC EDGAR** | [mcp-edgar](https://github.com/cmanohar/mcp-edgar) | 8-K/10-Q discovery, filings, XBRL fundamentals |
| **CoinGecko** | [mcp.api.coingecko.com](https://mcp.api.coingecko.com/mcp) (public) | BTC/ETH quotes, trending, global market cap |
| **Frankfurter** | — | USD FX rates (macro context) |
| **Yahoo chart / Stooq** | — | quote fallbacks |
| **Google News RSS** | — | news fallback |

## Settings (`.env`)

```bash
MCP_MARKET_ENABLED=true          # route tools via mcp_market bridge
MCP_COINGECKO_TRENDING=true      # add trending coins to idea discovery
MCP_YFINANCE_HISTORY=true        # enable fetch_price_history helper
ENABLE_COINGECKO=true
ENABLE_FX_CONTEXT=true
```

## API

- `GET /api/market/mcp` — provider catalog + health
- Diagnostics page — **Market data MCP** table

## Cursor MCP config (optional, dev machine)

Requires Node.js for `npx mcp-remote`:

```bash
cp .cursor/mcp.json.example .cursor/mcp.json
```

Servers in the example:

1. **coingecko** — public remote `https://mcp.api.coingecko.com/mcp`
2. **yfinance** — `uvx` + yfinance-mcp (local)
3. **sec-edgar** — `npx mcp-edgar` (local)

These are for **IDE assistance** while coding. The running firm app uses
`app/mcp_market` adapters so trading agents work inside Docker without Node.

## What we did not use (paid / keys)

- Alpha Vantage, Polygon, IEX — require API keys
- CoinGecko Pro MCP — optional; public tier is enough for demo

## Policy

See `data/policies/08-multi-asset-data.md` for asset-class rules per provider.
