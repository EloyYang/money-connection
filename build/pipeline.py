#!/usr/bin/env python3
"""Multi-asset correlation dashboard build pipeline.

One run does everything:
  1. current membership for every universe:
       - Nasdaq-100                (api.nasdaq.com list-type/nasdaq100)
       - KOSPI100                  (finance.naver.com constituent pages)
       - major US ETFs / BTC / ETH / gold (data/assets.json, curated list)
  2. sector/industry for new Nasdaq-100 tickers -> theme (KOSPI/ETF/crypto/gold
     get a single fixed theme each; classify() only runs for US equities)
  3. 5 years of daily OHLCV per ticker (Nasdaq for US assets, Naver for KOSPI)
  4. correlation / lead-lag / rotation analytics across the WHOLE universe
  5. render index.html from the template

Membership, market caps and prices all come from the same daily pull, so an
index change (add, drop, ticker rename) flows through without hand edits.
"지수" figures (주도주 leaders, index_chg) stay scoped to Nasdaq-100 only —
a market-cap-weighted blend of Samsung, Bitcoin and Apple would not mean
anything as a single "index". Fundamentals likewise only exist for the
original Nasdaq-100 set: Nasdaq's financials/EPS endpoints are US-equity only.

Usage:  python3 build/pipeline.py [--out dist] [--cache cache] [--max-workers 4]
"""
from __future__ import annotations
import argparse, datetime, json, math, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

YEARS = 5
CLIP = 0.25          # winsorise daily returns: one corporate action must not own a correlation
MAX_LAG = 5          # lead-lag window, trading days
PAIR_MIN_R = 0.20    # store a lag profile only for pairs the UI can surface
MIN_OVERLAP = 40     # trading days required before a pair gets a correlation
CORR_WINDOW_DAYS = 365   # correlation/network uses the most recent calendar year.
                         # Calendar days, not a row count: `dates` is now the UNION
                         # of every universe's trading days, and crypto trades all 7 —
                         # a fixed 252-row slice would silently cover less than a year
                         # once enough weekend-only crypto rows are mixed in.


def log(msg): print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_json(url, retries=4, timeout=30, headers=None):
    last = None
    h = dict(HEADERS)
    if headers: h.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:                       # noqa: BLE001 - any failure is retryable here
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


def get_text(url, encoding="utf-8", retries=4, timeout=30, headers=None):
    """Like get_json but for non-JSON responses (Naver's HTML/JS-literal pages)."""
    last = None
    h = dict(HEADERS)
    if headers: h.update(headers)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode(encoding, errors="replace")
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# --------------------------------------------------------------------------
# 1. membership
# --------------------------------------------------------------------------
def fetch_constituents():
    d = get_json("https://api.nasdaq.com/api/quote/list-type/nasdaq100")
    rows = d["data"]["data"]["rows"]
    out = {}
    for r in rows:
        tk = r["symbol"].strip().upper()
        name = re.sub(r"\s+(Common Stock|Ordinary Shares?|Class [A-Z])( .*)?$", "",
                      r.get("companyName", "")).strip() or tk
        cap = r.get("marketCap") or ""
        try: cap = float(str(cap).replace(",", ""))
        except ValueError: cap = 0.0
        px = str(r.get("lastSalePrice", "")).replace("$", "").replace(",", "")
        try: px = float(px)
        except ValueError: px = None
        out[tk] = {"name": name, "market_cap": cap, "quote_px": px,
                   "asset_class": "us_stock", "currency": "USD", "source": "nasdaq_stock"}
    log(f"Nasdaq-100 members: {len(out)} (as of {d['data'].get('date')})")
    return out, d["data"].get("date")


# --------------------------------------------------------------------------
# 1b. other universes: KOSPI100, US ETFs, crypto, gold
# --------------------------------------------------------------------------
def fetch_usdkrw_rate():
    """Live rate to convert KOSPI market caps into the same USD scale used for
    node sizing everywhere else. Falls back to a fixed estimate if the free
    endpoint is unreachable — sizing only, never shown to the user as a quote."""
    try:
        d = get_json("https://api.exchangerate-api.com/v4/latest/USD", retries=2, timeout=15)
        rate = float(d["rates"]["KRW"])
        log(f"USD/KRW: {rate}")
        return rate
    except Exception as e:                            # noqa: BLE001
        log(f"  ! USD/KRW lookup failed ({e}), falling back to 1350")
        return 1350.0


def fetch_kospi100(usdkrw):
    """Scrape Naver's KOSPI100 constituent pages: name, code, price, market cap
    (원 표기 억원 -> 원). No public JSON endpoint for this list; the HTML table
    is stable and cheap (~10 short pages)."""
    out = {}
    pat = re.compile(
        r'<td class="ctg"><a href="/item/main\.naver\?code=(\d{6})"[^>]*>([^<]+)</a></td>\s*'
        r'<td class="number_2">([\d,]+)</td>.*?'
        r'<td class="number_2">([\d,]+)</td>\s*</tr>', re.S)
    for page in range(1, 15):
        html = get_text(
            f"https://finance.naver.com/sise/entryJongmok.naver?type=KPI100&page={page}",
            encoding="euc-kr", retries=3, headers={"Referer": "https://finance.naver.com/"})
        rows = pat.findall(html)
        if not rows:
            break
        for code, name, price, cap_eok in rows:
            cap_krw = float(cap_eok.replace(",", "")) * 1e8
            out[code] = {
                "name": name.strip(), "quote_px": float(price.replace(",", "")),
                "market_cap": cap_krw / usdkrw,          # USD-equivalent, for node sizing
                "market_cap_krw": cap_krw,
                "asset_class": "kr_stock", "currency": "KRW", "source": "naver_stock",
            }
        time.sleep(0.15)
    log(f"KOSPI100 members: {len(out)}")
    return out


