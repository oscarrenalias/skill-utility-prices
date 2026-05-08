---
name: utility-prices
description: Use when the user asks about PKS Live electricity contract price-fixings ("hintakiinnitys", "kiinnitykset", "Priima Live"), wants the current snt/kWh prices for monthly/quarterly periods or multi-period bundles, or wants to analyze how those prices have moved historically. Wraps `pks_prices.py` — fetches a fresh snapshot from PKS Live (auth + REST + SignalR) and persists it to a local SQLite history. All commands except `fetch` are read-only against that DB.
---

# utility-prices

A self-contained CLI skill that pulls electricity contract pricing from PKS Live and stores snapshots in a local SQLite DB. Everything needed (`pks_prices.py`, `pyproject.toml`, `uv.lock`, `.env.example`) lives in this skill folder. The DB and `.env` are created beside the script and are not part of the distributed package.

## Setup (first-time only)

The skill folder is a self-contained uv project:

```bash
cd <skill-folder>          # the directory containing this SKILL.md
cp .env.example .env       # then edit .env with PKS_USERNAME / PKS_PASSWORD
uv sync                    # install the locked dependency set into ./.venv
```

After that, every invocation goes through `uv run`, which auto-activates that venv.

## Invocation

The script reads `.env` from its own directory (not from cwd), and the SQLite DB is also placed beside the script. So commands work from any cwd as long as you give `uv run` the skill folder via `--directory`:

```bash
uv run --directory <skill-folder> python pks_prices.py <subcommand> [args…]
```

If you `cd` into the skill folder first, you can drop `--directory`:

```bash
cd <skill-folder>
uv run python pks_prices.py <subcommand> [args…]
```

The examples below use the short form; substitute as needed.

## Subcommands

| Command | Output | Read/write | Notes |
|---|---|---|---|
| `fetch` | Human report (default) or JSON (`--json`) | Writes a snapshot to DB unless `--no-save` | Network-bound (~3–5 s). Default subcommand. |
| `describe` | JSON: tables, columns, indexes, row counts, latest sample row, common query templates, full schema SQL | Read-only | First call when an agent has no prior knowledge of the DB. |
| `query "<SQL>" [params…]` | JSON: `{ok, row_count, rows[]}` | Read-only (DB opened with `mode=ro`; writes fail) | Use `?` placeholders + positional `params`. |
| `runs` | JSON: every distinct `fetched_at` and per-table row counts | Read-only | Use to discover how much history exists. |

## Rules

1. **Always run via `uv run`.** Example: `uv run python pks_prices.py describe` (or with `--directory <skill-folder>` from elsewhere). Never call `python pks_prices.py` directly — the venv won't be active and imports will fail. If `uv sync` hasn't been run, the first `uv run` will install dependencies.
2. **`fetch` requires credentials** in the skill folder's `.env` (created from `.env.example`) or via `--username`/`--password`. If the user is asking purely about historical data already in the DB, **do not call `fetch`** — go straight to `describe`/`query`. `fetch` is for getting a fresh snapshot.
3. **For programmatic use of `fetch`, always pass `--json`.** The default human report is Finnish-language with box-drawing characters and is not designed to be parsed.
4. **Never invent SQL columns.** Call `describe` first if you don't already know the schema. Column names and table semantics are returned in that JSON, including a `latest_row` per table so you can see real values.
5. **Output is JSON-on-stdout for every non-fetch command and for `fetch --json`.** Errors are also JSON: `{"ok": false, "error": "<kind>", "message": "..."}`. Exit code is non-zero on error. Stderr carries `--debug` progress lines (only printed when `--debug` is passed) — ignore unless diagnosing a failure.
6. **The DB is append-only.** Each run inserts a new snapshot — there is no upsert. Same-day re-runs produce multiple snapshots. Treat `fetched_at` as the snapshot key.

## Choosing what to do

- "What are the current prices?" → `fetch --json` (gets fresh data + persists).
- "What's the price of Q3-26 right now?" → `fetch --json` then read from response.
- "What's the trend for Q3-26 over the last week?" → `query` against `period_prices`. Don't `fetch` first unless the user explicitly asked for the latest.
- "How many snapshots do we have?" → `runs`.
- "What columns does the DB have?" → `describe`.

## Schema cheat-sheet (canonical source: `describe`)

```
period_prices(fetched_at, period_id, period_name, instrument_name, period_type,
              season, period_start, period_stop, price, price_with_vat)
  -- one row per period per snapshot
  -- period_type: 0 = monthly, 1 = quarterly
  -- price is snt/kWh excl. VAT; price_with_vat includes VAT (~25.5%)

multi_prices (fetched_at, contract_id, bundle_type, bundle_label,
              period_start, period_stop, price, price_with_vat,
              consumption_estimate, cost_estimate, cost_estimate_with_vat)
  -- one row per multi-period bundle per snapshot
  -- bundle_type: 0 = year-end bundle, 1 = next-12-months,
  --              2 = next-year, 3 = second-half-of-year
```

Both tables share `fetched_at` (UTC ISO-8601 with seconds, e.g. `2026-05-08T11:13:19+00:00`) — join on it to align period prices with bundle prices from the same run.

## Example invocations

```bash
# Discover the DB
uv run python pks_prices.py describe

# Fetch latest snapshot, structured output
uv run python pks_prices.py fetch --json

# Fetch without persisting (e.g. for one-off inspection)
uv run python pks_prices.py fetch --json --no-save

# Latest prices for all monthly periods
uv run python pks_prices.py query \
  "SELECT period_name, instrument_name, price, price_with_vat
   FROM period_prices
   WHERE fetched_at = (SELECT MAX(fetched_at) FROM period_prices)
     AND period_type = 0
   ORDER BY period_start"

# History of a specific instrument (parameterised)
uv run python pks_prices.py query \
  "SELECT fetched_at, price, price_with_vat
   FROM period_prices WHERE instrument_name = ? ORDER BY fetched_at" \
  "Q3-26"

# Day-over-day change per period (window function)
uv run python pks_prices.py query \
  "SELECT fetched_at, period_name, price,
          price - LAG(price) OVER (PARTITION BY period_id ORDER BY fetched_at) AS delta
   FROM period_prices ORDER BY fetched_at DESC, period_id"

# How many historical runs do we have?
uv run python pks_prices.py runs
```

## Failure modes worth recognising

| `error` field | What it means | What to do |
|---|---|---|
| `missing_credentials` | No `.env` and no `--username`/`--password` | Ask the user; don't guess. |
| `auth_failed` (`UserNotFoundException`) | Wrong user, **or** the account is federated (TELIA-IBS / Finnish bank ID). The current implementation only supports email+password Cognito accounts. | Tell the user; do not retry with different credentials. |
| `db_missing` | `query`/`describe` called but no DB exists yet | Run `fetch` once to create it. |
| `sql_error` | Bad SQL or write attempt against the read-only DB | Re-read `describe` output; `query` cannot mutate. |
| `fetch_failed` | Network, GraphQL, SignalR, or HTML-scrape failure mid-fetch | Re-run with `--debug` to see which step (`[1/6]…[6/6]`) failed. The 60-second magic-link token expiry is one likely cause for transient failures. |

If `fetch_failed` happens repeatedly, the issue may be that PKS changed the page markup or the SignalR hub. Re-run with `--debug` to see exactly which step in the `[1/6]…[6/6]` pipeline fails.
