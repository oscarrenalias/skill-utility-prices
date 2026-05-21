# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script Python tool that logs into PKS Live (a Finnish electricity retailer's customer portal) and pulls two things:

1. **Price-fixing data** ("hintakiinnitys") — current snt/kWh prices for monthly/quarterly periods and multi-period bundles. Each run appends a snapshot to the local SQLite history.
2. **Metered consumption / actuals** — daily kWh + cost per metering point, with day/night/winter-day breakdown and per-day min/max/avg. Each run upserts rows keyed by `(metering_point_id, contract_type, period_start)`, so re-fetching the same window refreshes any restated values.

Designed to run once a day from cron.

The tool is packaged as a self-contained agent skill at **`.apm/skills/utility-prices/`** (intended for distribution via `apm`). That folder is the uv project — it owns `pyproject.toml`, `uv.lock`, `pks_prices.py`, `SKILL.md`, `.env`, and the SQLite DB (`pks_prices.db`). The repo root only holds dev/maintenance artifacts (this file).

## Commands

uv-managed project (Python 3.12+). Always use `uv run` so the right venv is active. From the repo root, point uv at the skill folder via `--directory`:

```bash
# Prices (append-only snapshots)
uv run --directory .apm/skills/utility-prices python pks_prices.py prices fetch
uv run --directory .apm/skills/utility-prices python pks_prices.py prices fetch --debug
uv run --directory .apm/skills/utility-prices python pks_prices.py prices fetch --json

# Actuals (upserted by natural key)
uv run --directory .apm/skills/utility-prices python pks_prices.py actuals fetch --json
uv run --directory .apm/skills/utility-prices python pks_prices.py actuals fetch --from 2026-04-01 --to 2026-04-30 --json
uv run --directory .apm/skills/utility-prices python pks_prices.py actuals fetch --days 7 --debug

# Read-only DB helpers
uv run --directory .apm/skills/utility-prices python pks_prices.py describe
uv run --directory .apm/skills/utility-prices python pks_prices.py query "<SQL>"
uv run --directory .apm/skills/utility-prices python pks_prices.py runs

# Or cd in once and drop --directory:
cd .apm/skills/utility-prices && uv run python pks_prices.py describe

# First-time setup (or after dependency changes):
cd .apm/skills/utility-prices && uv sync

# Add a new dependency to the skill:
cd .apm/skills/utility-prices && uv add <package>
```

There are no tests, no linter, no formatter configured. Don't add them unless asked.

The CLI requires an explicit subcommand — there is no default action. `pks_prices.py` with no args prints help.

## Architecture

### Two fetch flows, one shared first step

Both `prices fetch` and `actuals fetch` start by getting a Cognito ID token via SRP (`USER_SRP_AUTH`). After that they diverge:

**`prices fetch` (6 steps)** — the harder of the two. There are **two stacked sites**, and the data lives on the legacy one:

1. `pkslive.pks.fi` — modern React SPA, **AWS Cognito** auth (user pool `eu-west-1_s5PJjscpB`, client `4nre7e2dhlbbmmvh0egegjg4d6`), GraphQL backend at `graphql.akamon.cloud`.
2. `live.pks.fi` — legacy ASP.NET site that actually serves the price data.

```
Cognito SRP (USER_SRP_AUTH) → ID token (RS256 JWT)
  → GraphQL `priima_live_externalMagicLink` mutation → 60-second magic-link JWT
    → GET live.pks.fi/Kirjaudu/PKS/Online?token=<jwt> → session cookies set
      → REST + SignalR calls on live.pks.fi
```

The magic-link JWT expires in **60 seconds** — don't insert long-running steps between getting it and redeeming it.

**`actuals fetch` (4 steps)** — much simpler. Everything is on the modern stack:

```
Cognito SRP → ID token
  → GraphQL `customerships_context_Customerships` → customerId
    → GraphQL `mp_select_CustomerMeteringPoints` → list of meteringPointIds + types
      → GraphQL `energy_usage_GetConsumptionData` (per meter) → daily readings
```

All four calls go to `graphql.akamon.cloud` with the same Cognito ID token as the `authorization` header. No `live.pks.fi` traversal, no SignalR, no HTML scrape. Auth lifetime is the full ID-token lifetime (~1 hour), not the 60-second magic-link window.

### Prices come from two places (legacy site)

| What | Source | Code |
|---|---|---|
| Multi-period bundle prices (year-end, next-12-months, next-year, second-half) | REST `Api/MultiTransaction/PricesV2/{userId}/true` | `fetch_multi_prices` |
| Monthly + quarterly period prices | **SignalR** `priceHub.invoke('getNewestPrices', tubeType)` over WebSocket | `fetch_period_prices_via_signalr` |
| Period definitions (names, dates, types) | REST `Api/Periods/Available` | `fetch_periods_map` |

The SignalR hub is **classic ASP.NET SignalR (clientProtocol 1.5)**, not ASP.NET Core SignalR. Common Python libraries (`signalrcore`) target the wrong protocol — we hand-roll the WS protocol: `negotiate` (HTTP) → WebSocket connect → `start` (HTTP) → send `{"H":"priceHub","M":"getNewestPrices","A":[tubeType],"I":"0"}` → wait for frame with matching `I`.