def resolve_kospi_themes(kospi_members, cfg, cache_path):
    """KOSPI gets the SAME 8 themes as Nasdaq, not its own bucket — each
    stock's WICS industry label (Naver's item page, `업종명 : <a>...</a>`)
    maps onto whichever Nasdaq theme it resembles (data/themes.json
    `kospi_industry_map`). Cached like resolve_themes(): only new/never-seen
    codes get fetched."""
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    codes = list(kospi_members)
    # entries cached before the dividend field was added are bare strings
    missing = [c for c in codes if not isinstance(cache.get(c), dict)]
    if missing:
        log(f"looking up WICS industry for {len(missing)} new KOSPI ticker(s)")

    def one(code):
        try:
            html = get_text(f"https://finance.naver.com/item/main.naver?code={code}",
                            encoding="utf-8", retries=3, timeout=20)
            m = re.search(r'업종명\s*:\s*<a[^>]*>([^<]+)</a>', html)
            # same page carries 배당수익률 — free, and the defensive/income
            # profile is dishonest if only US names have a dividend
            y = re.search(r'배당수익률[\s\S]{0,300}?<em[^>]*>([\d.]+)</em>', html)
            return code, {"industry": m.group(1).strip() if m else None,
                          "yield": float(y.group(1)) if y else None}
        except Exception as e:                        # noqa: BLE001
            log(f"  ! industry lookup failed for {code}: {e}")
            return code, {"industry": None, "yield": None}

    if missing:
        with ThreadPoolExecutor(max_workers=6) as ex:
            for code, industry in ex.map(one, missing):
                cache[code] = industry

    assign = {}
    imap = cfg.get("kospi_industry_map", {})
    for code in codes:
        rec = cache.get(code) or {}
        ind = rec.get("industry")
        theme = imap.get(ind, 9)
        assign[code] = theme
        kospi_members[code]["div_yield"] = rec.get("yield")
        if theme == 9:
            log(f"  ! {code} ({kospi_members[code]['name']}) industry '{ind}' has no theme mapping")
    cache = {c: v for c, v in cache.items() if c in set(codes)}   # drop removed members
    json.dump(cache, open(cache_path, "w"), indent=1, ensure_ascii=False, sort_keys=True)
    return assign


def fetch_extra_assets():
    """US ETFs / crypto / gold from the curated data/assets.json list. Market
    cap is unknown at this point for ETFs and gold (no AUM endpoint); it is
    backfilled from the price series in fetch_all_prices() once fetched."""
    cfg = json.load(open(os.path.join(ROOT, "data", "assets.json")))
    out = {}
    for e in cfg["etfs"]:
        out[e["t"]] = {"name": e["label"], "quote_px": None, "market_cap": 0.0,
                       "asset_class": "etf", "currency": "USD", "source": "nasdaq_etf"}
    for e in cfg["crypto"]:
        out[e["t"]] = {"name": e["label"], "quote_px": None, "market_cap": 0.0,
                       "asset_class": "crypto", "currency": "USD", "source": "nasdaq_crypto",
                       "circulating_supply": e["circulating_supply"]}
    for e in cfg["gold"]:
        out[e["t"]] = {"name": e["label"], "quote_px": None, "market_cap": 0.0,
                       "asset_class": "commodity", "currency": "USD", "source": "nasdaq_etf"}
    log(f"extra assets: {len(cfg['etfs'])} ETF + {len(cfg['crypto'])} crypto + {len(cfg['gold'])} gold")
    return out


# --------------------------------------------------------------------------
# 2. sector -> theme
# --------------------------------------------------------------------------
def load_theme_config():
    return json.load(open(os.path.join(ROOT, "data", "themes.json")))


def classify(tk, sector, industry, cfg):
    ov = cfg["overrides"].get(tk)
    if ov: return ov, "override"
    ind = (industry or "").lower()
    for pattern, theme in cfg["industry_rules"]:
        if re.search(pattern, ind):
            return theme, f"industry:{industry}"
    sec = (sector or "").lower().strip()
    if sec in cfg["sector_fallback"]:
        return cfg["sector_fallback"][sec], f"sector:{sector}"
    return 9, "unclassified"


def resolve_themes(tickers, cfg, cache_path):
    known = {}
    if os.path.exists(cache_path):
        known = json.load(open(cache_path))
    missing = [t for t in tickers if t not in known]
    if missing:
        log(f"looking up sector/industry for {len(missing)} new ticker(s): {', '.join(missing)}")

    def one(tk):
        try:
            d = get_json(f"https://api.nasdaq.com/api/quote/{tk}/summary?assetclass=stocks", retries=3)
            sd = (d.get("data") or {}).get("summaryData") or {}
            return tk, (sd.get("Sector") or {}).get("value"), (sd.get("Industry") or {}).get("value")
        except Exception as e:                        # noqa: BLE001
            log(f"  ! sector lookup failed for {tk}: {e}")
            return tk, None, None

    if missing:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for tk, sector, industry in ex.map(one, missing):
                known[tk] = {"sector": sector, "industry": industry}

    assign = {}
    for tk in tickers:
        info = known.get(tk, {})
        theme, why = classify(tk, info.get("sector"), info.get("industry"), cfg)
        info["theme"] = theme
        info["why"] = why
        known[tk] = info
        assign[tk] = theme
        if theme == 9:
            log(f"  ! {tk} could not be classified (sector={info.get('sector')}, industry={info.get('industry')})")
    json.dump(known, open(cache_path, "w"), indent=1, ensure_ascii=False, sort_keys=True)
    return assign, known


# --------------------------------------------------------------------------
# 2b. fundamentals
# --------------------------------------------------------------------------
def money(v):
    """Nasdaq statement figures are strings in thousands: '$215,938,000'."""
    if v is None: return None
    s = str(v).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "N/A", "--", "n/a"): return None
    neg = s.startswith("(") and s.endswith(")")
    try: x = float(s.strip("()"))
    except ValueError: return None
    return -x if neg else x


# Bump when the shape of a fundamentals entry changes: cached rows built by an
# older pipeline are then refetched instead of silently missing new fields.
FUND_SCHEMA = 2


