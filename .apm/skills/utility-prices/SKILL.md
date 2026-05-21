---
name: utility-prices
description: Use when the user asks about their PKS Live electricity contract — price-fixings ("hintakiinnitys", "kiinnitykset", "Priima Live") or metered consumption / actuals ("kulutus", "sähkönkäyttö"). Wraps `pks_prices.py` — `prices fetch` pulls a fresh price-fixing snapshot via Cognito + REST + SignalR, `actuals fetch` pulls daily metered consumption via the GraphQL API. Both persist to a local SQLite history. `describe`/`query`/`runs` are read-only against that DB.
---

# utility-prices

A self-contained CLI skill that pulls two kinds of data from PKS Live and stores them in a local SQLite DB:

- **Price-fixings** (`prices fetch`) — current snt/kWh prices for monthly / quarterly periods and multi-period bundles. Append-only snapshots.
- **Metered consumption / actuals** (`actuals fetch`) — daily kWh and cost per metering point, with day/night/winter-day breakdown and min/max/avg per day. Upserted by `(metering_point_id, contract_type, period_start)` so re-fetching the same window refreshes any restated values.

Everything needed (`pks_prices.py`, `pyproject.toml`, `uv.lock`, `.env.example`) lives in this skill folder. The DB and `.env` are created beside the script and are not part of the distributed package.

## Setup (first-time only)

The skill folder is a self-contained uv project:

```bash
cd <skill-folder>          # the directory containing this SKILL.md
cp .env.example .env       # then edit .env with PKS_USERNAME / PKS_PASSWORD
uv sync                    # install the locked dependency set into ./.venv
```

After that, every invocation goes through `uv run`, which auto-activates that venv.

## Invocation

The script reads `.env` from its own directory (not from cwd), and the SQLite DB is also placed beside the script. Commands work from any cwd as long as you give `uv run` the skill folder via `--directory`:

```bash
uv run --directory <skill-folder> python pks_prices.py <command> [args…]
```

If you `cd` into the skill folder first, you can drop `--directory`. The examples below use the short form.

## Subcommands

| Command | Output | Read/write | Notes |
|---|---|---|---|
| `prices fetch` | Human report (default) or JSON (`--json`) | Appends a snapshot unless `--no-save` | Network-bound (~3–5 s). Cognito + magic-link + SignalR. |
| `actuals fetch` | Human report (default) or JSON (`--json`) | Upserts rows unless `--no-save` | Cognito + GraphQL only. The API only accepts one calendar month per call, so the script iterates month-by-month internally: ~1 s/month × meters. A 30-day range is ~2–4 s; a multi-year backfill takes seconds-per-month. |
| `describe` | JSON: tables, columns, indexes, row counts, latest sample row, common query templates, full schema SQL | Read-only | First call when an agent has no prior knowledge of the DB. |
| `query "<SQL>" [params…]` | JSON: `{ok, row_count, rows[]}` | Read-only (DB opened with `mode=ro`; writes fail) | Use `?` placeholders + positional `params`. |
| `runs` | JSON: every distinct price-fixing `fetched_at` and per-table row counts | Read-only | Lists price snapshots only — actuals are upserted, not snapshotted. |

## Rules

1. **Always run via `uv run`.** Example: `uv run python pks_prices.py describe` (or `--directory <skill-folder>` from elsewhere). Never call `python pks_prices.py` directly — the venv won't be active and imports will fail.
2. **`prices fetch` and `actuals fetch` both require credentials** in the skill folder's `.env` (created from `.env.example`) or via `--username`/`--password`. If the user is asking purely about data already in the DB, **do not call a fetch** — go straight to `describe`/`query`.
3. **For programmatic use of either fetch, always pass `--json`.** The default human report is Finnish-language with box-drawing characters and is not designed to be parsed.
4. **Never invent SQL columns.** Call `describe` first if you don't already know the schema. Column names, table semantics, and a `latest_row` per table are returned in that JSON.
5. **Output is JSON-on-stdout** for every non-fetch command and for `<x> fetch --json`. Errors are also JSON: `{"ok": false, "error": "<kind>", "message": "..."}`. Exit code is non-zero on error. Stderr carries `--debug` progress lines.
6. **Price tables are append-only, actuals are upserted.** `period_prices` / `multi_prices` get a new row per snapshot keyed by `fetched_at`. `consumption_daily` is keyed by `(metering_point_id, contract_type, period_start)` — re-fetching a window updates the existing row in place.
7. **Backfills are safe and idempotent.** `actuals fetch --from 2024-01-01` (or any multi-year range) works in a single command — the script breaks the range into calendar months under the hood and the upsert key means re-running the same range is a no-op against existing data. Use this when populating an empty DB or filling gaps. With `--debug`, expect one `MMMMM-MM…` line per month per meter.

