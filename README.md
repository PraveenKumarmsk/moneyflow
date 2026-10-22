# MoneyFlow Intelligence Terminal

Single-page Indian market dashboard: Command Center, Stock Radar, OI Flow, Option Pulse, Money Flow,
RSI Screener, Liquidity Map, Calm Zone, Trade Journal, Alert Center, F&O Academy - all on one page.

## Run
```bash
cp .env.example .env      # add UPSTOX_ACCESS_TOKEN for live data
./run.sh                  # open http://localhost:8000
# or: docker build -t moneyflow . && docker run -p 8000:8000 --env-file .env moneyflow
```
Tests: `cd backend && python -m pytest -q`

## Data sources (the badge at the top always says which one is active)
| Mode | How | What you get |
|---|---|---|
| **LIVE** | Upstox API (`UPSTOX_ACCESS_TOKEN`) | Quotes (~3 s poll), candles, futures OI, option chain + greeks |
| **DELAYED** | `DATA_PROVIDER=yahoo` (unofficial, personal use) | Quotes + candles, ~15 min delay. No OI / options |
| **DEMO** | default when no token | Synthetic data with a red banner. Never live |

### Getting live data with Upstox (free account)
1. Create an app at https://account.upstox.com/developer/apps (redirect URL: any you control).
2. Complete the OAuth login to get an **access token** (valid until ~3:30 AM IST next day - regenerate daily).
3. Put it in `.env` as `UPSTOX_ACCESS_TOKEN=...`, restart. Instruments (equity, current-month futures, option expiries) are resolved automatically from Upstox's daily instrument file.

## What is real vs. not
- FII/DII: no free official API; enter the NSE-published numbers manually (never invented).
- Global cues: only with `GLOBAL_CUES=yahoo` (or yahoo/demo mode). Economic/earnings calendars: not included.
- OI change = since this app began tracking (not vs. previous close), shown with the baseline time.
- Universe: 31 liquid F&O stocks in `backend/universe.py` (edit to add more). Breadth is for this universe only.
- Exchange holidays are not modelled in the OPEN/CLOSED indicator.
- Alerts, journal, watchlist, saved screens live in your browser (localStorage).

## Not built yet (from the master spec)
User auth + Postgres/Redis, admin panel, WebSocket push (uses 3 s polling), backtesting/replay,
pre-market brief, opening-range radar, IV rank/percentile, bid/ask depth, email/push alerts.

## Disclaimer
Education and analytics only. Not investment advice; no accuracy or returns promised.
