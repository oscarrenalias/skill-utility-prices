# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script Python tool that logs into PKS Live (a Finnish electricity retailer's customer portal) and pulls electricity contract price-fixing data ("hintakiinnitys"). Each run appends a snapshot to a local SQLite history. Designed to run once a day from cron.

The tool is packaged as a self-contained agent skill at **`.apm/skills/utility-prices/`** (intended for distribution via `apm`). That folder is the uv project — it owns `pyproject.toml`, `uv.lock`, `pks_prices.py`, `SKILL.md`, `.env`, and the SQLite DB (`pks_prices.db`). The repo root only holds dev/maintenance artifacts (this file).

## Commands

uv-managed project (Python 3.12). Always use `uv run` so the right venv is active. From the repo root, point uv at the skill folder via `--directory`:

```bash
uv run --directory .apm/skills/utility-prices python pks_prices.py            # fetch + persist + print
uv run --directory .apm/skills/utility-prices python pks_prices.py --debug    # progress on stderr
uv run --directory .apm/skills/utility-prices python pks_prices.py fetch --json
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

## Architecture

### The auth flow is the hard part — and it's two-stack

There are **two stacked sites**, and you must traverse both to reach the data:

1. **`pkslive.pks.fi`** — modern React SPA, **AWS Cognito** auth (user pool `eu-west-1_s5PJjscpB`, client `4nre7e2dhlbbmmvh0egegjg4d6`), GraphQL backend at `graphql.akamon.cloud`.
2. **`live.pks.fi`** — legacy ASP.NET site that actually serves the price data (matches `sample.html`).

Login is a **5-step pipeline**, mirrored exactly by `cmd_fetch()`:

```
Cognito SRP (USER_SRP_AUTH) → ID token (RS256 JWT)
  → GraphQL `priima_live_externalMagicLink` mutation → 60-second magic-link JWT
    → GET live.pks.fi/Kirjaudu/PKS/Online?token=<jwt> → session cookies set
      → REST + SignalR calls on live.pks.fi
```

The magic-link JWT expires in **60 seconds** — don't insert long-running steps between getting it and redeeming it.

### Prices come from two places

| What | Source | Code |
|---|---|---|
| Multi-period bundle prices (year-end, next-12-months, next-year, second-half) | REST `Api/MultiTransaction/PricesV2/{userId}/true` | `fetch_multi_prices` |
| Monthly + quarterly period prices | **SignalR** `priceHub.invoke('getNewestPrices', tubeType)` over WebSocket | `fetch_period_prices_via_signalr` |
| Period definitions (names, dates, types) | REST `Api/Periods/Available` | `fetch_periods_map` |

The SignalR hub is **classic ASP.NET SignalR (clientProtocol 1.5)**, not ASP.NET Core SignalR. Common Python libraries (`signalrcore`) target the wrong protocol — we hand-roll the WS protocol: `negotiate` (HTTP) → WebSocket connect → `start` (HTTP) → send `{"H":"priceHub","M":"getNewestPrices","A":[tubeType],"I":"0"}` → wait for frame with matching `I`.

### The `userId` (e.g. `44665`) is **server-rendered into HTML**

It's not the Cognito sub, customer ID, or contract ID. It only appears in the inline JS on `/Hinnankiinnitys/Jaksot` (`PeriodsList.InitList('True', '2', 44665, 25.5)`), so we GET that page and regex-scrape it (`fetch_periods_page_user_id`). If PKS changes the page markup, this regex breaks and the multi-period prices fail.

### Persistence

`pks_prices.db` (SQLite, auto-created beside the script). Two append-only tables, both keyed by a shared `fetched_at` UTC ISO-8601 timestamp:

- `period_prices` — one row per period per snapshot. `period_type` 0=monthly, 1=quarterly.
- `multi_prices` — one row per multi-period bundle per snapshot. `bundle_type` 0=year-end, 1=next-12-months, 2=next-year, 3=second-half.

`describe`/`query`/`runs` subcommands open the DB **read-only** via `file:?mode=ro` URI. Only `fetch` writes.

### When the flow breaks

If auth or price retrieval breaks, re-capture the login → "Priima Live" → `/Hinnankiinnitys/Jaksot` flow in Chrome DevTools and inspect the network log. The script's `[1/6]…[6/6]` debug labels (visible with `--debug`) map directly onto the steps you'll see in the trace, so you can narrow the failure to a specific stage.

## Known gotchas

- **Federated logins won't work.** Some PKS users authenticate via TELIA-IBS (Finnish bank/mobile ID) and don't have a Cognito-direct password. `USER_SRP_AUTH` will fail for those users with `UserNotFoundException`. The current implementation only supports email+password Cognito accounts.
- **`tubeType=1` is the default**, server-rendered per tenant in the page's inline JS (`var tubeType = (('1') === '2') ? 2 : 1;`). Override with `--tube-type` if needed.
- **SignalR response shape is best-effort.** WebSocket frames are not introspectable from HTTP captures, so `normalize_period_prices()` handles both list-of-dicts and dict-of-id→price shapes defensively. If a future schema change breaks this, that function is where to look.
- **Default subcommand routing.** Running `pks_prices.py` with no args (or only `fetch`'s flags like `--debug`) implicitly invokes `fetch`. The argv rewrite in `main()` is intentional — preserve it if refactoring the parser.