## Initializing an empty consumption history

When the user is setting up the skill on a new machine, or when `consumption_daily` is empty (e.g. the DB was created by `prices fetch` first), run a backfill **before** answering any question that depends on consumption trends. Without it, queries like "how much did I use last winter?" return nothing.

**Detection.** The DB is empty for consumption when either:
- `describe` reports `consumption_daily.row_count == 0`, or
- `query "SELECT COUNT(*) FROM consumption_daily"` returns 0 (or `db_missing`).

**Backfill recipe.** Pull at least the two most recent full calendar years plus year-to-date. With today = `YYYY-MM-DD`, the start date is January 1 of `YYYY - 2`. Example for today = 2026-05-21:

```bash
uv run python pks_prices.py actuals fetch \
  --from 2024-01-01 --to 2026-05-21 --json
```

That single call covers 2024, 2025, and 2026 year-to-date. Internally the script splits it into one GraphQL call per calendar month per metering point; for a household with two meters (consumption + production feed-in) and a ~28-month range, expect ~30–60 seconds and ~50–60 GraphQL calls. The upsert key means re-running the same range is a safe no-op against existing rows, so the agent can re-issue the command if it's unsure whether it completed.

**After backfilling**, confirm with one query and tell the user what you loaded:

```bash
uv run python pks_prices.py query \
  "SELECT metering_point_id, contract_type, COUNT(*) AS days,
          MIN(period_start) AS earliest, MAX(period_start) AS latest,
          ROUND(SUM(sum_kwh), 1) AS total_kwh,
          ROUND(SUM(cost_with_vat), 2) AS total_eur_vat
   FROM consumption_daily
   GROUP BY metering_point_id, contract_type"
```

For routine top-ups after the initial backfill, prefer the small-window form (`actuals fetch --days 7 --json` or similar) — the upsert key still handles overlap cleanly, but pulling only the recent window keeps each call fast.

## Lock-in price vs. last year's actuals (the primary use case)

**This is the headline question this skill exists to answer.** For a given upcoming locking period (a monthly or quarterly tariff offer in `period_prices`), surface — for the user, not as a recommendation — the four numbers needed to decide whether to lock:

1. The current lock-in price (snt/kWh, incl. VAT).
2. The user's actual consumption for the same calendar period one year ago (kWh).
3. The euro cost the user actually paid for that period last year (€, incl. VAT).
4. The hypothetical euro cost if that historical consumption had been billed at the current lock-in price, and the delta vs. (3).

The agent's job is to compute these and present them. Do **not** say "you should lock" or "you shouldn't lock" — the user decides. Surface caveats (see below) instead.

### Prerequisites

- `period_prices` must have a recent snapshot. If `runs` shows no snapshot from today, call `prices fetch --json` first.
- `consumption_daily` must cover the same calendar period one year ago. If it doesn't (run `describe` and look at `earliest`), trigger the empty-DB backfill above before querying.

### The canonical query

For a single period identified by `instrument_name` (e.g. `"Q3-26"`, `"Q4-26"`, `"Kesä-26"`, `"Heinä-26"` — quarter or month, the column is the same):