def fetch_fundamentals_one(tk):
    f = {"fetched": datetime.date.today().isoformat(), "v": FUND_SCHEMA}
    sd = ((get_json(f"https://api.nasdaq.com/api/quote/{tk}/summary?assetclass=stocks", retries=2)
           .get("data") or {}).get("summaryData") or {})
    val = lambda k: (sd.get(k) or {}).get("value")
    f["target"] = money(val("OneYrTarget"))
    f["range52"] = val("FiftTwoWeekHighLow")
    f["yield"] = val("Yield")
    f["div"] = val("AnnualizedDividend")

    d = get_json(f"https://api.nasdaq.com/api/company/{tk}/financials?frequency=1", retries=2).get("data") or {}
    rows = lambda tab: {r["value1"].strip(): r for r in (d.get(tab) or {}).get("rows", [])}
    inc, bal, cf = rows("incomeStatementTable"), rows("balanceSheetTable"), rows("cashFlowTable")
    hdr = (d.get("incomeStatementTable") or {}).get("headers", {})
    f["periods"] = [hdr.get(f"value{i}") for i in (2, 3, 4, 5)]
    pick = lambda t, label: [money((t.get(label) or {}).get(f"value{i}")) for i in (2, 3, 4, 5)]
    f["revenue"] = pick(inc, "Total Revenue")
    f["op_income"] = pick(inc, "Operating Income")
    f["net_income"] = pick(inc, "Net Income") if "Net Income" in inc else pick(cf, "Net Income")
    f["liabilities"] = pick(bal, "Total Liabilities")
    f["lt_debt"] = pick(bal, "Long-Term Debt")
    f["cash"] = pick(bal, "Cash and Cash Equivalents")
    f["equity"] = pick(bal, "Total Equity")
    f["assets"] = pick(bal, "Total Assets")

    # quarterly cut of the same statements, for the 분기별 toggle
    dq = get_json(f"https://api.nasdaq.com/api/company/{tk}/financials?frequency=2", retries=2).get("data") or {}
    rq = lambda tab: {r["value1"].strip(): r for r in (dq.get(tab) or {}).get("rows", [])}
    qinc, qbal, qcf = rq("incomeStatementTable"), rq("balanceSheetTable"), rq("cashFlowTable")
    qhdr = (dq.get("incomeStatementTable") or {}).get("headers", {})
    f["q_periods"] = [qhdr.get(f"value{i}") for i in (2, 3, 4, 5)]
    f["q_revenue"] = pick(qinc, "Total Revenue")
    f["q_op_income"] = pick(qinc, "Operating Income")
    f["q_net_income"] = pick(qinc, "Net Income") if "Net Income" in qinc else pick(qcf, "Net Income")
    f["q_liabilities"] = pick(qbal, "Total Liabilities")
    f["q_cash"] = pick(qbal, "Cash and Cash Equivalents")

    eps = (get_json(f"https://api.nasdaq.com/api/quote/{tk}/eps", retries=2).get("data") or {}).get("earningsPerShare") or []
    f["eps"] = [{"t": "P" if e.get("type") == "PreviousQuarter" else "U",
                 "p": e.get("period"),
                 "c": e.get("consensus"),
                 "a": e.get("earnings")} for e in eps]
    prev = [e for e in f["eps"] if e["t"] == "P" and e.get("a")]
    f["eps_ttm"] = round(sum(float(e["a"]) for e in prev[-4:]), 2) if len(prev) >= 4 else None

    # reported quarters carry their actual report date; forecasts extend further
    # out than /eps does, and yearlyForecast is the only annual EPS series that
    # exists here (no endpoint serves historical annual EPS).
    try:
        sur = (get_json(f"https://api.nasdaq.com/api/company/{tk}/earnings-surprise", retries=2)
               .get("data") or {}).get("earningsSurpriseTable") or {}
        f["surprise"] = [{"p": r.get("fiscalQtrEnd"), "d": r.get("dateReported"),
                          "a": r.get("eps"), "c": money(r.get("consensusForecast")),
                          "s": money(r.get("percentageSurprise"))}
                         for r in (sur.get("rows") or [])]
    except Exception:
        f["surprise"] = []
    try:
        fc = get_json(f"https://api.nasdaq.com/api/analyst/{tk}/earnings-forecast", retries=2).get("data") or {}
        conv = lambda tab: [{"p": r.get("fiscalEnd"), "c": r.get("consensusEPSForecast"),
                             "hi": r.get("highEPSForecast"), "lo": r.get("lowEPSForecast"),
                             "n": r.get("noOfEstimates")}
                            for r in ((fc.get(tab) or {}).get("rows") or [])]
        f["fc_year"] = conv("yearlyForecast")
        f["fc_qtr"] = conv("quarterlyForecast")
    except Exception:
        f["fc_year"] = f["fc_qtr"] = []
    return f