### Actuals come from one GraphQL query

`energy_usage_GetConsumptionData` returns two parallel arrays:
- `consumption.range.items[]` — `{startTime, endTime, sum, costWithVat, costWithoutVat, status, unit}` per day
- `consumption.sumTimeSeries.sumValues[]` — `{start, stop, value, valueCount, minValue, maxValue, minValueTime, maxValueTime, avgValue, dayTime, nightTime, winterDayTime}` per day

`merge_consumption()` joins them on the day's start time. Both must come from the same query — the API doesn't expose them on independent endpoints.

The query takes a `resolution: ResolutionDuration!` (ISO-8601: `P1DT`, `PT1H`, `PT15M`). The persisted schema (`consumption_daily`) is daily-shaped; finer resolutions are accepted on the wire but only the daily aggregates from `range.items` are stored. Re-fetching at the same resolution refreshes existing rows (upsert).

### The `userId` (e.g. `44665`) for `prices fetch` is **server-rendered into HTML**

It's not the Cognito sub, customer ID, or contract ID. It only appears in the inline JS on `/Hinnankiinnitys/Jaksot` (`PeriodsList.InitList('True', '2', 44665, 25.5)`), so we GET that page and regex-scrape it (`fetch_periods_page_user_id`). If PKS changes the page markup, this regex breaks and the multi-period prices fail.

`actuals fetch` does **not** need this — it uses the GraphQL `customerId` (`2137637`-style), which comes from `customerships_context_Customerships` and is different from the legacy `userId`.

### Persistence

`pks_prices.db` (SQLite, auto-created beside the script). Four tables:

Append-only (price snapshots), keyed by a shared `fetched_at` UTC ISO-8601 timestamp:
- `period_prices` — one row per period per snapshot. `period_type` 0=monthly, 1=quarterly.
- `multi_prices` — one row per multi-period bundle per snapshot. `bundle_type` 0=year-end, 1=next-12-months, 2=next-year, 3=second-half.

Upserted (actuals):
- `meter_points` — one row per metering point ever observed; PK is `metering_point_id`. `type` is `Sales` (consumption) or `SalesProduction` (production / feed-in). `first_seen_at` is preserved across upserts.
- `consumption_daily` — daily aggregates. PK is `(metering_point_id, contract_type, period_start)`. Re-fetching the same window refreshes `sum_kwh`, costs, day/night splits, and `fetched_at`. `period_start` is a Helsinki day boundary expressed in UTC (e.g. `2026-05-01T21:00:00Z` = midnight Helsinki summer; `2026-12-01T22:00:00Z` = midnight Helsinki winter).

`describe`/`query`/`runs` open the DB **read-only** via `file:?mode=ro` URI. Only fetch commands write. `runs` lists price-fixing snapshots only — actuals don't have a snapshot concept.

### When the flow breaks

For `prices fetch`, re-capture the login → "Priima Live" → `/Hinnankiinnitys/Jaksot` flow in Chrome DevTools and inspect the network log. The script's `[1/6]…[6/6]` debug labels (visible with `--debug`) map directly onto the steps you'll see in the trace.

For `actuals fetch`, capture the `pkslive.pks.fi` dashboard and look at the calls to `graphql.akamon.cloud`. The script's `[1/4]…[4/4]` debug labels show which GraphQL operation failed (`customerships_context_Customerships`, `mp_select_CustomerMeteringPoints`, `energy_usage_GetConsumptionData`).

## Known gotchas

- **Federated logins won't work.** Some PKS users authenticate via TELIA-IBS (Finnish bank/mobile ID) and don't have a Cognito-direct password. `USER_SRP_AUTH` will fail for those users with `UserNotFoundException`. Both fetch flows are affected.
- **`tubeType=1` is the default** for prices, server-rendered per tenant in the page's inline JS (`var tubeType = (('1') === '2') ? 2 : 1;`). Override with `--tube-type` if needed.
- **`productIdentifier` is per-contract.** Defaults to `Priima`. Override with `--product-identifier` if the user is on a different product. We don't currently auto-discover it (the contracts GraphQL query is bypassed to keep `actuals fetch` to four hops).
- **Day boundary timezone matters.** Consumption data is bucketed by Helsinki calendar day, but timestamps are in UTC. The script converts `--from`/`--to` (YYYY-MM-DD, Helsinki) to UTC via `zoneinfo.ZoneInfo("Europe/Helsinki")` — DST shifts the day boundary from `21:00Z` (summer) to `22:00Z` (winter). Don't store a fixed offset.
- **SignalR response shape is best-effort.** WebSocket frames are not introspectable from HTTP captures, so `normalize_period_prices()` handles both list-of-dicts and dict-of-id→price shapes defensively. If a future schema change breaks this, that function is where to look.
- **No CLI default subcommand.** Every invocation must say `prices`, `actuals`, `describe`, `query`, or `runs`. The previous no-arg-implies-`fetch` shim was removed.