```sql
WITH lock AS (
  SELECT instrument_name, period_start, period_stop, price_with_vat
  FROM period_prices
  WHERE instrument_name = ?
  ORDER BY fetched_at DESC
  LIMIT 1
),
historic AS (
  SELECT
    SUM(sum_kwh)       AS kwh,
    SUM(cost_with_vat) AS eur_actual_with_vat,
    COUNT(*)           AS days
  FROM consumption_daily, lock
  WHERE contract_type = 'Sales'
    AND consumption_daily.period_start >= datetime(lock.period_start, '-1 year')
    AND consumption_daily.period_start <= datetime(lock.period_stop,  '-1 year')
)
SELECT
  lock.instrument_name,
  date(lock.period_start, '+3 hours') AS lock_starts,
  date(lock.period_stop)              AS lock_ends,
  lock.price_with_vat                 AS lock_snt_kwh_with_vat,
  ROUND(historic.kwh, 1)              AS last_year_kwh,
  ROUND(historic.eur_actual_with_vat, 2) AS last_year_eur_actual,
  ROUND(historic.kwh * lock.price_with_vat / 100, 2) AS hypothetical_eur_at_lock,
  ROUND(historic.kwh * lock.price_with_vat / 100 - historic.eur_actual_with_vat, 2) AS delta_eur,
  ROUND(100.0 * (historic.kwh * lock.price_with_vat / 100 - historic.eur_actual_with_vat) / historic.eur_actual_with_vat, 1) AS delta_pct,
  historic.days AS days_of_data
FROM lock, historic;
```

Pass the period identifier as one positional `?` param. Example:

```bash
uv run python pks_prices.py query "<sql above>" "Q3-26"
```

This single query also appears as `common_queries.lock_in_vs_last_year_actuals` in `describe` output — fetch it from there to avoid copy-paste drift.

### How to interpret the result

- `delta_eur > 0` / `delta_pct > 0` → at this lock-in price, last year's quarter would have cost **more** than what was actually paid. Locking now means paying more *relative to last year*.
- `delta_eur < 0` → locking would have been cheaper than last year's actual bill for that quarter.
- `days_of_data` should equal the calendar length of the period (28–31 for a month, 90–92 for a quarter). Anything significantly less = incomplete history; warn the user that the comparison is partial.

### Presentation format (this is what the user wants to see)

When asked to analyse a lock-in offer, render the result as a small markdown table followed by a plain-language sentence and the caveats. Here is the canonical layout — adapt the numbers and period label, keep the structure:

```markdown
## Q4-26 (Oct 1 – Dec 31, 2026) — lock-in vs. last year

| | |
|---|---|
| Current lock-in price | **9.859 snt/kWh** (incl. VAT) |
| Last year's consumption (Q4-25) | **8,995 kWh** over 92 days (full quarter ✓) |
| Last year's actual bill for Q4-25 | **€641.52** (incl. VAT) |
| Hypothetical cost at the lock-in price | **€886.82** |
| **Delta** | **+€245.30** (**+38.2%**) |

In plain terms: if Q4-26 consumption matches last year's pattern, locking now
at 9.859 snt/kWh costs about **€245 more** than what was actually paid for the
same quarter last year.

[then the four caveats, as bullets]
```

