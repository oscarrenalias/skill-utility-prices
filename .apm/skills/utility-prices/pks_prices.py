"""Fetch PKS Live price-fixing data and print to stdout.

Requires PKS_USERNAME and PKS_PASSWORD env vars (or --username/--password CLI args).
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sqlite3
import sys
import time
import urllib.parse

import requests
import websocket
from dotenv import load_dotenv
from pycognito import Cognito


COGNITO_USERPOOL_ID = "eu-west-1_s5PJjscpB"
COGNITO_CLIENT_ID = "4nre7e2dhlbbmmvh0egegjg4d6"
COGNITO_REGION = "eu-west-1"
GRAPHQL_URL = "https://graphql.akamon.cloud/"
LIVE_BASE = "https://live.pks.fi"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

MAGIC_LINK_QUERY = (
    "query priima_live_externalMagicLink($tenantId: String!, $baseUrl: String!) {"
    "  auth { externalMagicLink(tenantId: $tenantId, baseUrl: $baseUrl) { url } }"
    "}"
)

MULTI_TYPE_LABELS = {
    0: "Vuoden 2026 loppuun",
    1: "Vuodeksi eteenpäin",
    2: "Vuodelle 2027",
    3: "Tämä loppupuoli",
}

DB_PATH = pathlib.Path(__file__).resolve().parent / "pks_prices.db"

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS period_prices (
    fetched_at         TEXT    NOT NULL,
    period_id          INTEGER NOT NULL,
    period_name        TEXT,
    instrument_name    TEXT,
    period_type        INTEGER,
    season             TEXT,
    period_start       TEXT,
    period_stop        TEXT,
    price              REAL,
    price_with_vat     REAL
);
CREATE INDEX IF NOT EXISTS idx_period_prices_fetched_at ON period_prices(fetched_at);
CREATE INDEX IF NOT EXISTS idx_period_prices_period_id  ON period_prices(period_id);

CREATE TABLE IF NOT EXISTS multi_prices (
    fetched_at             TEXT    NOT NULL,
    contract_id            INTEGER,
    bundle_type            INTEGER,
    bundle_label           TEXT,
    period_start           TEXT,
    period_stop            TEXT,
    price                  REAL,
    price_with_vat         REAL,
    consumption_estimate   REAL,
    cost_estimate          REAL,
    cost_estimate_with_vat REAL
);
CREATE INDEX IF NOT EXISTS idx_multi_prices_fetched_at ON multi_prices(fetched_at);
"""


def log(msg, *, debug):
    if debug:
        print(msg, file=sys.stderr, flush=True)


def cognito_login(username, password):
    u = Cognito(
        COGNITO_USERPOOL_ID,
        COGNITO_CLIENT_ID,
        username=username,
        user_pool_region=COGNITO_REGION,
    )
    u.authenticate(password=password)
    return u.id_token


def get_magic_link_url(id_token):
    payload = {
        "operationName": "priima_live_externalMagicLink",
        "query": MAGIC_LINK_QUERY,
        "variables": {
            "tenantId": "PKS",
            "baseUrl": f"{LIVE_BASE}/Kirjaudu/PKS/Online",
        },
    }
    headers = {
        "authorization": id_token,
        "content-type": "application/json",
        "x-akamon-organisation": "PKS",
        "x-akamon-product-name": "SIERRA",
        "x-akamon-userpool-id": COGNITO_USERPOOL_ID,
        "origin": "https://pkslive.pks.fi",
        "referer": "https://pkslive.pks.fi/",
        "user-agent": USER_AGENT,
    }
    r = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]["auth"]["externalMagicLink"]["url"]


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def redeem_magic_link(session, magic_url):
    r = session.get(magic_url, timeout=30, allow_redirects=True)
    r.raise_for_status()


def ts():
    return int(time.time() * 1000)