def fetch_fundamentals(tickers, cache_path, max_refresh, workers):
    """Statements move quarterly, so refresh a rotating slice: the oldest
    `max_refresh` entries plus anything missing. A full run costs 3 calls per
    ticker; this keeps the daily job cheap while nothing goes stale for long."""
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    today = datetime.date.today().isoformat()
    missing = [t for t in tickers if t not in cache or cache[t].get("v") != FUND_SCHEMA]
    stale = sorted([t for t in tickers if t in cache and cache[t].get("v") == FUND_SCHEMA],
                   key=lambda t: cache[t].get("fetched", ""))
    todo = missing + [t for t in stale if cache[t].get("fetched", "") != today]
    todo = todo[:max(len(missing), max_refresh)]
    if todo:
        log(f"fundamentals: refreshing {len(todo)} ({len(missing)} new)")

    def one(tk):
        try:
            return tk, fetch_fundamentals_one(tk), None
        except Exception as e:                        # noqa: BLE001
            return tk, None, str(e)

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for tk, data, err in ex.map(one, todo):
                if data: cache[tk] = data
                else: log(f"  ! fundamentals failed for {tk}: {err}")
    cache = {t: v for t, v in cache.items() if t in set(tickers)}   # drop removed members
    json.dump(cache, open(cache_path, "w"), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return cache


# --------------------------------------------------------------------------
# 2c. macro factors
# --------------------------------------------------------------------------
# Each factor is a tradeable daily proxy, signed so that a POSITIVE value always
# means "this factor went up". FRED's CSV endpoint is unreachable from CI, so
# rates are read off Treasury-ETF returns (inverted) rather than actual yields —
# the sign and relative ordering are what the sensitivity read-out needs.
MACRO_FACTORS = [
    {"key": "rate",   "label": "금리",     "proxy": "IEF",  "sign": -1,
     "note": "미 국채 7-10년 ETF 수익률의 반대 부호 (가격↓ = 금리↑)"},
    {"key": "dollar", "label": "달러",     "proxy": "UUP",  "sign": +1, "note": "달러인덱스 ETF"},
    {"key": "oil",    "label": "유가",     "proxy": "USO",  "sign": +1, "note": "WTI 원유 ETF"},
    {"key": "vol",    "label": "변동성",   "proxy": "VIXY", "sign": +1, "note": "VIX 선물 ETF"},
    {"key": "credit", "label": "신용선호", "proxy": "HYG",  "sign": +1, "note": "하이일드 회사채 ETF"},
    {"key": "market", "label": "시장",     "proxy": "SPY",  "sign": +1, "note": "S&P500 ETF"},
    {"key": "gold",   "label": "금",       "proxy": "GLD",  "sign": +1, "note": "금 ETF"},
]

# BLS public API v1 needs no key. Monthly, so it can only support monthly-return
# comparisons — nowhere near daily-factor precision, and labelled as such.
BLS_SERIES = [
    {"id": "CUUR0000SA0",    "key": "cpi",      "label": "소비자물가(CPI)", "diff": "pct"},
    {"id": "LNS14000000",    "key": "unemp",    "label": "실업률",          "diff": "abs"},
    {"id": "CES0000000001",  "key": "payrolls", "label": "비농업 고용",     "diff": "pct"},
]


def fetch_macro_factors(frm, to):
    """Daily factor returns keyed by factor key."""
    out = {}
    for f in MACRO_FACTORS:
        try:
            rows = fetch_history(f["proxy"], frm.isoformat(), to.isoformat(), assetclass="etf")
        except Exception as e:                        # noqa: BLE001
            log(f"  ! macro proxy {f['proxy']} failed: {e}")
            continue
        closes = {d: r[3] for d, r in rows.items()}
        dates = sorted(closes)
        rr, prev = {}, None
        for d in dates:
            if prev and prev > 0:
                rr[d] = max(-CLIP, min(CLIP, (closes[d] - prev) / prev)) * f["sign"]
            prev = closes[d]
        out[f["key"]] = rr
    log(f"macro factors: {len(out)}/{len(MACRO_FACTORS)} ({', '.join(out)})")
    return out


def fetch_bls_macro():
    """Monthly CPI / unemployment / payrolls. Returns {key: {'YYYY-MM': value}}."""
    import urllib.request
    body = json.dumps({"seriesid": [b["id"] for b in BLS_SERIES],
                       "startyear": str(datetime.date.today().year - 6),
                       "endyear": str(datetime.date.today().year)}).encode()
    try:
        req = urllib.request.Request("https://api.bls.gov/publicAPI/v1/timeseries/data/",
                                     data=body, headers={**HEADERS, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())
    except Exception as e:                            # noqa: BLE001
        log(f"  ! BLS fetch failed: {e}")
        return {}
    by_id = {b["id"]: b for b in BLS_SERIES}
    out = {}
    for sr in (d.get("Results") or {}).get("series", []):
        meta = by_id.get(sr.get("seriesID"))
        if not meta: continue
        vals = {}
        for row in sr.get("data", []):
            per = row.get("period", "")
            if not per.startswith("M") or per == "M13": continue
            try: vals[f"{row['year']}-{per[1:]}"] = float(row["value"])
            except (ValueError, KeyError): continue
        if vals: out[meta["key"]] = vals
    log(f"BLS macro: {', '.join(f'{k}({len(v)}개월)' for k, v in out.items()) or '없음'}")
    return out


def ols_beta(xs, ys):
    """Univariate beta with its t-statistic — the t is what says whether the
    sensitivity is worth reading at all."""
    n = len(xs)
    if n < 40: return None, None
    mx, my = sum(xs)/n, sum(ys)/n
    sxx = sum((x-mx)**2 for x in xs)
    if sxx <= 0: return None, None
    beta = sum((xs[i]-mx)*(ys[i]-my) for i in range(n)) / sxx
    a = my - beta*mx
    resid = [ys[i] - (a + beta*xs[i]) for i in range(n)]
    sse = sum(r*r for r in resid)
    if n <= 2 or sse <= 0: return round(beta, 3), None
    se = math.sqrt(sse/(n-2)/sxx)
    return round(beta, 3), (round(beta/se, 2) if se > 0 else None)


# --------------------------------------------------------------------------
# 3. prices
# --------------------------------------------------------------------------
def parse_money(v):
    if v is None: return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if s in ("", "N/A", "n/a"): return None
    try: return float(s)
    except ValueError: return None


def fetch_history(tk, frm, to, assetclass="stocks"):
    url = (f"https://api.nasdaq.com/api/quote/{tk}/historical"
           f"?assetclass={assetclass}&fromdate={frm}&todate={to}&limit=99999")
    rows = ((get_json(url).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    out = {}
    for r in rows:
        try: dt = datetime.datetime.strptime(r["date"], "%m/%d/%Y").date()
        except Exception: continue
        c = parse_money(r.get("close"))
        if c is None: continue
        o = parse_money(r.get("open")) or c
        h = parse_money(r.get("high")) or max(o, c)
        l = parse_money(r.get("low")) or min(o, c)
        v = parse_money(r.get("volume")) or 0
        out[dt] = [round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v / 1000)]
    return out


def fetch_naver_history(code, frm, to):
    """Naver returns the whole requested range in one call — a JS-array-literal
    payload, not strict JSON (the header row uses single quotes), so the data
    rows are pulled out with a regex rather than json.loads."""
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}&requestType=1"
           f"&startTime={frm.replace('-','')}&endTime={to.replace('-','')}&timeframe=day")
    text = get_text(url, retries=3, headers={"Referer": "https://finance.naver.com/"})
    out = {}
    for m in re.finditer(r'\["(\d{8})",\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)', text):
        ds, o, h, l, c, v = m.groups()
        try: dt = datetime.datetime.strptime(ds, "%Y%m%d").date()
        except Exception: continue
        o, h, l, c, v = float(o), float(h), float(l), float(c), float(v)
        out[dt] = [round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v / 1000)]
    return out


def fetch_yahoo(tk, frm, to):
    """Fallback for symbols Nasdaq's historical endpoint resolves to the wrong
    instrument (it has done so for AMZN). Close-only."""
    p1 = int(datetime.datetime.combine(frm, datetime.time()).timestamp())
    p2 = int(datetime.datetime.combine(to, datetime.time()).timestamp())
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}"
           f"?period1={p1}&period2={p2}&interval=1d")
    d = get_json(url, retries=2)
    res = (d.get("chart") or {}).get("result") or []
    if not res: return {}
    r0 = res[0]
    ts = r0.get("timestamp") or []
    q = ((r0.get("indicators") or {}).get("quote") or [{}])[0]
    out = {}
    for i, t in enumerate(ts):
        c = (q.get("close") or [None] * len(ts))[i]
        if c is None: continue
        o = (q.get("open") or [None] * len(ts))[i] or c
        h = (q.get("high") or [None] * len(ts))[i] or max(o, c)
        l = (q.get("low") or [None] * len(ts))[i] or min(o, c)
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        dt = datetime.datetime.utcfromtimestamp(t).date()
        out[dt] = [round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v / 1000)]
    return out


def fetch_one_series(tk, member, frm, to):
    """Dispatch to the right source/assetclass for this member."""
    src = member.get("source", "nasdaq_stock")
    if src == "naver_stock":
        return fetch_naver_history(tk, frm.isoformat(), to.isoformat())
    assetclass = {"nasdaq_stock": "stocks", "nasdaq_etf": "etf", "nasdaq_crypto": "crypto"}[src]
    return fetch_history(tk, frm.isoformat(), to.isoformat(), assetclass=assetclass)


