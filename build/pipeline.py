#!/usr/bin/env python3
"""Nasdaq-100 dashboard build pipeline.

One run does everything:
  1. current index membership          (api.nasdaq.com list-type/nasdaq100)
  2. sector/industry for new tickers   (api.nasdaq.com quote/<tk>/summary)  -> theme
  3. 5 years of daily OHLCV per ticker (api.nasdaq.com quote/<tk>/historical)
  4. correlation / lead-lag / rotation analytics
  5. render index.html from the template

Membership, market caps and prices all come from the same daily pull, so an
index change (add, drop, ticker rename) flows through without hand edits.

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
CORR_WINDOW = 252    # correlation/network uses the most recent year


def log(msg): print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


def get_json(url, retries=4, timeout=30):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:                       # noqa: BLE001 - any failure is retryable here
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
        out[tk] = {"name": name, "market_cap": cap, "quote_px": px}
    log(f"index members: {len(out)} (as of {d['data'].get('date')})")
    return out, d["data"].get("date")


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
# 3. prices
# --------------------------------------------------------------------------
def parse_money(v):
    if v is None: return None
    s = str(v).strip().replace("$", "").replace(",", "")
    if s in ("", "N/A", "n/a"): return None
    try: return float(s)
    except ValueError: return None


def fetch_history(tk, frm, to):
    url = (f"https://api.nasdaq.com/api/quote/{tk}/historical"
           f"?assetclass=stocks&fromdate={frm}&todate={to}&limit=99999")
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


def fetch_all_prices(members, workers):
    to = datetime.date.today()
    frm = to - datetime.timedelta(days=int(YEARS * 365.25) + 5)
    series, problems = {}, []

    def one(tk):
        try:
            rows = fetch_history(tk, frm.isoformat(), to.isoformat())
        except Exception as e:                        # noqa: BLE001
            return tk, {}, f"historical failed: {e}"
        if not rows:
            return tk, {}, "no rows"
        # Sanity gate: the historical series must agree with today's quote.
        # Nasdaq's endpoint has resolved a symbol to an unrelated instrument
        # before, and a silently wrong series would poison every correlation.
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


def analyse(series, members, themes, index_date):
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
    w0 = max(0, len(dates) - CORR_WINDOW)
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
    matrix = [[None] * n for _ in range(n)]
    for i in range(n):
        matrix[i][i] = 1.0
        ri = w_rets[tickers[i]]
        for j in range(i + 1, n):
            rj = w_rets[tickers[j]]
            xs, ys = [], []
            for k in range(len(w_dates)):
                if ri[k] is not None and rj[k] is not None:
                    xs.append(ri[k]); ys.append(rj[k])
            if len(xs) < MIN_OVERLAP: continue
            r = pearson(xs, ys)
            if r is not None:
                matrix[i][j] = matrix[j][i] = round(max(-1.0, min(1.0, r)), 3)

    corr = {
        "tickers": tickers,
        "stats": stats,
        "matrix": matrix,
        "meta": {
            "range_from": stats[tickers[0]]["first_date"] if tickers else "",
            "range_to": max(s["last_date"] for s in stats.values()) if stats else "",
            "method": f"일별 종가 수익률의 피어슨 상관계수 (공통 거래일 기준, 최소 {MIN_OVERLAP}일 중복 필요, 일간수익률 ±{int(CLIP*100)}% 윈저화)",
            "sources": "api.nasdaq.com historical quotes",
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

    cycle = analyse_cycle(dates, rets5y, tickers, themes)
    return corr, px, oh, cycle


def analyse_cycle(dates, rets, tickers, themes):
    cfg = load_theme_config()
    groups = {}
    for t in tickers:
        groups.setdefault(str(themes.get(t, 9)), []).append(t)
    groups = {g: m for g, m in groups.items() if m}

    theme_ret, theme_idx = {}, {}
    for g, members in groups.items():
        tr, lvl, idx = [], 100.0, []
        for i in range(len(dates)):
            vals = [rets[t][i] for t in members if rets[t][i] is not None]
            v = sum(vals) / len(vals) if vals else None
            tr.append(v)
            if v is not None: lvl *= (1 + v)
            idx.append(round(lvl, 2))
        theme_ret[g], theme_idx[g] = tr, idx

    mkt, lvl, mkt_idx = [], 100.0, []
    for i in range(len(dates)):
        vals = [rets[t][i] for t in tickers if rets[t][i] is not None]
        v = sum(vals) / len(vals) if vals else None
        mkt.append(v)
        if v is not None: lvl *= (1 + v)
        mkt_idx.append(round(lvl, 2))

    months = sorted({(d.year, d.month) for d in dates})
    mkeys = [f"{y}-{m:02d}" for y, m in months]
    mi = {k: i for i, k in enumerate(mkeys)}

    def monthly(series_ret):
        acc, seen = [1.0] * len(mkeys), [0] * len(mkeys)
        for i, d in enumerate(dates):
            v = series_ret[i]
            if v is None: continue
            k = mi[f"{d.year}-{d.month:02d}"]
            acc[k] *= (1 + v); seen[k] += 1
        return [round((acc[i] - 1) * 100, 2) if seen[i] >= 10 else None for i in range(len(mkeys))]

    theme_month = {g: monthly(theme_ret[g]) for g in groups}
    mkt_month = monthly(mkt)

    leaders = []
    for i in range(len(mkeys)):
        vals = [(theme_month[g][i], g) for g in groups if theme_month[g][i] is not None]
        leaders.append(max(vals)[1] if vals else None)

    trans = {a: {b: 0 for b in groups} for a in groups}
    for i in range(len(leaders) - 1):
        a, b = leaders[i], leaders[i + 1]
        if a and b: trans[a][b] += 1

    weeks = {}
    for i, d in enumerate(dates):
        weeks.setdefault(d.isocalendar()[:2], []).append(i)
    wkeys = sorted(weeks)

    def weekly(series_ret):
        out = []
        for wk in wkeys:
            acc, seen = 1.0, 0
            for i in weeks[wk]:
                v = series_ret[i]
                if v is not None: acc *= (1 + v); seen += 1
            out.append(acc - 1 if seen >= 3 else None)
        return out

    tw = {g: weekly(theme_ret[g]) for g in groups}
    MAXL = 4

    def lc(a, b, k):
        xs, ys = [], []
        for t in range(len(a)):
            s = t - k
            if s < 0 or s >= len(b) or a[t] is None or b[s] is None: continue
            xs.append(a[t]); ys.append(b[s])
        if len(xs) < 30: return None
        r = pearson(xs, ys)
        return None if r is None else round(r, 3)

    gs = sorted(groups, key=int)
    lead = {f"{a}|{b}": [lc(tw[a], tw[b], k) for k in range(-MAXL, MAXL + 1)]
            for ai, a in enumerate(gs) for b in gs[ai + 1:]}

    lead_ex, worst_ex = [], []
    for i in range(len(mkeys) - 1):
        if not leaders[i] or mkt_month[i + 1] is None: continue
        nxt = {g: theme_month[g][i + 1] for g in groups if theme_month[g][i + 1] is not None}
        cur = {g: theme_month[g][i] for g in groups if theme_month[g][i] is not None}
        if not nxt or not cur: continue
        if leaders[i] in nxt: lead_ex.append(nxt[leaders[i]] - mkt_month[i + 1])
        worst = min(cur, key=cur.get)
        if worst in nxt: worst_ex.append(nxt[worst] - mkt_month[i + 1])

    def tstat(xs):
        if len(xs) < 3: return 0.0, 0.0, len(xs)
        m = sum(xs) / len(xs)
        sd = (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5
        return round(m, 2), (round(m / (sd / math.sqrt(len(xs))), 2) if sd else 0.0), len(xs)

    same = sum(trans[a][a] for a in groups)
    tot = sum(trans[a][b] for a in groups for b in groups)
    p0 = 1 / max(1, len(groups))
    z = ((same - tot * p0) / ((tot * p0 * (1 - p0)) ** 0.5)) if tot else 0
    lm, lt, ln = tstat(lead_ex)
    wm, wt, wn = tstat(worst_ex)

    return {
        "stats": {
            "persist": {"same": same, "total": tot, "pct": round(same / tot * 100) if tot else 0,
                        "random": round(p0 * 100), "z": round(z, 2)},
            "leader_next": {"excess": lm, "t": lt, "n": ln,
                            "winrate": round(sum(1 for x in lead_ex if x > 0) / ln * 100) if ln else 0},
            "worst_next": {"excess": wm, "t": wt, "n": wn},
            "lag0_dominant": all(
                (p[MAXL] is not None) and all(abs(p[k + MAXL]) <= abs(p[MAXL])
                                              for k in range(-MAXL, MAXL + 1) if p[k + MAXL] is not None)
                for p in lead.values()) if lead else True,
        },
        "dates": [d.isoformat() for d in dates],
        "themes": {g: {"label": cfg["themes"][g]["label"], "index": theme_idx[g], "members": groups[g]}
                   for g in gs},
        "market_index": mkt_idx,
        "months": mkeys,
        "theme_month": theme_month,
        "market_month": mkt_month,
        "leaders": leaders,
        "transitions": trans,
        "weeks": len(wkeys),
        "max_lag": MAXL,
        "lead": lead,
        "range": [dates[0].isoformat(), dates[-1].isoformat()],
    }


# --------------------------------------------------------------------------
# 5. render
# --------------------------------------------------------------------------
def render(corr, px, oh, cycle, members, themes, index_date, problems, out_dir):
    tpl = open(os.path.join(ROOT, "build", "template.html")).read()
    engine = open(os.path.join(ROOT, "build", "candle_engine.js")).read()
    cfg = load_theme_config()

    nodes = []
    for t in corr["tickers"]:
        nodes.append({"id": t, "name": members[t]["name"], "group": themes.get(t, 9),
                      "cap": members[t]["market_cap"]})
    meta = {
        "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "index_date": index_date,
        "count": len(nodes),
        "problems": [{"ticker": t, "note": p} for t, p in problems],
        "themes": {g: cfg["themes"][g]["label"] for g in cfg["themes"]},
    }
    blob = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False)
    html = (tpl.replace("/*__CANDLE_ENGINE__*/", engine, 1)
               .replace("/*__CORR__*/", blob(corr))
               .replace("/*__PX__*/", blob(px))
               .replace("/*__CY__*/", blob(cycle))
               .replace("/*__OH__*/", blob(oh))
               .replace("/*__NODES__*/", blob(nodes))
               .replace("/*__META__*/", blob(meta)))
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    open(path, "w").write(html)
    log(f"wrote {path}  {os.path.getsize(path)/1024/1024:.2f} MB")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--cache", default=os.path.join(ROOT, "data"))
    ap.add_argument("--max-workers", type=int, default=4)
    a = ap.parse_args()

    cfg = load_theme_config()
    members, index_date = fetch_constituents()
    themes, _ = resolve_themes(list(members), cfg, os.path.join(a.cache, "sectors.json"))
    series, problems = fetch_all_prices(members, a.max_workers)

    if len(series) < 0.8 * len(members):
        log(f"ABORT: only {len(series)}/{len(members)} tickers fetched — refusing to publish a thin build")
        sys.exit(1)

    corr, px, oh, cycle = analyse(series, members, themes, index_date)
    log(f"analysed: {len(corr['tickers'])} tickers, {len(px['profiles'])} lag profiles, "
        f"{len(cycle['themes'])} themes, {len(cycle['months'])} months")
    render(corr, px, oh, cycle, members, themes, index_date, problems, a.out)


if __name__ == "__main__":
    main()
