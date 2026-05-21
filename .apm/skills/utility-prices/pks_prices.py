"""PKS Live data fetcher and local SQLite history store.

Top-level subcommands:
  prices    Fetch electricity price-fixing snapshots (Cognito + magic-link + SignalR).
  actuals   Fetch metered consumption history (Cognito + GraphQL only).
  describe  Emit DB schema and table summaries as JSON.
  query     Run a read-only SQL query against the DB.
  runs      List historical price-fixing snapshots.

Requires PKS_USERNAME and PKS_PASSWORD env vars (or --username/--password CLI args)
for `prices fetch` and `actuals fetch`.
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
from zoneinfo import ZoneInfo

import requests
import websocket
from dotenv import load_dotenv
from pycognito import Cognito


COGNITO_USERPOOL_ID = "eu-west-1_s5PJjscpB"
COGNITO_CLIENT_ID = "4nre7e2dhlbbmmvh0egegjg4d6"
COGNITO_REGION = "eu-west-1"
GRAPHQL_URL = "https://graphql.akamon.cloud/"
LIVE_BASE = "https://live.pks.fi"
TENANT_ID = "PKS"
HELSINKI_TZ = ZoneInfo("Europe/Helsinki")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

MAGIC_LINK_QUERY = (
    "query priima_live_externalMagicLink($tenantId: String!, $baseUrl: String!) {"
    "  auth { externalMagicLink(tenantId: $tenantId, baseUrl: $baseUrl) { url } }"
    "}"
)

CUSTOMERSHIPS_QUERY = """query customerships_context_Customerships {
  endUser {
    tenancies {
      tenantId
      customerIds
    }
  }
}"""

METERING_POINTS_QUERY = """query mp_select_CustomerMeteringPoints(
  $tenantId: ID!,
  $customerIdentifier: ID!,
  $includeFuture: Boolean,
  $includePast: Boolean
) {
  customersByIdentifier(
    tenantId: $tenantId
    customerIdentifier: $customerIdentifier
  ) {
    customers {
      identifier
      externalId
      meteringpoints(
        tenantId: $tenantId
        includeFuture: $includeFuture
        includePast: $includePast
      ) {
        meteringpoints {
          identifier
          gsrnIdentifier
          meteringPointCode
          type
          status
          address {
            streetName
            buildingNumber
            stairwellIdentification
            apartment
            addressViewFormShort
            co
            postcode
            cityName
          }
        }
      }
    }
  }
}"""

CONSUMPTION_QUERY = """query energy_usage_GetConsumptionData(
  $tenantId: ID!,
  $productIdentifier: ID!,
  $fuseSize: FuseSizeEnum,
  $customerId: ID!,
  $meteringPointId: ID!,
  $contractType: ContractTypeEnum!,
  $measurementType: MeasurementType!,
  $period: ConsumptionRangePeriod!,
  $periodStart: String!,
  $periodEnd: String!,
  $cacheTimeout: String,
  $skipFirstAndLastAvailable: Boolean,
  $offsetHours: Int,
  $isLastAvailableFullDay: Boolean,
  $includeExternalPrices: Boolean,
  $resolution: ResolutionDuration!,
  $consumptionDataResolution: ResolutionDuration,
  $timezone: String,
  $readingTypes: [ReadingType!]
) {
  consumption {
    range(
      tenantId: $tenantId
      productIdentifier: $productIdentifier
      fuseSize: $fuseSize
      customerId: $customerId
      meteringPointId: $meteringPointId
      contractType: $contractType
      measurementType: $measurementType
      period: $period
      periodStart: $periodStart
      periodEnd: $periodEnd
      cacheTimeout: $cacheTimeout
      skipFirstAndLastAvailable: $skipFirstAndLastAvailable
      useOnlyDailySums: true
      offsetHours: $offsetHours
      isLastAvailableFullDay: $isLastAvailableFullDay
      includeExternalPrices: $includeExternalPrices
    ) {
      items {
        startTime
        endTime
        sum
        costWithVat
        costWithoutVat
        status
        unit
      }
      firstAvailable
      lastAvailable
    }
    sumTimeSeries(
      tenantId: $tenantId
      customerId: $customerId
      meteringPointId: $meteringPointId
      contractType: $contractType
      measurementType: $measurementType
      resolution: $resolution
      startDate: $periodStart
      endDate: $periodEnd
      consumptionDataResolution: $consumptionDataResolution
      timezone: $timezone
      readingTypes: $readingTypes
    ) {
      meteringPointIdentifier
      measurementType
      resolution
      timeSeriesUnit
      sumValues {
        start
        stop
        value
        valueCount
        minValue
        maxValue
        minValueTime
        maxValueTime
        avgValue
        dayTime
        nightTime
        winterDayTime
      }
    }
  }
}"""

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

CREATE TABLE IF NOT EXISTS meter_points (
    metering_point_id  TEXT PRIMARY KEY,
    customer_id        TEXT NOT NULL,
    gsrn_identifier    TEXT,
    type               TEXT,
    status             TEXT,
    address            TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consumption_daily (
    metering_point_id  TEXT NOT NULL,
    contract_type      TEXT NOT NULL,
    period_start       TEXT NOT NULL,
    period_end         TEXT,
    sum_kwh            REAL,
    cost_with_vat      REAL,
    cost_without_vat   REAL,
    unit               TEXT,
    status             INTEGER,
    day_time_kwh       REAL,
    night_time_kwh     REAL,
    winter_day_kwh     REAL,
    min_value          REAL,
    min_value_time     TEXT,
    max_value          REAL,
    max_value_time     TEXT,
    avg_value          REAL,
    value_count        INTEGER,
    fetched_at         TEXT NOT NULL,
    PRIMARY KEY (metering_point_id, contract_type, period_start)
);
CREATE INDEX IF NOT EXISTS idx_consumption_daily_period_start
    ON consumption_daily(period_start);
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


def graphql_call(id_token, op_name, query, variables, *, debug=False):
    payload = {
        "operationName": op_name,
        "query": query,
        "variables": variables,
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
    log(f"  graphql: {op_name}", debug=debug)
    r = requests.post(GRAPHQL_URL, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors on {op_name}: {body['errors']}")
    return body["data"]


def get_magic_link_url(id_token):
    data = graphql_call(
        id_token,
        "priima_live_externalMagicLink",
        MAGIC_LINK_QUERY,
        {"tenantId": "PKS", "baseUrl": f"{LIVE_BASE}/Kirjaudu/PKS/Online"},
    )
    return data["auth"]["externalMagicLink"]["url"]


def fetch_customer_id(id_token, *, debug=False):
    data = graphql_call(
        id_token, "customerships_context_Customerships", CUSTOMERSHIPS_QUERY, {},
        debug=debug,
    )
    tenancies = data["endUser"]["tenancies"]
    for t in tenancies:
        if t["tenantId"] == TENANT_ID and t["customerIds"]:
            return t["customerIds"][0]
    raise RuntimeError(f"No customer found for tenant {TENANT_ID}")


def fetch_metering_points(id_token, customer_id, *, debug=False):
    data = graphql_call(
        id_token,
        "mp_select_CustomerMeteringPoints",
        METERING_POINTS_QUERY,
        {
            "tenantId": TENANT_ID,
            "customerIdentifier": customer_id,
            "includeFuture": True,
            "includePast": False,
        },
        debug=debug,
    )
    customers = data["customersByIdentifier"]["customers"]
    if not customers:
        return []
    mps = customers[0]["meteringpoints"]["meteringpoints"]
    out = []
    for mp in mps:
        addr = mp.get("address") or {}
        out.append({
            "metering_point_id": mp["identifier"],
            "gsrn_identifier": mp.get("gsrnIdentifier"),
            "type": mp.get("type"),
            "status": mp.get("status"),
            "address": addr.get("addressViewFormShort"),
        })
    return out


def fetch_consumption(
    id_token, customer_id, metering_point_id, contract_type,
    period_start_utc, period_end_utc,
    *, product_identifier="Priima", resolution="P1DT", debug=False,
):
    data = graphql_call(
        id_token,
        "energy_usage_GetConsumptionData",
        CONSUMPTION_QUERY,
        {
            "tenantId": TENANT_ID,
            "productIdentifier": product_identifier,
            "customerId": customer_id,
            "meteringPointId": metering_point_id,
            "contractType": contract_type,
            "measurementType": "ActivePower",
            "period": "Month",
            "periodStart": period_start_utc,
            "periodEnd": period_end_utc,
            "resolution": resolution,
            "skipFirstAndLastAvailable": False,
            "isLastAvailableFullDay": False,
            "includeExternalPrices": False,
        },
        debug=debug,
    )
    return data["consumption"]


def merge_consumption(consumption):
    """Merge range.items[] (sum/cost) with sumTimeSeries.sumValues[] (breakdown)
    keyed by period start."""
    items = (consumption.get("range") or {}).get("items") or []
    sums = (consumption.get("sumTimeSeries") or {}).get("sumValues") or []
    by_start = {}
    for it in items:
        by_start[it["startTime"]] = {
            "period_start": it["startTime"],
            "period_end": it.get("endTime"),
            "sum_kwh": it.get("sum"),
            "cost_with_vat": it.get("costWithVat"),
            "cost_without_vat": it.get("costWithoutVat"),
            "status": it.get("status"),
            "unit": it.get("unit"),
        }
    for sv in sums:
        row = by_start.setdefault(sv["start"], {
            "period_start": sv["start"], "period_end": sv.get("stop"),
        })
        row["period_end"] = row.get("period_end") or sv.get("stop")
        if row.get("sum_kwh") is None:
            row["sum_kwh"] = sv.get("value")
        row.update({
            "day_time_kwh": sv.get("dayTime"),
            "night_time_kwh": sv.get("nightTime"),
            "winter_day_kwh": sv.get("winterDayTime"),
            "min_value": sv.get("minValue"),
            "min_value_time": sv.get("minValueTime"),
            "max_value": sv.get("maxValue"),
            "max_value_time": sv.get("maxValueTime"),
            "avg_value": sv.get("avgValue"),
            "value_count": sv.get("valueCount"),
        })
    return sorted(by_start.values(), key=lambda r: r["period_start"])


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


def save_prices_to_db(periods, period_prices, multi, fetched_at, db_path=DB_PATH):
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


def save_meter_points(meter_points, customer_id, fetched_at, db_path=DB_PATH):
    rows = [
        (
            mp["metering_point_id"],
            customer_id,
            mp.get("gsrn_identifier"),
            mp.get("type"),
            mp.get("status"),
            mp.get("address"),
            fetched_at,
            fetched_at,
        )
        for mp in meter_points
    ]
    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT INTO meter_points "
            "(metering_point_id, customer_id, gsrn_identifier, type, status, address, "
            " first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(metering_point_id) DO UPDATE SET "
            "  customer_id     = excluded.customer_id, "
            "  gsrn_identifier = excluded.gsrn_identifier, "
            "  type            = excluded.type, "
            "  status          = excluded.status, "
            "  address         = excluded.address, "
            "  last_seen_at    = excluded.last_seen_at",
            rows,
        )
    return len(rows)


def save_consumption(rows, metering_point_id, contract_type, fetched_at, db_path=DB_PATH):
    db_rows = [
        (
            metering_point_id,
            contract_type,
            r["period_start"],
            r.get("period_end"),
            r.get("sum_kwh"),
            r.get("cost_with_vat"),
            r.get("cost_without_vat"),
            r.get("unit"),
            r.get("status"),
            r.get("day_time_kwh"),
            r.get("night_time_kwh"),
            r.get("winter_day_kwh"),
            r.get("min_value"),
            r.get("min_value_time"),
            r.get("max_value"),
            r.get("max_value_time"),
            r.get("avg_value"),
            r.get("value_count"),
            fetched_at,
        )
        for r in rows
    ]
    with open_db(db_path) as conn:
        conn.executemany(
            "INSERT INTO consumption_daily "
            "(metering_point_id, contract_type, period_start, period_end, sum_kwh, "
            " cost_with_vat, cost_without_vat, unit, status, day_time_kwh, night_time_kwh, "
            " winter_day_kwh, min_value, min_value_time, max_value, max_value_time, "
            " avg_value, value_count, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(metering_point_id, contract_type, period_start) DO UPDATE SET "
            "  period_end       = excluded.period_end, "
            "  sum_kwh          = excluded.sum_kwh, "
            "  cost_with_vat    = excluded.cost_with_vat, "
            "  cost_without_vat = excluded.cost_without_vat, "
            "  unit             = excluded.unit, "
            "  status           = excluded.status, "
            "  day_time_kwh     = excluded.day_time_kwh, "
            "  night_time_kwh   = excluded.night_time_kwh, "
            "  winter_day_kwh   = excluded.winter_day_kwh, "
            "  min_value        = excluded.min_value, "
            "  min_value_time   = excluded.min_value_time, "
            "  max_value        = excluded.max_value, "
            "  max_value_time   = excluded.max_value_time, "
            "  avg_value        = excluded.avg_value, "
            "  value_count      = excluded.value_count, "
            "  fetched_at       = excluded.fetched_at",
            db_rows,
        )
    return len(db_rows)


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


def print_prices_report(periods, period_prices, multi):
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


def print_actuals_report(per_meter, period_start_date, period_end_date):
    print("=" * 78)
    print(
        f"PKS Live — Kulutus    {period_start_date} → {period_end_date}    "
        f"({time.strftime('%Y-%m-%d %H:%M')})"
    )
    print("=" * 78)
    for entry in per_meter:
        mp = entry["meter"]
        rows = entry["rows"]
        total_kwh = sum((r.get("sum_kwh") or 0) for r in rows)
        total_cost_vat = sum((r.get("cost_with_vat") or 0) for r in rows)
        print()
        print(
            f"Käyttöpaikka {mp['metering_point_id']}  ({mp.get('type')})  "
            f"{mp.get('address') or ''}"
        )
        print("-" * 78)
        print(
            f"  {'Päivä':<12} {'kWh':>8} {'pvä-aika':>10} {'yö-aika':>10} "
            f"{'€':>9} {'€ (sis. ALV)':>14}"
        )
        for r in rows:
            day = (r.get("period_start") or "")[:10]
            print(
                f"  {day:<12} "
                f"{(r.get('sum_kwh') or 0):>8.2f} "
                f"{(r.get('day_time_kwh') or 0):>10.2f} "
                f"{(r.get('night_time_kwh') or 0):>10.2f} "
                f"{(r.get('cost_without_vat') or 0):>9.3f} "
                f"{(r.get('cost_with_vat') or 0):>14.3f}"
            )
        print("-" * 78)
        print(
            f"  Yhteensä {len(rows)} päivää: {total_kwh:.2f} kWh, "
            f"{total_cost_vat:.2f} € (sis. ALV)"
        )
    print()


SCHEMA_HINTS = {
    "description": (
        "PKS Live data. `period_prices`/`multi_prices` hold append-only snapshots of "
        "price-fixing data (one row per period/bundle per snapshot). `meter_points` and "
        "`consumption_daily` hold metered actuals, upserted by natural key — re-fetching "
        "the same date range refreshes the existing rows."
    ),
    "tables": {
        "period_prices": (
            "One row per period (monthly or quarterly) per price-fixing snapshot. "
            "period_type 0=monthly, 1=quarterly. price/price_with_vat are snt/kWh."
        ),
        "multi_prices": (
            "One row per multi-period bundle per price-fixing snapshot. "
            "bundle_type: 0=year-end bundle, 1=next-12-months, 2=next-year, 3=second-half-of-year."
        ),
        "meter_points": (
            "One row per metering point ever observed for this customer. "
            "Upserted on every `actuals fetch` — `first_seen_at` is preserved, `last_seen_at` "
            "tracks the latest fetch. `type` is 'Sales' (consumption) or 'SalesProduction' "
            "(feed-in / production)."
        ),
        "consumption_daily": (
            "One row per (metering_point_id, contract_type, period_start) — daily actuals "
            "for each metering point. Upsert key means re-fetching a date range refreshes "
            "values (numbers can be restated by the network company). "
            "sum_kwh is the daily total in kWh; day_time_kwh / night_time_kwh / winter_day_kwh "
            "split it by tariff time-of-day. cost_with_vat / cost_without_vat are in EUR. "
            "period_start is the Helsinki day boundary in UTC (e.g. '2026-05-01T21:00:00Z' = "
            "midnight Helsinki summer time, '2026-12-01T22:00:00Z' = midnight Helsinki winter time)."
        ),
    },
    "common_queries": {
        "latest_price_snapshot": (
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
        "total_consumption_last_30_days": (
            "SELECT metering_point_id, contract_type, "
            "SUM(sum_kwh) AS kwh, SUM(cost_with_vat) AS eur_with_vat "
            "FROM consumption_daily "
            "WHERE period_start >= date('now', '-30 days') "
            "GROUP BY metering_point_id, contract_type"
        ),
        "monthly_consumption_by_meter": (
            "SELECT metering_point_id, contract_type, "
            "strftime('%Y-%m', period_start) AS month, "
            "SUM(sum_kwh) AS kwh, SUM(cost_with_vat) AS eur_with_vat "
            "FROM consumption_daily "
            "GROUP BY metering_point_id, contract_type, month "
            "ORDER BY month DESC"
        ),
        "day_vs_night_share": (
            "SELECT period_start, sum_kwh, day_time_kwh, night_time_kwh, "
            "ROUND(100.0 * night_time_kwh / sum_kwh, 1) AS night_pct "
            "FROM consumption_daily WHERE contract_type = 'Sales' "
            "ORDER BY period_start DESC LIMIT 30"
        ),
        "lock_in_vs_last_year_actuals": (
            "WITH lock AS ("
            "  SELECT instrument_name, period_start, period_stop, price_with_vat "
            "  FROM period_prices WHERE instrument_name = ? "
            "  ORDER BY fetched_at DESC LIMIT 1"
            "), "
            "historic AS ("
            "  SELECT SUM(sum_kwh) AS kwh, SUM(cost_with_vat) AS eur_actual_with_vat, "
            "         COUNT(*) AS days "
            "  FROM consumption_daily, lock "
            "  WHERE contract_type = 'Sales' "
            "    AND consumption_daily.period_start >= datetime(lock.period_start, '-1 year') "
            "    AND consumption_daily.period_start <= datetime(lock.period_stop,  '-1 year')"
            ") "
            "SELECT lock.instrument_name, "
            "       date(lock.period_start, '+3 hours') AS lock_starts, "
            "       date(lock.period_stop)             AS lock_ends, "
            "       lock.price_with_vat                AS lock_snt_kwh_with_vat, "
            "       ROUND(historic.kwh, 1)             AS last_year_kwh, "
            "       ROUND(historic.eur_actual_with_vat, 2) AS last_year_eur_actual, "
            "       ROUND(historic.kwh * lock.price_with_vat / 100, 2) AS hypothetical_eur_at_lock, "
            "       ROUND(historic.kwh * lock.price_with_vat / 100 - historic.eur_actual_with_vat, 2) AS delta_eur, "
            "       ROUND(100.0 * (historic.kwh * lock.price_with_vat / 100 - historic.eur_actual_with_vat) "
            "             / historic.eur_actual_with_vat, 1) AS delta_pct, "
            "       historic.days AS days_of_data "
            "FROM lock, historic"
        ),
        "available_lock_in_offers": (
            "SELECT instrument_name, period_type, price_with_vat, "
            "       date(period_start, '+3 hours') AS starts, "
            "       date(period_stop)              AS ends "
            "FROM period_prices "
            "WHERE fetched_at = (SELECT MAX(fetched_at) FROM period_prices) "
            "  AND price IS NOT NULL "
            "ORDER BY period_start"
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

            col_names = {c["name"] for c in cols}
            timeline_col = (
                "fetched_at" if "fetched_at" in col_names else
                "period_start" if "period_start" in col_names else
                None
            )
            mn = mx = runs = None
            if timeline_col:
                mn, mx = conn.execute(
                    f"SELECT MIN({timeline_col}), MAX({timeline_col}) FROM {t}"
                ).fetchone()
                if timeline_col == "fetched_at":
                    runs = conn.execute(
                        f"SELECT COUNT(DISTINCT {timeline_col}) FROM {t}"
                    ).fetchone()[0]

            sample = None
            r = conn.execute(f"SELECT * FROM {t} ORDER BY rowid DESC LIMIT 1").fetchone()
            if r is not None:
                sample = {k: r[k] for k in r.keys()}
            info["tables"][t] = {
                "description": SCHEMA_HINTS["tables"].get(t),
                "columns": cols,
                "indexes": indexes,
                "row_count": row_count,
                "earliest": mn,
                "latest": mx,
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
        print(json.dumps({"ok": False, "error": "db_missing", "message": f"No DB at {args.db} — run `prices fetch` or `actuals fetch` first."}))
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
    if not pathlib.Path(args.db).exists():
        print(json.dumps({"ok": False, "error": "db_missing", "message": f"No DB at {args.db} — run `prices fetch` first."}))
        sys.exit(1)
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


def build_prices_payload(periods, period_prices, multi, fetched_at, db_path, saved):
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


def cmd_prices_fetch(args):
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
            n_periods, n_multi = save_prices_to_db(
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
        payload = build_prices_payload(
            periods, period_prices, multi, fetched_at, args.db, saved
        )
        payload["ok"] = True
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print_prices_report(periods, period_prices, multi)


def helsinki_day_start_utc(date_str):
    d = dt.date.fromisoformat(date_str)
    local = dt.datetime.combine(d, dt.time(0, 0, 0), tzinfo=HELSINKI_TZ)
    utc = local.astimezone(dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def helsinki_day_end_utc(date_str):
    d = dt.date.fromisoformat(date_str)
    local = dt.datetime.combine(d, dt.time(23, 59, 59, 999000), tzinfo=HELSINKI_TZ)
    utc = local.astimezone(dt.timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.999Z")


def iter_helsinki_months(from_date_iso, to_date_iso):
    """Yield (period_start_utc, period_end_utc, label) for each Helsinki calendar
    month that overlaps [from_date_iso, to_date_iso]. Partial first/last months
    are clipped to the requested range. The API only accepts one month per
    `energy_usage_GetConsumptionData` call."""
    start = dt.date.fromisoformat(from_date_iso)
    end = dt.date.fromisoformat(to_date_iso)
    cur = start.replace(day=1)
    while cur <= end:
        if cur.month == 12:
            next_month_first = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            next_month_first = cur.replace(month=cur.month + 1, day=1)
        last_day = next_month_first - dt.timedelta(days=1)
        clip_start = max(cur, start)
        clip_end = min(last_day, end)
        yield (
            helsinki_day_start_utc(clip_start.isoformat()),
            helsinki_day_end_utc(clip_end.isoformat()),
            f"{cur.year}-{cur.month:02d}",
        )
        cur = next_month_first


def resolve_date_range(args):
    today_helsinki = dt.datetime.now(HELSINKI_TZ).date()
    end_date = dt.date.fromisoformat(args.to_date) if args.to_date else today_helsinki
    if args.from_date:
        start_date = dt.date.fromisoformat(args.from_date)
    else:
        days = args.days if args.days else 30
        start_date = end_date - dt.timedelta(days=days)
    if start_date > end_date:
        raise ValueError(f"--from ({start_date}) is after --to ({end_date})")
    return start_date.isoformat(), end_date.isoformat()


def cmd_actuals_fetch(args):
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
        from_date, to_date = resolve_date_range(args)
    except ValueError as e:
        fail("bad_args", str(e))

    try:
        log("[1/4] Cognito SRP login…", debug=args.debug)
        id_token = cognito_login(args.username, args.password)

        log("[2/4] Fetching customer id…", debug=args.debug)
        customer_id = fetch_customer_id(id_token, debug=args.debug)
        log(f"        customer_id={customer_id}", debug=args.debug)

        log("[3/4] Fetching metering points…", debug=args.debug)
        meter_points = fetch_metering_points(id_token, customer_id, debug=args.debug)
        if args.metering_point:
            meter_points = [m for m in meter_points if m["metering_point_id"] == args.metering_point]
            if not meter_points:
                fail("bad_args", f"Metering point {args.metering_point} not found.")
        log(
            f"        {len(meter_points)} metering point(s): "
            + ", ".join(f"{m['metering_point_id']}({m['type']})" for m in meter_points),
            debug=args.debug,
        )

        months = list(iter_helsinki_months(from_date, to_date))
        log(
            f"[4/4] Fetching consumption {from_date}…{to_date} "
            f"({len(months)} month(s) × {len(meter_points)} meter(s), "
            f"resolution={args.resolution}, product={args.product_identifier})…",
            debug=args.debug,
        )
        per_meter = []
        for mp in meter_points:
            rows_by_start = {}
            for month_start_utc, month_end_utc, label in months:
                log(
                    f"        {mp['metering_point_id']} {label}…",
                    debug=args.debug,
                )
                consumption = fetch_consumption(
                    id_token,
                    customer_id,
                    mp["metering_point_id"],
                    mp["type"],
                    month_start_utc,
                    month_end_utc,
                    product_identifier=args.product_identifier,
                    resolution=args.resolution,
                    debug=args.debug,
                )
                for row in merge_consumption(consumption):
                    rows_by_start[row["period_start"]] = row
            rows = sorted(rows_by_start.values(), key=lambda r: r["period_start"])
            per_meter.append({"meter": mp, "rows": rows})
            log(
                f"        {mp['metering_point_id']}: {len(rows)} rows total",
                debug=args.debug,
            )
    except Exception as e:
        kind = type(e).__name__
        if "Cognito" in kind or "InitiateAuth" in str(e) or "UserNotFound" in kind:
            fail("auth_failed", f"{kind}: {e}")
        fail("fetch_failed", f"{kind}: {e}")

    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    saved = False
    total_rows = 0
    if not args.no_save:
        try:
            save_meter_points(
                meter_points, customer_id, fetched_at, db_path=pathlib.Path(args.db)
            )
            for entry in per_meter:
                mp = entry["meter"]
                n = save_consumption(
                    entry["rows"],
                    mp["metering_point_id"],
                    mp["type"],
                    fetched_at,
                    db_path=pathlib.Path(args.db),
                )
                total_rows += n
            saved = True
            log(
                f"      saved/updated {total_rows} consumption rows + "
                f"{len(meter_points)} meter point(s) to {args.db}",
                debug=args.debug,
            )
        except Exception as e:
            if json_mode:
                fail("db_write_failed", f"{type(e).__name__}: {e}")
            raise

    if json_mode:
        payload = {
            "ok": True,
            "fetched_at": fetched_at,
            "saved_to_db": saved,
            "db_path": str(args.db) if saved else None,
            "from_date": from_date,
            "to_date": to_date,
            "resolution": args.resolution,
            "customer_id": customer_id,
            "meters": per_meter,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print_actuals_report(per_meter, from_date, to_date)


def build_parser():
    ap = argparse.ArgumentParser(
        description="PKS Live data fetcher and local SQLite store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Subcommands:\n"
            "  prices fetch      Log in, pull price-fixings, persist snapshot, print report\n"
            "  actuals fetch     Pull metered consumption history and upsert into DB\n"
            "  describe          Emit DB schema as JSON (for agents)\n"
            "  query             Run a read-only SQL query, output JSON rows\n"
            "  runs              List historical price-fixing snapshots\n"
        ),
    )
    ap.add_argument(
        "--db",
        default=os.environ.get("PKS_DB_PATH", str(DB_PATH)),
        help="SQLite path (default: %(default)s)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_prices = sub.add_parser("prices", help="Price-fixing snapshot commands")
    prices_sub = ap_prices.add_subparsers(dest="action", required=True)
    ap_prices_fetch = prices_sub.add_parser(
        "fetch", help="Fetch current prices and persist a snapshot"
    )
    ap_prices_fetch.add_argument("--username", default=os.environ.get("PKS_USERNAME"))
    ap_prices_fetch.add_argument("--password", default=os.environ.get("PKS_PASSWORD"))
    ap_prices_fetch.add_argument(
        "--tube-type", type=int, default=int(os.environ.get("PKS_TUBE_TYPE", "1"))
    )
    ap_prices_fetch.add_argument(
        "--no-save", action="store_true", help="Skip writing to SQLite"
    )
    ap_prices_fetch.add_argument(
        "--json", action="store_true",
        help="Emit a structured JSON snapshot instead of the human report",
    )
    ap_prices_fetch.add_argument("--debug", action="store_true")

    ap_actuals = sub.add_parser("actuals", help="Metered consumption commands")
    actuals_sub = ap_actuals.add_subparsers(dest="action", required=True)
    ap_actuals_fetch = actuals_sub.add_parser(
        "fetch", help="Fetch metered consumption and upsert into DB"
    )
    ap_actuals_fetch.add_argument("--username", default=os.environ.get("PKS_USERNAME"))
    ap_actuals_fetch.add_argument("--password", default=os.environ.get("PKS_PASSWORD"))
    ap_actuals_fetch.add_argument(
        "--from", dest="from_date",
        help="Start date YYYY-MM-DD (Helsinki). Default: --to minus --days.",
    )
    ap_actuals_fetch.add_argument(
        "--to", dest="to_date",
        help="End date YYYY-MM-DD (Helsinki). Default: today.",
    )
    ap_actuals_fetch.add_argument(
        "--days", type=int, default=None,
        help="When --from is not given, fetch this many days back from --to (default 30).",
    )
    ap_actuals_fetch.add_argument(
        "--resolution", default="P1DT",
        help="ISO-8601 duration. P1DT (daily, default), PT1H (hourly), PT15M (15-min).",
    )
    ap_actuals_fetch.add_argument(
        "--metering-point",
        help="Limit to one metering point id (default: all the customer has).",
    )
    ap_actuals_fetch.add_argument(
        "--product-identifier", default="Priima",
        help="Product identifier on the contract (default: Priima).",
    )
    ap_actuals_fetch.add_argument(
        "--no-save", action="store_true", help="Skip writing to SQLite"
    )
    ap_actuals_fetch.add_argument(
        "--json", action="store_true",
        help="Emit a structured JSON snapshot instead of the human report",
    )
    ap_actuals_fetch.add_argument("--debug", action="store_true")

    sub.add_parser("describe", help="Emit DB schema as JSON")

    ap_query = sub.add_parser("query", help="Run a read-only SQL query against the DB")
    ap_query.add_argument("sql", help="SQL string (use ? placeholders for params)")
    ap_query.add_argument("params", nargs="*", help="Positional bindings for ? placeholders")

    sub.add_parser("runs", help="List all historical price-fixing snapshots")

    return ap


DISPATCH = {
    ("prices", "fetch"): cmd_prices_fetch,
    ("actuals", "fetch"): cmd_actuals_fetch,
}


def main():
    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent / ".env")
    ap = build_parser()
    args = ap.parse_args()

    if args.cmd == "describe":
        cmd_describe(args)
    elif args.cmd == "query":
        cmd_query(args)
    elif args.cmd == "runs":
        cmd_runs(args)
    else:
        handler = DISPATCH.get((args.cmd, args.action))
        if handler is None:
            ap.error(f"Unknown command: {args.cmd} {args.action}")
        handler(args)


if __name__ == "__main__":
    main()