def fetch_periods_page_user_id(session):
    r = session.get(f"{LIVE_BASE}/Hinnankiinnitys/Jaksot", timeout=30)
    r.raise_for_status()
    m = re.search(r"InitList\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*(\d+)\s*,\s*([\d.]+)", r.text)
    if not m:
        raise RuntimeError("Could not extract userId from periods page")
    return int(m.group(1)), float(m.group(2))


def fetch_periods_map(session):
    r = session.get(f"{LIVE_BASE}/Api/Periods/Available", params={"ts": ts()}, timeout=30)
    r.raise_for_status()
    out = {}
    for p in r.json():
        out[p["Id"]] = p
        if p.get("Parent"):
            out[p["Parent"]["Id"]] = p["Parent"]
    return out


def fetch_multi_prices(session, user_id, use_vat):
    r = session.get(
        f"{LIVE_BASE}/Api/MultiTransaction/PricesV2/{user_id}/{'true' if use_vat else 'false'}",
        params={"ts": ts()},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def fetch_period_prices_via_signalr(session, tube_type=1, debug=False):
    cd = json.dumps([{"name": "pricehub"}], separators=(",", ":"))

    log("  signalr/negotiate…", debug=debug)
    r = session.get(
        f"{LIVE_BASE}/signalr/negotiate",
        params={"clientProtocol": "1.5", "connectionData": cd, "_": ts()},
        timeout=30,
    )
    r.raise_for_status()
    nego = r.json()
    connection_token = nego["ConnectionToken"]

    cookie_header = "; ".join(f"{c.name}={c.value}" for c in session.cookies)

    ws_url = "wss://live.pks.fi/signalr/connect?" + urllib.parse.urlencode(
        {
            "transport": "webSockets",
            "clientProtocol": "1.5",
            "connectionToken": connection_token,
            "connectionData": cd,
            "tid": ts() % 11,
        }
    )
    log("  ws connect…", debug=debug)
    ws = websocket.create_connection(
        ws_url,
        header=[
            f"Cookie: {cookie_header}",
            f"User-Agent: {USER_AGENT}",
            "Origin: https://live.pks.fi",
        ],
        timeout=30,
    )

    try:
        ws.settimeout(10)
        try:
            init = ws.recv()
            log(f"  ws init frame: {init[:120]}", debug=debug)
        except Exception:
            pass

        log("  signalr/start…", debug=debug)
        r = session.get(
            f"{LIVE_BASE}/signalr/start",
            params={
                "transport": "webSockets",
                "clientProtocol": "1.5",
                "connectionToken": connection_token,
                "connectionData": cd,
                "_": ts(),
            },
            timeout=30,
        )
        r.raise_for_status()

        invocation_id = "0"
        ws.send(
            json.dumps(
                {"H": "priceHub", "M": "getNewestPrices", "A": [tube_type], "I": invocation_id}
            )
        )
        log("  invoked getNewestPrices, waiting for response…", debug=debug)

        deadline = time.time() + 30
        while time.time() < deadline:
            ws.settimeout(max(1.0, deadline - time.time()))
            try:
                frame = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not frame:
                continue
            try:
                msg = json.loads(frame)
            except json.JSONDecodeError:
                continue
            if msg.get("I") == invocation_id:
                if "E" in msg:
                    raise RuntimeError(f"priceHub.getNewestPrices error: {msg['E']}")
                return msg.get("R")
        raise TimeoutError("Timed out waiting for priceHub.getNewestPrices result")
    finally:
        try:
            ws.close()
        except Exception:
            pass
        try:
            session.get(
                f"{LIVE_BASE}/signalr/abort",
                params={
                    "transport": "webSockets",
                    "clientProtocol": "1.5",
                    "connectionToken": connection_token,
                    "connectionData": cd,
                },
                timeout=10,
            )
        except Exception:
            pass


def open_db(path=DB_PATH):
    conn = sqlite3.connect(str(path))
    conn.executescript(DB_SCHEMA)
    return conn


def save_to_db(periods, period_prices, multi, fetched_at, db_path=DB_PATH):
    period_rows = []
    for entry in normalize_period_prices(period_prices):
        pid = entry.get("PeriodId") or entry.get("Id") or entry.get("periodId")
        if pid is None:
            continue
        p = periods.get(pid, {})
        period_rows.append(
            (
                fetched_at,
                pid,
                p.get("Name"),
                p.get("InstrumentName"),
                p.get("PeriodType"),
                p.get("Season"),
                p.get("Start"),
                p.get("Stop"),
                entry.get("Price") or entry.get("price"),
                entry.get("PriceWithVat") or entry.get("priceWithVat"),
            )
        )

    multi_rows = []
    for cp in (multi or {}).get("ContractPrices", []) or []:
        contract_id = cp.get("ContractId")
        for bundle in cp.get("Prices", []) or []:
            btype = bundle.get("MultiTransactionV2ContractPriceType")
            multi_rows.append(
                (
                    fetched_at,
                    contract_id,
                    btype,
                    MULTI_TYPE_LABELS.get(btype, "Muu"),
                    bundle.get("Start"),
                    bundle.get("Stop"),
                    bundle.get("Price"),
                    bundle.get("PriceWithVat"),
                    bundle.get("ConsumptionEstimate"),
                    bundle.get("CostEstimate"),
                    bundle.get("CostEstimateWithVat"),
                )
            )

    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT INTO period_prices "
            "(fetched_at, period_id, period_name, instrument_name, period_type, season, "
            " period_start, period_stop, price, price_with_vat) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            period_rows,
        )
        conn.executemany(
            "INSERT INTO multi_prices "
            "(fetched_at, contract_id, bundle_type, bundle_label, period_start, period_stop, "
            " price, price_with_vat, consumption_estimate, cost_estimate, cost_estimate_with_vat) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            multi_rows,
        )
    return len(period_rows), len(multi_rows)


