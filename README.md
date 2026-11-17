# Dip-Recovery Hunter — BTC Race Bot V1

A long-only cbBTC/USDC trading bot for the make.fun competition
`btc-race-24h-20261117-0603` ("Race a BTC trading bot live for 24 hours").

## What it does

The bot speaks `agent-fun/bars-replay/v1` (NDJSON on stdin/stdout) and starts
with $1,000 USDC paper money. It buys dips off a trailing 4-hour high, but only
when the 12-hour trend is not down, exits with a take-profit / stop-loss / time
exit, and keeps a strict trade cap. If no dip ever fires, a slow EMA trend arm
takes one momentum trade; if still nothing by bar 1200, a small rescue trade
guarantees at least one trade. It never shorts, never uses leverage, never adds
to a loser, and never uses randomness (fully deterministic).

- Dip: 1.2% below the trailing 240-bar high + 0.15% bounce off the 15-bar low
  (2.5% after a losing trade), blocked when the 12h trend is below -1%.
- Exits: take profit +1.2%, stop loss -1.0%, max hold 4 hours.
- Guards: 30-bar cooldown after a sell, at most 4 trades, no new buys once
  equity falls under $970.

## Files

- `main.py` — the bot (Python 3.13, standard library only).
- `vendor/makefun_bars.py` — the kit's Python shim, copied byte for byte.

## Badges

[![make.fun · btc-race-24h-20261117-0603](https://make.fun/badge/btc-race-24h-20261117-0603.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![rank](https://make.fun/badge/btc-race-24h-20261117-0603/Web3beta/btc-race-24h-20261117-0603/rank.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
[![reward](https://make.fun/badge/btc-race-24h-20261117-0603/Web3beta/btc-race-24h-20261117-0603/reward.svg)](https://make.fun/bounty/btc-race-24h-20261117-0603?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261117-0603)