def fetch_all_prices(members, workers):
    to = datetime.date.today()
    frm = to - datetime.timedelta(days=int(YEARS * 365.25) + 5)
    series, problems = {}, []

    def one(tk):
        try:
            rows = fetch_one_series(tk, members[tk], frm, to)
        except Exception as e:                        # noqa: BLE001
            return tk, {}, f"historical failed: {e}"
        if not rows:
            return tk, {}, "no rows"
        # Sanity gate: the historical series must agree with today's quote.
        # Nasdaq's endpoint has resolved a symbol to an unrelated instrument
        # before, and a silently wrong series would poison every correlation.
        # Skipped when quote_px is unknown (ETF/crypto/gold: no separate quote
        # call for those, so nothing to cross-check against, and none of them
        # share Nasdaq's stock-symbol-collision failure mode anyway).
        quote = members[tk].get("quote_px")
        last = rows[max(rows)][3]
        if quote and last and abs(last - quote) / quote > 0.25:
            return tk, rows, f"series/quote mismatch (hist {last} vs quote {quote})"
        return tk, rows, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tk, rows, problem in ex.map(one, list(members)):
            if problem and "mismatch" in problem:
                log(f"  ! {tk}: {problem} -> trying Yahoo")
                try:
                    alt = fetch_yahoo(tk, frm, to)
                except Exception as e:                # noqa: BLE001
                    alt = {}
                    log(f"    Yahoo failed too: {e}")
                if alt:
                    series[tk] = alt
                    problems.append((tk, "nasdaq symbol mismatch — using Yahoo"))
                    continue
                problems.append((tk, problem + " — excluded"))
                continue
            if problem:
                problems.append((tk, problem))
                log(f"  ! {tk}: {problem}")
                continue
            series[tk] = rows
    log(f"prices: {len(series)}/{len(members)} tickers")

    # Backfill size/quote for assets whose membership source had none:
    #  - ETF / gold: no AUM endpoint, so approximate size with recent dollar
    #    turnover (median close x volume over the trailing ~60 sessions) —
    #    a liquidity proxy, not literal fund size, but a reasonable ordering.
    #  - crypto: circulating_supply x latest close IS a real market cap.
    for tk, m in members.items():
        rows = series.get(tk)
        if not rows or m.get("market_cap"):
            continue
        by_date = sorted(rows.items())
        last_close = by_date[-1][1][3]
        m["quote_px"] = last_close
        if m.get("asset_class") == "crypto" and m.get("circulating_supply"):
            m["market_cap"] = last_close * m["circulating_supply"]
        else:
            recent = by_date[-60:]
            turnovers = sorted(row[3] * row[4] * 1000 for _, row in recent)
            m["market_cap"] = turnovers[len(turnovers) // 2] if turnovers else 0.0

    return series, problems


# --------------------------------------------------------------------------
# 4. analytics
# --------------------------------------------------------------------------
def pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx <= 0 or sy <= 0: return None
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / math.sqrt(sx * sy)


def build_returns(dates, closes):
    r, prev = [None] * len(dates), None
    for i, px in enumerate(closes):
        if px is None: continue
        if prev is not None and prev > 0:
            r[i] = max(-CLIP, min(CLIP, (px - prev) / prev))
        prev = px
    return r


PERIODS = [
    {"key": "1m", "label": "1개월", "days": 31},
    {"key": "3m", "label": "3개월", "days": 92},
    {"key": "6m", "label": "6개월", "days": 183},
    {"key": "1y", "label": "1년",   "days": 365},
    {"key": "3y", "label": "3년",   "days": 1096},
    {"key": "5y", "label": "5년",   "days": 1827},
]


def corr_matrix(tickers, rets, dates, min_overlap=MIN_OVERLAP):
    n = len(tickers)
    m = [[None]*n for _ in range(n)]
    for i in range(n):
        m[i][i] = 1.0
        ri = rets[tickers[i]]
        for j in range(i+1, n):
            rj = rets[tickers[j]]
            xs, ys = [], []
            for k in range(len(dates)):
                if ri[k] is not None and rj[k] is not None:
                    xs.append(ri[k]); ys.append(rj[k])
            if len(xs) < min_overlap: continue
            r = pearson(xs, ys)
            if r is not None:
                m[i][j] = m[j][i] = round(max(-1.0, min(1.0, r)), 3)
    return m


def compute_factor_betas(tickers, dates, rets, factors):
    """Each ticker's sensitivity to every macro factor, over the most recent year."""
    cutoff = dates[-1] - datetime.timedelta(days=365)
    idx = [i for i, d in enumerate(dates) if d > cutoff]
    out = {}
    for t in tickers:
        rt = rets[t]
        row = {}
        for f in MACRO_FACTORS:
            fr = factors.get(f["key"])
            if not fr: continue
            xs, ys = [], []
            for i in idx:
                fv, sv = fr.get(dates[i]), rt[i]
                if fv is None or sv is None: continue
                xs.append(fv); ys.append(sv)
            beta, tstat = ols_beta(xs, ys)
            if beta is not None:
                row[f["key"]] = {"b": beta, "t": tstat, "n": len(xs)}
        if row: out[t] = row
    return out


def compute_macro_monthly(tickers, dates, closes, bls):
    """Monthly-return sensitivity to the BLS releases.

    A release describes the PREVIOUS month, so the macro change for month M is
    matched against the stock's return in month M+1 — the window in which the
    market actually learns it. Sample is ~60 months, so every result ships with
    its n and the reader is told the noise band."""
    if not bls: return {}, {}
    months = []
    seen = set()
    for d in dates:
        k = f"{d.year}-{d.month:02d}"
        if k not in seen: seen.add(k); months.append(k)
    # monthly stock returns
    mret = {}
    for t in tickers:
        first, last = {}, {}
        for i, d in enumerate(dates):
            v = closes[t][i]
            if v is None: continue
            k = f"{d.year}-{d.month:02d}"
            first.setdefault(k, v); last[k] = v
        mret[t] = {k: (last[k]-first[k])/first[k]*100 for k in last if first.get(k)}
    # macro month-over-month change, shifted one month forward (release timing)
    mchg = {}
    for b in BLS_SERIES:
        vals = bls.get(b["key"])
        if not vals: continue
        ks = sorted(vals)
        ch = {}
        for i in range(1, len(ks)):
            prev, cur = vals[ks[i-1]], vals[ks[i]]
            if b["diff"] == "pct":
                if prev: ch[ks[i]] = (cur-prev)/prev*100
            else:
                ch[ks[i]] = cur-prev
        shifted = {}
        for k, v in ch.items():
            y, mo = map(int, k.split("-"))
            y, mo = (y+1, 1) if mo == 12 else (y, mo+1)
            shifted[f"{y}-{mo:02d}"] = v
        mchg[b["key"]] = shifted

    out = {}
    for t in tickers:
        row = {}
        for key, ch in mchg.items():
            common = sorted(set(mret[t]) & set(ch))
            if len(common) < 24: continue
            r = pearson([ch[k] for k in common], [mret[t][k] for k in common])
            if r is not None: row[key] = {"r": round(r, 3), "n": len(common)}
        if row: out[t] = row
    series_out = {b["key"]: {"label": b["label"], "values": bls.get(b["key"], {})}
                  for b in BLS_SERIES if bls.get(b["key"])}
    return out, series_out


PROFILE_YEARS = 3


def weekly_returns(dates, closes, i0):
    """Week-over-week returns keyed by ISO year-week, using each week's last
    available close. Returns {} when the series is empty."""
    if not closes: return {}
    last = {}
    for i in range(i0, len(dates)):
        v = closes[i]
        if v is None: continue
        y, w, _ = dates[i].isocalendar()
        last[(y, w)] = v
    keys = sorted(last)
    out = {}
    for a, b in zip(keys, keys[1:]):
        pa = last[a]
        if pa: out[b] = (last[b] - pa) / pa
    return out


def compute_profiles(dates, closes, rets, tickers, bench):
    """The long-run character of each asset over the last 3 years.

    Four numbers, all read off the same window so they are comparable:
      cagr  연평균 성장률
      r2    log(종가)를 시간에 회귀했을 때의 결정계수 — 우상향이 '꾸준했는가'.
            수익률이 아무리 커도 경로가 들쭉날쭉하면 낮게 나온다.
      vol   연변동성
      mdd   최대 낙폭
      dcap  하락장 방어력 = 벤치마크가 내린 주들만 모아 (내 누적 / 벤치 누적).
            1보다 작으면 시장이 빠질 때 덜 빠졌다는 뜻.

    `bench` is SPY's close series when available: "하락장" means the broad risk
    market falling, and the same yardstick has to apply to every asset or the
    ratios are not comparable across markets.

    Down-capture is measured WEEKLY, not daily. Seoul closes hours before New
    York, so a KOSPI name's same-day return reflects a session that ended
    before the US fell; weekly buckets absorb that offset and let one number
    compare across three markets.
    """
    cut = dates[-1] - datetime.timedelta(days=365 * PROFILE_YEARS)
    i0 = next((i for i, d in enumerate(dates) if d > cut), 0)
    bench_wk = weekly_returns(dates, bench, i0) if bench else None
    out = {}
    for t in tickers:
        px = [(i, v) for i, v in enumerate(closes[t][i0:], start=i0) if v is not None]
        if len(px) < 120:                      # under ~6 months there is no "character"
            continue
        first, last = px[0][1], px[-1][1]
        n = len(px)
        years = (dates[px[-1][0]] - dates[px[0][0]]).days / 365.25
        rec = {"n": n, "years": round(years, 2)}

        if first > 0 and years >= 0.5:
            rec["cagr"] = round(((last / first) ** (1 / years) - 1) * 100, 1)
            # consistency of the climb, not its size
            ys = [math.log(v) for _, v in px]
            xs = list(range(n))
            mx, my = sum(xs) / n, sum(ys) / n
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            sxx = sum((x - mx) ** 2 for x in xs)
            syy = sum((y - my) ** 2 for y in ys)
            rec["r2"] = round(sxy * sxy / (sxx * syy), 3) if sxx and syy else None

        r = [v for v in rets[t][i0:] if v is not None]
        if len(r) >= 60:
            mu = sum(r) / len(r)
            var = sum((x - mu) ** 2 for x in r) / (len(r) - 1)
            rec["vol"] = round(var ** 0.5 * (252 ** 0.5) * 100, 1)

        peak, mdd = None, 0.0
        for _, v in px:
            peak = v if peak is None else max(peak, v)
            if peak: mdd = min(mdd, (v - peak) / peak)
        rec["mdd"] = round(mdd * 100, 1)

        if bench_wk:
            mine_wk = weekly_returns(dates, closes[t], i0)
            mine, theirs, weeks = 0.0, 0.0, 0
            for k, b in bench_wk.items():
                m = mine_wk.get(k)
                if b >= 0 or m is None: continue
                mine += m; theirs += b; weeks += 1
            if weeks >= 20 and theirs < -0.05:      # need a real down-market sample
                rec["dcap"] = round(mine / theirs, 2)
                rec["dweeks"] = weeks
        out[t] = rec
    return out


def analyse(series, members, themes, index_date, nasdaq_tickers, factors=None, bls=None):
    dates = sorted({d for rows in series.values() for d in rows})
    di = {d: i for i, d in enumerate(dates)}
    tickers = sorted(series, key=lambda t: -members[t]["market_cap"])

    ohlc = {t: [None] * len(dates) for t in tickers}
    closes = {t: [None] * len(dates) for t in tickers}
    for t in tickers:
        for d, row in series[t].items():
            ohlc[t][di[d]] = row
            closes[t][di[d]] = row[3]

    rets5y = {t: build_returns(dates, closes[t]) for t in tickers}

    # ---- 1-year window: network + charts ----
    cutoff = dates[-1] - datetime.timedelta(days=CORR_WINDOW_DAYS)
    w0 = next((i for i, d in enumerate(dates) if d > cutoff), 0)
    w_dates = dates[w0:]
    w_close = {t: closes[t][w0:] for t in tickers}
    w_rets = {t: rets5y[t][w0:] for t in tickers}

    stats = {}
    for t in tickers:
        vals = [(i, v) for i, v in enumerate(w_close[t]) if v is not None]
        if not vals: continue
        rr = [v for v in w_rets[t] if v is not None]
        mean = sum(rr) / len(rr) if rr else 0
        vol = (sum((x - mean) ** 2 for x in rr) / max(1, len(rr) - 1)) ** 0.5 * math.sqrt(252) * 100 if len(rr) > 1 else 0
        stats[t] = {
            "days": len(vals),
            "first_date": w_dates[vals[0][0]].isoformat(),
            "last_date": w_dates[vals[-1][0]].isoformat(),
            "last_close": round(vals[-1][1], 2),
            "ret_1y": round((vals[-1][1] - vals[0][1]) / vals[0][1] * 100, 1),
            "vol": round(vol, 1),
            "market_cap": members[t]["market_cap"],
        }
    tickers = [t for t in tickers if t in stats]

    n = len(tickers)
    matrix = corr_matrix(tickers, w_rets, w_dates)

    # the same network at other look-backs, so the period control has data
    period_matrices, period_meta = {}, []
    for pd_ in PERIODS:
        if pd_["days"] == CORR_WINDOW_DAYS:
            period_matrices[pd_["key"]] = matrix
            p_from, p_to = w_dates[0], w_dates[-1]
        else:
            cut = dates[-1] - datetime.timedelta(days=pd_["days"])
            i0 = next((i for i, d in enumerate(dates) if d > cut), 0)
            p_dates = dates[i0:]
            p_rets = {t: rets5y[t][i0:] for t in tickers}
            # a 3-month window has ~63 sessions: the 1y minimum would void it
            min_ov = min(MIN_OVERLAP, max(20, int(len(p_dates) * 0.25)))
            period_matrices[pd_["key"]] = corr_matrix(tickers, p_rets, p_dates, min_ov)
            p_from, p_to = p_dates[0], p_dates[-1]
        period_meta.append({"key": pd_["key"], "label": pd_["label"],
                            "from": p_from.isoformat(), "to": p_to.isoformat()})
    log(f"correlation matrices: {', '.join(p['label'] for p in period_meta)}")

    corr = {
        "tickers": tickers,
        "stats": stats,
        "matrix": matrix,
        "periods": period_meta,
        "period_matrix": period_matrices,
        "meta": {
            "range_from": stats[tickers[0]]["first_date"] if tickers else "",
            "range_to": max(s["last_date"] for s in stats.values()) if stats else "",
            "method": f"일별 종가 수익률의 피어슨 상관계수 (공통 거래일 기준, 최소 {MIN_OVERLAP}일 중복 필요, 일간수익률 ±{int(CLIP*100)}% 윈저화)",
            "sources": "api.nasdaq.com (미국주식·ETF·암호화폐), finance.naver.com (코스피100)",
        },
    }

    # ---- lead-lag profiles + co-move rates (1y window) ----
    def lag_corr(a, b, k):
        xs, ys = [], []
        for t in range(len(a)):
            s = t - k
            if s < 0 or s >= len(b) or a[t] is None or b[s] is None: continue
            xs.append(a[t]); ys.append(b[s])
        if len(xs) < MIN_OVERLAP: return None, len(xs)
        return pearson(xs, ys), len(xs)

    def rates(x, y):
        yv = [v for v in y if v is not None]
        if not yv: return None
        base = sum(1 for v in yv if v > 0) / len(yv)
        s_n = s_u = nx_n = nx_u = 0
        for t in range(len(x)):
            if x[t] is None or x[t] <= 0: continue
            if y[t] is not None:
                s_n += 1; s_u += 1 if y[t] > 0 else 0
            if t + 1 < len(y) and y[t + 1] is not None:
                nx_n += 1; nx_u += 1 if y[t + 1] > 0 else 0
        if s_n < 30 or nx_n < 30: return None
        return [round(base, 3), round(s_u / s_n, 3), round(nx_u / nx_n, 3), nx_n]

    profiles, comove, noise = {}, {}, {}
    for i in range(n):
        a = tickers[i]
        for j in range(i + 1, n):
            b = tickers[j]
            if matrix[i][j] is None or abs(matrix[i][j]) < PAIR_MIN_R: continue
            key = "|".join(sorted([a, b]))
            flip = key.split("|")[0] != a
            prof, nn = [], 0
            for k in range(-MAX_LAG, MAX_LAG + 1):
                v, m = lag_corr(w_rets[a], w_rets[b], k)
                prof.append(None if v is None else round(v, 3))
                if k == 0: nn = m
            profiles[key] = list(reversed(prof)) if flip else prof
            noise[key] = nn
            ab, ba = rates(w_rets[a], w_rets[b]), rates(w_rets[b], w_rets[a])
            if ab and ba:
                comove[key] = {"ab": ba, "ba": ab} if flip else {"ab": ab, "ba": ba}

    px = {
        "dates": [d.isoformat() for d in w_dates],
        "prices": {t: [round(v, 2) if v is not None else None for v in w_close[t]] for t in tickers},
        "max_lag": MAX_LAG, "profiles": profiles, "comove": comove, "noise": noise,
    }

    oh = {
        "dates": [d.isoformat() for d in dates],
        "data": {t: ohlc[t] for t in tickers},
        "closeOnly": [t for t in tickers if all(
            (row is None or (row[0] == row[1] == row[2] == row[3])) for row in ohlc[t] if row)],
    }

    profiles = compute_profiles(dates, closes, rets5y, tickers, closes.get("SPY"))
    log(f"profiles: {len(profiles)} assets characterised over {PROFILE_YEARS}y")
    corr["profile"] = profiles

    # 지수/주도주 stays Nasdaq-100-only — see module docstring
    nd_scope = [t for t in tickers if t in nasdaq_tickers]
    leaders = latest_session_leaders(dates, ohlc, nd_scope, members)

    betas = compute_factor_betas(tickers, dates, rets5y, factors or {})
    macro_month, macro_series = compute_macro_monthly(tickers, dates, closes, bls or {})
    macro = {
        "factors": [{"key": f["key"], "label": f["label"], "proxy": f["proxy"], "note": f["note"]}
                    for f in MACRO_FACTORS if (factors or {}).get(f["key"])],
        "betas": betas,
        "monthly": macro_month,
        "indicators": macro_series,
        "window": "최근 1년 일간 수익률 회귀",
    }
    log(f"macro: {len(betas)} tickers with factor betas, {len(macro_month)} with monthly macro")
    return corr, px, oh, leaders, macro


def latest_session_leaders(dates, ohlc, tickers, members, top=6):
    """Who moved the index on the most recent session.

    `chg` is the plain close-to-close move; `contrib` weights it by the share
    of total market cap, which is what actually pushed the index around.

    `dates` is the shared axis across every universe in the build (KOSPI closes
    before the US session, crypto trades weekends), so its last index is not
    necessarily a Nasdaq-100 trading day. Walk back to the most recent date at
    least half of `tickers` actually has a close for."""
    if len(dates) < 2 or not tickers: return {}
    last = len(dates) - 1
    need = max(1, len(tickers) // 2)
    while last > 0 and sum(1 for t in tickers if ohlc[t][last] is not None) < need:
        last -= 1
    total_cap = sum(members[t]["market_cap"] for t in tickers) or 1
    rows, index_chg = [], 0.0
    for t in tickers:
        cur = ohlc[t][last]
        prev_i = last - 1
        while prev_i >= 0 and ohlc[t][prev_i] is None: prev_i -= 1
        if cur is None or prev_i < 0: continue
        prev = ohlc[t][prev_i]
        if not prev[3]: continue
        chg = (cur[3] - prev[3]) / prev[3] * 100
        w = members[t]["market_cap"] / total_cap
        contrib = chg * w
        index_chg += contrib
        rows.append({"t": t, "chg": round(chg, 2), "contrib": round(contrib, 4),
                     "close": cur[3], "vol": cur[4]})
    rows.sort(key=lambda r: -r["chg"])
    return {
        "date": dates[last].isoformat(),
        "index_chg": round(index_chg, 2),
        "advancers": sum(1 for r in rows if r["chg"] > 0),
        "decliners": sum(1 for r in rows if r["chg"] < 0),
        "up": rows[:top],
        "down": list(reversed(rows[-top:])),
        "by_contrib_up": sorted(rows, key=lambda r: -r["contrib"])[:top],
        "by_contrib_down": sorted(rows, key=lambda r: r["contrib"])[:top],
    }


def render(corr, px, oh, leaders, fund, macro, members, themes, index_date, problems, out_dir, usdkrw=None):
    tpl = open(os.path.join(ROOT, "build", "template.html")).read()
    engine = open(os.path.join(ROOT, "build", "candle_engine.js")).read()
    cfg = load_theme_config()

    def div_yield(t):
        """US names carry it as a string on the fundamentals record ("0.45%");
        KOSPI names get it from the Naver page the industry lookup already reads."""
        v = ((fund or {}).get(t) or {}).get("yield")
        if isinstance(v, str):
            m = re.search(r"([\d.]+)", v)
            if m: return float(m.group(1))
        return members[t].get("div_yield")

    nodes = []
    for t in corr["tickers"]:
        m = members[t]
        nodes.append({"id": t, "name": m["name"], "group": themes.get(t, 9),
                      "cap": m["market_cap"], "currency": m.get("currency", "USD"),
                      "assetClass": m.get("asset_class", "us_stock"),
                      "yield": div_yield(t)})
    meta = {
        "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "index_date": index_date,
        "count": len(nodes),
        "problems": [{"ticker": t, "note": p} for t, p in problems],
        "themes": {g: cfg["themes"][g]["label"] for g in cfg["themes"]},
        "leaders": leaders,
        "fx": {"KRW": usdkrw},          # 거래대금을 달러로 환산할 때 쓴다
    }
    auth_path = os.path.join(ROOT, "data", "auth.json")
    auth = json.load(open(auth_path)) if os.path.exists(auth_path) else {}
    auth = {k: v for k, v in auth.items() if not k.startswith("_")}
    have = [k for k, v in auth.items() if isinstance(v, dict) and any(
        vv for kk, vv in v.items() if not kk.startswith("_"))]
    log(f"auth providers configured: {', '.join(have) if have else '(none — 비회원 모드만)'}")

    blob = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False)
    body = (tpl.replace("/*__CANDLE_ENGINE__*/", engine, 1)
               .replace("/*__CORR__*/", blob(corr))
               .replace("/*__PX__*/", blob(px))
               .replace("/*__OH__*/", blob(oh))
               .replace("/*__NODES__*/", blob(nodes))
               .replace("/*__FUND__*/", blob(fund))
               .replace("/*__MACRO__*/", blob(macro))
               .replace("/*__META__*/", blob(meta))
               .replace("/*__AUTH__*/", blob(auth)))

    # The template is written for the Artifact wrapper, which supplies the
    # document head. A standalone site has to bring its own — without the
    # viewport meta, phones lay the page out at 980px and zoom out.
    # Logo: the node network the dashboard itself draws — a hub with three
    # connected satellites. No lettermark; the graph IS the mark.
    icon = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
            "<rect width='32' height='32' rx='7' fill='%230a0c10'/>"
            "<g stroke='%237e8794' stroke-width='1.5'>"
            "<line x1='16' y1='16.5' x2='8.5' y2='8.5'/>"
            "<line x1='16' y1='16.5' x2='24.5' y2='11'/>"
            "<line x1='16' y1='16.5' x2='11' y2='25'/>"
            "<line x1='8.5' y1='8.5' x2='24.5' y2='11'/></g>"
            "<circle cx='16' cy='16.5' r='4' fill='%23e8eaed'/>"
            "<circle cx='8.5' cy='8.5' r='3.1' fill='%233987e5'/>"
            "<circle cx='24.5' cy='11' r='3.1' fill='%233ddc84'/>"
            "<circle cx='11' cy='25' r='3.1' fill='%23f7931a'/></svg>")
    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="description" content="머니 커넥션 — 나스닥100·코스피100·주요 ETF·비트코인·이더리움·금이 서로 어떻게 연결돼 움직이는지 보여주는 상관관계 네트워크. 테마 로테이션·계절성·봉차트, 매일 자동 갱신.">