Rules for the table:
- Title line names the period in both forms — instrument code (`Q4-26`) and human-readable date range. For monthly offers use the Finnish month name + year (e.g. `Kesä-26 (June 2026)`).
- Five rows in this order: current lock-in price, last-year consumption (with day count and a ✓ if `days_of_data` equals the calendar length, or a ⚠ if it doesn't), last-year actual bill, hypothetical at lock-in, delta.
- Numbers bolded inside cells (`**€641.52**`), units always included.
- The delta row uses both absolute euros and percent.
- Sign and direction explicit in the plain-language line: "costs about X more" or "saves about X" — don't make the user re-derive it from the delta sign.

After the table, **always** include the four caveats from "Caveats to surface to the user" below — verbatim or paraphrased, but all four. Do **not** add "you should lock" / "you shouldn't lock". The user decides.

If the user asks about multiple periods at once (e.g. "all upcoming quarters"), use one section per period rather than one wide table — easier to read and each period's caveats stay attached to its numbers.

### Listing all lock-in periods on offer

To run the analysis across every offer in a single shot, first discover them:

```sql
SELECT instrument_name, period_type, price_with_vat,
       date(period_start, '+3 hours') AS starts,
       date(period_stop)              AS ends
FROM period_prices
WHERE fetched_at = (SELECT MAX(fetched_at) FROM period_prices)
  AND price IS NOT NULL
ORDER BY period_start;
```

Then loop the canonical query above for each `instrument_name`.

### Caveats to surface to the user

- **Consumption shifts year over year** (weather, EV charging, heat-pump runtime, occupancy). The historical kWh is a baseline, not a forecast. Mention this whenever the result is close to break-even.
- **The lock-in covers only the per-kWh energy charge.** Monthly basic fees, grid-transmission charges, and electricity tax are billed separately and are not in either side of the comparison.
- **The `Sales` meter only.** Production feed-in (`SalesProduction`) shouldn't enter this calculation — that meter records energy sold *back* to the grid, not bought.
- **Last year's actual cost reflects whatever pricing was in place then** — possibly a previous lock-in, possibly spot. The comparison is "this lock-in vs. last year's bill", not "this lock-in vs. last year's spot market".

### Daily-cron pattern

Once both DB sides are populated, a cron-friendly daily run looks like:

1. `prices fetch --json` — refresh today's lock-in offers.
2. `actuals fetch --days 7 --json` — keep the trailing week of actuals current (upserts overlap cleanly).
3. Discover periods (the listing query above), then run the canonical query for each one.
4. Surface a single small table to the user — one row per offer, with the four data points and the delta.

## Choosing what to do

- "What are the current prices?" → `prices fetch --json` (gets fresh data + persists).
- "What's the price of Q3-26 right now?" → `prices fetch --json` then read from response.
- "What's the trend for Q3-26 over the last week?" → `query` against `period_prices`. Don't fetch unless the user explicitly asked for the latest.
- "How much did we use yesterday / last week / last month?" → `actuals fetch --json --from … --to …` (refreshes the window).
- "Has my consumption changed compared to last year?" → `query` against `consumption_daily`. Run `actuals fetch` first only if the user wants the latest day refreshed.
- "How many price snapshots do we have?" → `runs`.
- "What columns does the DB have?" → `describe`.

## Schema cheat-sheet (canonical source: `describe`)

```
period_prices(fetched_at, period_id, period_name, instrument_name, period_type,
              season, period_start, period_stop, price, price_with_vat)
  -- one row per period per price-fixing snapshot
  -- period_type: 0 = monthly, 1 = quarterly
  -- price is snt/kWh excl. VAT; price_with_vat includes VAT (~25.5%)

multi_prices (fetched_at, contract_id, bundle_type, bundle_label,
              period_start, period_stop, price, price_with_vat,
              consumption_estimate, cost_estimate, cost_estimate_with_vat)
  -- one row per multi-period bundle per price-fixing snapshot
  -- bundle_type: 0 = year-end, 1 = next-12-months, 2 = next-year, 3 = second-half

meter_points(metering_point_id PK, customer_id, gsrn_identifier, type, status,
             address, first_seen_at, last_seen_at)
  -- one row per metering point ever observed for this customer
  -- type: 'Sales' (consumption) or 'SalesProduction' (feed-in / production)

consumption_daily(metering_point_id, contract_type, period_start, period_end,
                  sum_kwh, cost_with_vat, cost_without_vat, unit, status,
                  day_time_kwh, night_time_kwh, winter_day_kwh,
                  min_value, min_value_time, max_value, max_value_time,
                  avg_value, value_count, fetched_at)
  -- PK (metering_point_id, contract_type, period_start)
  -- sum_kwh is the daily total in kWh; day_time_kwh / night_time_kwh /
  --   winter_day_kwh break it down by tariff time-of-day
  -- cost_with_vat / cost_without_vat are in EUR for the day
  -- min_value / max_value / avg_value are kW (instantaneous) within the day;
  --   value_count is the number of sub-readings rolled up (96 = 15-min for a full day)
  -- period_start is the Helsinki day boundary expressed in UTC:
  --   2026-05-01T21:00:00Z  =  Helsinki midnight, summer (EEST = UTC+3)
  --   2026-12-01T22:00:00Z  =  Helsinki midnight, winter (EET  = UTC+2)
```

Both price tables share `fetched_at` (UTC ISO-8601 with seconds, e.g. `2026-05-08T11:13:19+00:00`) — join on it to align period prices with bundle prices from the same run.

## `actuals fetch` flags

| Flag | Default | Notes |
|---|---|---|
| `--from YYYY-MM-DD` | (none — see `--days`) | Start date, Helsinki calendar day. |
| `--to YYYY-MM-DD` | today | End date, Helsinki calendar day, inclusive. |
| `--days N` | 30 | If `--from` is not given, fetch N days back from `--to`. Ignored if `--from` set. |
| `--resolution` | `P1DT` | ISO-8601 duration. The DB schema is daily-shaped; `PT1H` / `PT15M` are accepted by the API but the daily aggregates from `range.items` are what get persisted. |
| `--metering-point ID` | all | Limit to one metering point. By default both Sales and SalesProduction are fetched. |
| `--product-identifier` | `Priima` | Override only if the user has a non-Priima product. |
| `--no-save` | off | Skip writing to the DB (one-off inspection). |
| `--json` / `--debug` | off | Structured output / stderr progress. |

## Example invocations

```bash
# Discover the DB
uv run python pks_prices.py describe

# --- Prices ---

# Fetch latest price snapshot, structured output
uv run python pks_prices.py prices fetch --json

# Fetch without persisting (one-off inspection)
uv run python pks_prices.py prices fetch --json --no-save

# How many historical price snapshots do we have?
uv run python pks_prices.py runs

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

# --- Actuals ---

# Daily consumption for the last 30 days (default range)
uv run python pks_prices.py actuals fetch --json

# Specific month
uv run python pks_prices.py actuals fetch --from 2026-04-01 --to 2026-04-30 --json

# Last 7 days only
uv run python pks_prices.py actuals fetch --days 7 --json

# Multi-year backfill into an empty DB (iterates calendar months under the hood)
uv run python pks_prices.py actuals fetch --from 2024-01-01 --to 2026-05-21 --json

# Total kWh & EUR consumed last 30 days, per meter
uv run python pks_prices.py query \
  "SELECT metering_point_id, contract_type,
          SUM(sum_kwh) AS kwh, SUM(cost_with_vat) AS eur_with_vat
   FROM consumption_daily
   WHERE period_start >= date('now', '-30 days')
   GROUP BY metering_point_id, contract_type"

# Day-vs-night share, last 30 days, consumption meter only
uv run python pks_prices.py query \
  "SELECT period_start, sum_kwh, day_time_kwh, night_time_kwh,
          ROUND(100.0 * night_time_kwh / sum_kwh, 1) AS night_pct
   FROM consumption_daily WHERE contract_type = 'Sales'
   ORDER BY period_start DESC LIMIT 30"
```

## Failure modes worth recognising

| `error` field | What it means | What to do |
|---|---|---|
| `missing_credentials` | No `.env` and no `--username`/`--password` | Ask the user; don't guess. |
| `auth_failed` (`UserNotFoundException`) | Wrong user, **or** the account is federated (TELIA-IBS / Finnish bank ID). The current implementation only supports email+password Cognito accounts. | Tell the user; do not retry with different credentials. |
| `bad_args` | E.g. `--from` after `--to`, or unknown metering point | Re-read the user's request; fix and retry. |
| `db_missing` | `query`/`describe` called but no DB exists yet | Run a fetch once to create it. |
| `sql_error` | Bad SQL or write attempt against the read-only DB | Re-read `describe` output; `query` cannot mutate. |
| `fetch_failed` | Network, GraphQL, SignalR, or HTML-scrape failure mid-fetch | Re-run with `--debug` to see which step failed. For `prices fetch` the 60-second magic-link token expiry is one likely cause for transient failures. |
| `db_write_failed` | Writing the snapshot/upsert failed | Run with `--debug`; the DB is auto-created beside the script so a missing directory shouldn't be the cause. |

If a fetch fails repeatedly, PKS may have changed the page markup, GraphQL schema, or the SignalR hub. Re-run with `--debug`:
- `prices fetch` prints `[1/6]…[6/6]` progress.
- `actuals fetch` prints `[1/4]…[4/4]` progress.
