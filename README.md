# skill-utility-prices

An agent skill that pulls two kinds of data from [PKS Live](https://pkslive.pks.fi) — electricity contract **price-fixings** ("hintakiinnitys") and **metered consumption / actuals** — and persists each fetch to a local SQLite database, so an agent can read current state and analyse how things have moved over time.

The purpose of the skill is to facilitate integration with an agent or personal assistant such as OpenClaw, for example to retrieve prices and consumption once a day, analyze historical data, and set alerts. While some of this functionality is provided by the official site, the convenience of integration with an agent is far superior.

Designed for one user with one PKS contract, running on a personal machine, ideally on a daily cron.

This skill has been fully vibe coded by Claude Code, which was provided with a trace of the browser network traffic (in HAR) format, and it figured out the rest.

## Skill

### utility-prices

Wraps a small Python CLI (`pks_prices.py`) with five subcommands:

| Subcommand | What it does |
|---|---|
| `prices fetch` | Logs in (Cognito SRP → GraphQL magic link → legacy session cookies), pulls multi-period bundle prices over REST and monthly/quarterly period prices over SignalR, **appends** a snapshot, and prints a Finnish-language report. With `--json`, returns structured data instead. |
| `actuals fetch` | Logs in (Cognito SRP only), discovers customer + metering points via GraphQL, fetches daily consumption for the requested date range (default: last 30 days), and **upserts** rows keyed by `(metering_point_id, contract_type, period_start)`. Captures kWh, EUR cost, day/night/winter-day breakdown, and per-day min/max/avg. With `--json`, returns structured data instead. |
| `describe` | Emits the SQLite schema as JSON — tables, columns, indexes, row counts, snapshot date range, sample rows, and ready-to-run query templates. The agent's discovery entry point. |
| `query "<SQL>" [params…]` | Runs a read-only SQL query (DB opened with `mode=ro`) and returns rows as JSON. Supports `?` placeholders for parameter binding. |
| `runs` | Lists every historical price-fixing snapshot with per-table row counts. Actuals are not snapshotted (they're upserted), so they don't appear here. |

All non-interactive subcommands emit JSON on stdout, with structured error envelopes (`{"ok": false, "error": "<kind>", "message": "..."}`) and non-zero exit codes on failure.

See [`.apm/skills/utility-prices/SKILL.md`](.apm/skills/utility-prices/SKILL.md) for the full agent-facing documentation.

## Installation

**With apm:**
```
apm install oscarrenalias/skill-utility-prices#vX.Y.Z
```

Replace `X.Y.Z` with the appropriate current version (see the [Releases](https://github.com/oscarrenalias/skill-utility-prices/releases) tab).

**Without apm:** download the zip from the [latest release](https://github.com/oscarrenalias/skill-utility-prices/releases) and extract it into your skills folder:

```
unzip skill-utility-prices-<version>.zip -d ~/.claude/skills/
```

Replace ```.claude``` with ```.agents``` if using Codex.

## Setup

The skill folder is a self-contained [uv](https://docs.astral.sh/uv/) project. Set it up once:

```
cd ~/.claude/skills/utility-prices
cp .env.example .env       # then edit .env with PKS_USERNAME and PKS_PASSWORD
uv sync
```

After that, the agent invokes the skill via `uv run python pks_prices.py …`. The `.env` file and the SQLite database (`pks_prices.db`) are created beside the script and stay local — they are never part of the distributed package.

## Limitations

- **Only email + password Cognito accounts are supported.** PKS users who authenticate via TELIA-IBS (Finnish bank ID / Mobiilivarmenne) do not have a Cognito-direct password and the SRP login will fail with `UserNotFoundException`. Driving the federated OIDC flow is not in scope.
- **Single tenant.** Constants like the Cognito user pool, GraphQL endpoint, and default tube type are hardcoded.
- **Reverse-engineered transport.** The legacy site (used by `prices fetch`) uses classic ASP.NET SignalR over WebSocket (clientProtocol 1.5) for live monthly/quarterly prices; the script implements the protocol directly. If the protocol or the page markup that exposes the user ID changes, the relevant step will need a fix. `actuals fetch` uses the modern GraphQL API and is more robust.
- **Daily granularity for actuals.** The `consumption_daily` table is daily-shaped. The GraphQL API supports finer resolutions (`PT1H`, `PT15M`) but only the daily aggregates are persisted today.