<meta property="og:title" content="머니 커넥션">
<meta property="og:description" content="돈이 어디에서 어디로 흐르는지 — 나스닥100 · 코스피100 · 미국 ETF · 암호화폐 · 금을 한 네트워크에서 비교합니다. 매일 자동 갱신.">
<link rel="icon" href="{icon}">
</head>
<body>
{body}
</body>
</html>
"""
    os.makedirs(out_dir, exist_ok=True)
    logo_src = os.path.join(ROOT, "logo.svg")          # ship the mark alongside the page
    if os.path.exists(logo_src):
        open(os.path.join(out_dir, "logo.svg"), "w").write(open(logo_src).read())
    path = os.path.join(out_dir, "index.html")
    open(path, "w").write(html)
    log(f"wrote {path}  {os.path.getsize(path)/1024/1024:.2f} MB")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--cache", default=os.path.join(ROOT, "data"))
    ap.add_argument("--max-workers", type=int, default=4)
    ap.add_argument("--fundamentals-refresh", type=int, default=25,
                    help="how many tickers refresh their statements per run")
    a = ap.parse_args()

    cfg = load_theme_config()

    # ---- membership: Nasdaq-100 + KOSPI100 + ETF/crypto/gold ----
    nasdaq_members, index_date = fetch_constituents()
    nasdaq_tickers = set(nasdaq_members)
    usdkrw = fetch_usdkrw_rate()
    kospi_members = fetch_kospi100(usdkrw)
    extra_members = fetch_extra_assets()

    members = {}
    members.update(nasdaq_members)
    members.update(kospi_members)
    members.update(extra_members)
    log(f"total universe: {len(members)} "
        f"({len(nasdaq_members)} Nasdaq-100 + {len(kospi_members)} KOSPI100 + {len(extra_members)} ETF/crypto/gold)")

    # theme: Nasdaq-100 and KOSPI100 both classify into the same 8 sector
    # themes (KOSPI via its own WICS industry label -> theme mapping); ETF/
    # crypto/gold get one fixed theme each (11/12/13) since "industry" is
    # not a meaningful concept for those instrument types.
    themes, _ = resolve_themes(list(nasdaq_members), cfg, os.path.join(a.cache, "sectors.json"))
    themes.update(resolve_kospi_themes(kospi_members, cfg, os.path.join(a.cache, "kospi_industries.json")))
    themes.update({t: (12 if m["asset_class"] == "crypto" else 13 if m["asset_class"] == "commodity" else 11)
                   for t, m in extra_members.items()})

    series, problems = fetch_all_prices(members, a.max_workers)

    _to = datetime.date.today()
    _frm = _to - datetime.timedelta(days=int(YEARS * 365.25) + 5)
    factors = fetch_macro_factors(_frm, _to)
    bls = fetch_bls_macro()

    if len(series) < 0.8 * len(members):
        log(f"ABORT: only {len(series)}/{len(members)} tickers fetched — refusing to publish a thin build")
        sys.exit(1)

    corr, px, oh, leaders, macro = analyse(series, members, themes, index_date,
                                                  nasdaq_tickers, factors, bls)
    log(f"analysed: {len(corr['tickers'])} tickers, {len(px['profiles'])} lag profiles")
    log(f"latest session {leaders.get('date')}: 나스닥100 지수 {leaders.get('index_chg')}% "
        f"({leaders.get('advancers')} up / {leaders.get('decliners')} down)")

    # fundamentals (PER/EPS/재무제표) only exist for the original Nasdaq-100
    # equities — Nasdaq's financials endpoints do not cover KOSPI/ETF/crypto/gold
    fund_scope = [t for t in corr["tickers"] if t in nasdaq_tickers]
    fund = fetch_fundamentals(fund_scope, os.path.join(a.cache, "fundamentals.json"),
                              a.fundamentals_refresh, a.max_workers)
    render(corr, px, oh, leaders, fund, macro, members, themes, index_date, problems, a.out, usdkrw)


if __name__ == "__main__":
    main()