def fmt_price(v):
    if v is None:
        return "    -   "
    return f"{v:>7.3f}"


def normalize_period_prices(raw):
    """priceHub.getNewestPrices may return a list or a dict — normalize to list of dicts."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = []
        for pid, val in raw.items():
            if isinstance(val, dict):
                d = dict(val)
                d.setdefault("PeriodId", int(pid))
                items.append(d)
            else:
                items.append({"PeriodId": int(pid), "Price": val})
        return items
    return list(raw)


def print_report(periods, period_prices, multi):
    monthly = []
    quarterly = []
    other = []
    for entry in normalize_period_prices(period_prices):
        pid = entry.get("PeriodId") or entry.get("Id") or entry.get("periodId")
        if pid is None:
            continue
        period = periods.get(pid, {})
        name = period.get("Name") or f"Period {pid}"
        instrument = period.get("InstrumentName", "")
        period_type = period.get("PeriodType")
        price = entry.get("Price") or entry.get("price")
        price_vat = entry.get("PriceWithVat") or entry.get("priceWithVat")
        row = (name, instrument, price, price_vat, pid)
        if period_type == 0:
            monthly.append(row)
        elif period_type == 1:
            quarterly.append(row)
        else:
            other.append(row)

    monthly.sort(key=lambda r: (periods.get(r[4], {}).get("Start", ""), r[1]))
    quarterly.sort(key=lambda r: (periods.get(r[4], {}).get("Start", ""), r[1]))

    print("=" * 78)
    print(f"PKS Live — Hintakiinnitykset    {time.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 78)

    def section(title, rows):
        if not rows:
            return
        print()
        print(title)
        print("-" * 78)
        print(f"  {'Jakso':<20} {'Instrumentti':<14} {'snt/kWh':>9}  {'sis. ALV':>9}")
        for name, inst, price, vat, _ in rows:
            print(f"  {name:<20} {inst:<14} {fmt_price(price)}  {fmt_price(vat)}")

    section("Kuukausittaiset kiinnitysjaksot", monthly)
    section("Vuosineljänneksien kiinnitysjaksot", quarterly)
    if other:
        section("Muut jaksot", other)

    print()
    print("Pidemmät kiinnitysjaksot")
    print("-" * 78)
    print(
        f"  {'Tyyppi':<22} {'Aikaväli':<25} {'snt/kWh':>9}  {'sis. ALV':>9}"
    )
    for cp in (multi or {}).get("ContractPrices", []) or []:
        for p in cp.get("Prices", []) or []:
            label = MULTI_TYPE_LABELS.get(p.get("MultiTransactionV2ContractPriceType"), "Muu")
            start = (p.get("Start") or "")[:10]
            stop = (p.get("Stop") or "")[:10]
            window = f"{start} → {stop}"
            print(
                f"  {label:<22} {window:<25} "
                f"{fmt_price(p.get('Price'))}  {fmt_price(p.get('PriceWithVat'))}"
            )
    print()


SCHEMA_HINTS = {
    "description": (
        "PKS Live price-fixing data. Each `pks_prices.py fetch` run inserts one snapshot "
        "across both tables, tagged with a shared `fetched_at` UTC ISO-8601 timestamp."
    ),
    "tables": {
        "period_prices": (
            "One row per period (monthly or quarterly) per snapshot. "
            "period_type 0=monthly, 1=quarterly. price/price_with_vat are snt/kWh."
        ),
        "multi_prices": (
            "One row per multi-period bundle per snapshot. "
            "bundle_type: 0=year-end bundle, 1=next-12-months, 2=next-year, 3=second-half-of-year."
        ),
    },
    "common_queries": {
        "latest_snapshot": (
            "SELECT * FROM period_prices "
            "WHERE fetched_at = (SELECT MAX(fetched_at) FROM period_prices)"
        ),
        "price_history_for_instrument": (
            "SELECT fetched_at, price, price_with_vat FROM period_prices "
            "WHERE instrument_name = ? ORDER BY fetched_at"
        ),
        "daily_change_per_period": (
            "SELECT fetched_at, period_name, price, "
            "price - LAG(price) OVER (PARTITION BY period_id ORDER BY fetched_at) AS delta "
            "FROM period_prices ORDER BY fetched_at DESC, period_id"
        ),
    },
}


def open_db_readonly(path):
    uri = f"file:{pathlib.Path(path).resolve()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def cmd_describe(args):
    if not pathlib.Path(args.db).exists():
        out = {"db_path": str(args.db), "exists": False, **SCHEMA_HINTS,
               "schema_sql": DB_SCHEMA.strip()}
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    info = {"db_path": str(pathlib.Path(args.db).resolve()), "exists": True, "tables": {}}
    with open_db_readonly(args.db) as conn:
        conn.row_factory = sqlite3.Row
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )]
        for t in tables:
            cols = [
                {
                    "name": r["name"],
                    "type": r["type"],
                    "nullable": not r["notnull"],
                    "primary_key": bool(r["pk"]),
                }
                for r in conn.execute(f"PRAGMA table_info({t})")
            ]
            indexes = [
                {"name": r["name"], "unique": bool(r["unique"])}
                for r in conn.execute(f"PRAGMA index_list({t})")
            ]
            row_count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            mn, mx, runs = conn.execute(
                f"SELECT MIN(fetched_at), MAX(fetched_at), COUNT(DISTINCT fetched_at) FROM {t}"
            ).fetchone()
            sample = None
            r = conn.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 1").fetchone()
            if r is not None:
                sample = {k: r[k] for k in r.keys()}
            info["tables"][t] = {
                "description": SCHEMA_HINTS["tables"].get(t),
                "columns": cols,
                "indexes": indexes,
                "row_count": row_count,
                "earliest_fetched_at": mn,
                "latest_fetched_at": mx,
                "snapshot_count": runs,
                "latest_row": sample,
            }

    info["description"] = SCHEMA_HINTS["description"]
    info["common_queries"] = SCHEMA_HINTS["common_queries"]
    info["schema_sql"] = DB_SCHEMA.strip()
    print(json.dumps(info, indent=2, ensure_ascii=False))


def cmd_query(args):
    sql = args.sql.strip()
    if not sql:
        print(json.dumps({"ok": False, "error": "empty_sql", "message": "Empty SQL."}))
        sys.exit(1)
    if not pathlib.Path(args.db).exists():
        print(json.dumps({"ok": False, "error": "db_missing", "message": f"No DB at {args.db} — run `fetch` first."}))
        sys.exit(1)
    try:
        with open_db_readonly(args.db) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, args.params)
            rows = [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
    except sqlite3.OperationalError as e:
        print(json.dumps({"ok": False, "error": "sql_error", "message": str(e)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(
        {"ok": True, "row_count": len(rows), "rows": rows},
        indent=2, ensure_ascii=False, default=str,
    ))


def cmd_runs(args):
    with open_db_readonly(args.db) as conn:
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            "SELECT fetched_at, "
            "       (SELECT COUNT(*) FROM period_prices p WHERE p.fetched_at = a.fetched_at) AS period_rows, "
            "       (SELECT COUNT(*) FROM multi_prices  m WHERE m.fetched_at = a.fetched_at) AS multi_rows "
            "FROM (SELECT DISTINCT fetched_at FROM period_prices "
            "      UNION SELECT DISTINCT fetched_at FROM multi_prices) a "
            "ORDER BY fetched_at DESC"
        ))
    out = [{"fetched_at": r["fetched_at"],
            "period_rows": r["period_rows"],
            "multi_rows": r["multi_rows"]} for r in rows]
    print(json.dumps({"run_count": len(out), "runs": out}, indent=2, ensure_ascii=False))


def build_snapshot_payload(periods, period_prices, multi, fetched_at, db_path, saved):
    period_rows = []
    for entry in normalize_period_prices(period_prices):
        pid = entry.get("PeriodId") or entry.get("Id") or entry.get("periodId")
        if pid is None:
            continue
        p = periods.get(pid, {})
        period_rows.append({
            "period_id": pid,
            "period_name": p.get("Name"),
            "instrument_name": p.get("InstrumentName"),
            "period_type": p.get("PeriodType"),
            "season": p.get("Season"),
            "start": p.get("Start"),
            "stop": p.get("Stop"),
            "price": entry.get("Price") or entry.get("price"),
            "price_with_vat": entry.get("PriceWithVat") or entry.get("priceWithVat"),
        })

    multi_rows = []
    for cp in (multi or {}).get("ContractPrices", []) or []:
        for bundle in cp.get("Prices", []) or []:
            btype = bundle.get("MultiTransactionV2ContractPriceType")
            multi_rows.append({
                "contract_id": cp.get("ContractId"),
                "bundle_type": btype,
                "bundle_label": MULTI_TYPE_LABELS.get(btype, "Muu"),
                "start": bundle.get("Start"),
                "stop": bundle.get("Stop"),
                "price": bundle.get("Price"),
                "price_with_vat": bundle.get("PriceWithVat"),
                "consumption_estimate": bundle.get("ConsumptionEstimate"),
                "cost_estimate": bundle.get("CostEstimate"),
                "cost_estimate_with_vat": bundle.get("CostEstimateWithVat"),
            })

    return {
        "fetched_at": fetched_at,
        "saved_to_db": saved,
        "db_path": str(db_path) if saved else None,
        "periods": period_rows,
        "multi": multi_rows,
    }


def cmd_fetch(args):
    json_mode = getattr(args, "json", False)

    def fail(kind, message):
        if json_mode:
            print(json.dumps({"ok": False, "error": kind, "message": message}, ensure_ascii=False))
        else:
            print(f"ERROR ({kind}): {message}", file=sys.stderr)
        sys.exit(1)

    if not args.username or not args.password:
        fail(
            "missing_credentials",
            "Set PKS_USERNAME and PKS_PASSWORD in .env or pass --username/--password.",
        )

    try:
        log("[1/6] Cognito SRP login…", debug=args.debug)
        id_token = cognito_login(args.username, args.password)

        log("[2/6] Fetching magic link from GraphQL…", debug=args.debug)
        magic_url = get_magic_link_url(id_token)

        log("[3/6] Redeeming magic link at live.pks.fi…", debug=args.debug)
        session = make_session()
        redeem_magic_link(session, magic_url)

        log("[4/6] Reading periods page (userId, vatRate)…", debug=args.debug)
        user_id, vat_rate = fetch_periods_page_user_id(session)
        log(f"        userId={user_id} vatRate={vat_rate}", debug=args.debug)

        log("[5/6] Fetching period definitions and multi-period prices…", debug=args.debug)
        periods = fetch_periods_map(session)
        multi = fetch_multi_prices(session, user_id, use_vat=True)

        log("[6/6] Fetching period prices over SignalR…", debug=args.debug)
        period_prices = fetch_period_prices_via_signalr(
            session, tube_type=args.tube_type, debug=args.debug
        )
    except Exception as e:
        kind = type(e).__name__
        if "Cognito" in kind or "InitiateAuth" in str(e) or "UserNotFound" in kind:
            fail("auth_failed", f"{kind}: {e}")
        fail("fetch_failed", f"{kind}: {e}")

    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    saved = False
    if not args.no_save:
        try:
            n_periods, n_multi = save_to_db(
                periods, period_prices, multi, fetched_at, db_path=pathlib.Path(args.db)
            )
            saved = True
            log(
                f"      saved {n_periods} period rows + {n_multi} multi rows to {args.db}",
                debug=args.debug,
            )
        except Exception as e:
            if json_mode:
                fail("db_write_failed", f"{type(e).__name__}: {e}")
            raise

    if json_mode:
        payload = build_snapshot_payload(
            periods, period_prices, multi, fetched_at, args.db, saved
        )
        payload["ok"] = True
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(periods, period_prices, multi)


def build_parser():
    ap = argparse.ArgumentParser(
        description="PKS Live price fetcher and SQLite history store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Subcommands:\n"
            "  fetch     (default) Log in, fetch current prices, persist, print report\n"
            "  describe  Emit DB schema as JSON (for agents)\n"
            "  query     Run a read-only SQL query, output JSON rows\n"
            "  runs      List historical snapshots and their row counts\n"
        ),
    )
    ap.add_argument("--db", default=os.environ.get("PKS_DB_PATH", str(DB_PATH)),
                    help="SQLite path (default: %(default)s)")
    sub = ap.add_subparsers(dest="cmd")

    ap_fetch = sub.add_parser("fetch", help="Fetch current prices and persist to DB")
    ap_fetch.add_argument("--username", default=os.environ.get("PKS_USERNAME"))
    ap_fetch.add_argument("--password", default=os.environ.get("PKS_PASSWORD"))
    ap_fetch.add_argument("--tube-type", type=int,
                          default=int(os.environ.get("PKS_TUBE_TYPE", "1")))
    ap_fetch.add_argument("--no-save", action="store_true", help="Skip writing to SQLite")
    ap_fetch.add_argument("--json", action="store_true",
                          help="Emit a structured JSON snapshot instead of the human report")
    ap_fetch.add_argument("--debug", action="store_true")

    sub.add_parser("describe", help="Emit DB schema as JSON")

    ap_query = sub.add_parser("query", help="Run a read-only SQL query against the DB")
    ap_query.add_argument("sql", help="SQL string (use ? placeholders for params)")
    ap_query.add_argument("params", nargs="*", help="Positional bindings for ? placeholders")

    sub.add_parser("runs", help="List all historical snapshots")

    return ap


def main():
    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent / ".env")
    ap = build_parser()

    argv = sys.argv[1:]
    known_subs = {"fetch", "describe", "query", "runs"}
    help_flags = {"-h", "--help"}
    has_sub = any(a in known_subs for a in argv)
    has_help = any(a in help_flags for a in argv)
    if not has_sub and not has_help:
        argv = ["fetch", *argv]

    args = ap.parse_args(argv)
    cmd = args.cmd or "fetch"
    {"fetch": cmd_fetch, "describe": cmd_describe, "query": cmd_query, "runs": cmd_runs}[cmd](args)


if __name__ == "__main__":
    main()
