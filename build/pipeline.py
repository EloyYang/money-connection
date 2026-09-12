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
import argparse, datetime, io, json, math, os, re, sys, time, urllib.error, urllib.request, zipfile
import xml.etree.ElementTree as ET
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


SP500_MIN_MEMBERS = 480    # 진짜 목록이면 500 안팎 — 이보다 한참 적게 나오면
                           # 소스가 바뀌었거나 잘못 읽은 것으로 본다

# 정상 티커 형태만 받는다: 1~5글자 알파벳, 필요하면 점 하나 + 클래스 한 글자
# (BRK.B, BF.B 같은 복수 클래스). SPY 보유 목록에는 인수합병 등으로 생기는
# "CONTRA ..." 같은 잔여 포지션이 가짜 티커로 섞여 나올 때가 있어 걸러낸다.
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")


def _clean_spy_name(raw):
    """SPY 자료의 회사명은 전부 대문자다. "CL A"/"CLASS B" 같은 클래스
    표기만 떼고 제목 표기로 바꾼다 — 그 이상 손대면(JPMorgan, McDonald's
    같은 흔한 예외를 다 맞추려다) 오히려 틀린 이름을 자신 있게 보여주는
    위험이 더 크다. Jpmorgan·At&T 처럼 어색한 대문자 몇 개는 남지만
    회사를 착각하게 만들지는 않는다."""
    return re.sub(r"\s+(CL|CLASS)\s+[A-Z]$", "", raw.strip()).title()


def _fetch_sp500_from_spy():
    """State Street 가 SPY(S&P500 을 그대로 추종하는, 운용자산 8천억 달러
    안팎의 실제 ETF) 보유 종목을 매일 공개하는 xlsx 를 받는다. 실제로 그
    지수를 복제하려고 매일 리밸런싱하는 펀드의 자료라, 위키백과보다 뒤처질
    이유가 구조적으로 없다 — 지수가 바뀌면 펀드도 그날 안에 따라 바뀐다.
    xlsx 는 zip 안에 xml 이 든 것뿐이라 표준 라이브러리(zipfile + xml)만
    으로 읽는다 — openpyxl 같은 별도 패키지가 필요 없다."""
    url = ("https://www.ssga.com/us/en/intermediary/library-content/"
           "products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    z = zipfile.ZipFile(io.BytesIO(raw))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

    shared = []
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
        t = si.find("m:t", ns)
        if t is not None:
            shared.append(t.text or "")
        else:
            shared.append("".join((r_.find("m:t", ns).text or "")
                                  for r_ in si.findall("m:r", ns) if r_.find("m:t", ns) is not None))

    def cellval(c):
        v = c.find("m:v", ns)
        val = v.text if v is not None else None
        if c.get("t") == "s" and val is not None:
            val = shared[int(val)]
        return val

    rows = ET.fromstring(z.read("xl/worksheets/sheet1.xml")).findall(".//m:row", ns)
    out = {}
    past_header = False
    for row in rows:
        cells = [cellval(c) for c in row.findall("m:c", ns)]
        if len(cells) < 2:
            continue
        name, tk = cells[0], cells[1]
        if not past_header:
            # 종목이 나오기 전에 "Fund Name:"/"Ticker Symbol:" 같은 안내 행이
            # 몇 줄 있다 — 진짜 열 제목("Name"/"Ticker") 행을 볼 때까지 건너뛴다.
            # 고정된 행 수(예: 4번째부터)로 자르면 안내 행이 한 줄만 늘어도 깨진다.
            if name == "Name" and tk == "Ticker":
                past_header = True
            continue
        if not tk or not _TICKER_RE.match(tk):
            continue                                  # 잔여 포지션 등 정상 티커가 아닌 행은 건너뛴다
        cusip = cells[2] if len(cells) > 2 else None  # 13F 보유내역(CUSIP 기준)을 티커로 바꿀 때 쓴다
        out[tk] = {"name": _clean_spy_name(name) if name else tk, "market_cap": 0.0, "quote_px": None,
                   "asset_class": "us_stock", "currency": "USD", "source": "nasdaq_stock", "cusip": cusip}
    if not past_header:
        raise RuntimeError("표 머리글(Name/Ticker)을 못 찾음 — 파일 구조가 바뀐 것으로 보임")
    return out


def _fetch_sp500_from_wikipedia():
    """SPY 자료가 막히는 드문 경우를 위한 두 번째 소스. 코스피100을
    네이버에서 긁는 것과 같은 방식(HTML 표 파싱)이라 SPY 쪽보다 페이지
    구조 변경에 더 취약하다 — 그래서 1순위가 아니라 예비다."""
    html = get_text("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                    headers={"User-Agent": UA})
    i = html.index('id="constituents"')
    j = html.index("</table>", i)
    table = html[i:j]
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)[1:]:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 2:
            continue
        m = re.search(r">([^<]+)</a>", tds[0])
        tk = (m.group(1) if m else re.sub(r"<[^>]+>", "", tds[0])).strip().upper()
        name = re.sub(r"<[^>]+>", "", tds[1]).strip()
        if not tk or not _TICKER_RE.match(tk):
            continue
        out[tk] = {"name": name or tk, "market_cap": 0.0, "quote_px": None,
                   "asset_class": "us_stock", "currency": "USD", "source": "nasdaq_stock"}
    return out


def fetch_sp500(cache_path=None):
    """S&P500 종목 명단. 나스닥엔 이 지수용 엔드포인트가 없어 다른 데서
    받아야 한다 — 편입·편출이 수시로 있어서 캐시해 두면 바뀐 걸 놓치므로
    매번 새로 받는다.

    1순위는 SPY(지수를 실제로 추종하는 ETF)가 매일 공개하는 보유 종목
    자료다. 위키백과 문서보다 신뢰도가 높다 — 사람이 편집해 두길 기다리는
    게 아니라, 펀드가 그날 실제로 사고판 결과이기 때문이다. 그게 막히면
    위키백과를 2순위로 시도하고, 그마저 안 되면(또는 파싱된 종목 수가
    말이 안 되게 적으면 — 소스가 바뀌었다는 신호로 본다) 마지막으로 성공한
    명단을 그대로 쓴다. 그것도 없으면 이번 빌드에서는 S&P500 만 건너뛴다
    — 그날 하루 전체 사이트가 죽는 것보다는 낫다."""
    out, source = {}, None
    for name, fn in (("SPY 보유 종목", _fetch_sp500_from_spy),
                     ("위키백과", _fetch_sp500_from_wikipedia)):
        try:
            out = fn()
            if len(out) < SP500_MIN_MEMBERS:
                raise RuntimeError(f"파싱된 종목이 {len(out)}개뿐 — 소스가 바뀐 것으로 보임")
            source = name
            break
        except Exception as e:                        # noqa: BLE001
            log(f"  ! S&P500 명단을 {name}에서 못 받아 왔습니다 ({e})")

    if source is None:
        if cache_path and os.path.exists(cache_path):
            out = json.load(open(cache_path))
            log(f"  마지막으로 받아 둔 명단({len(out)}개)을 대신 씁니다 — 오늘 새 편입은 "
                f"반영되지 않았을 수 있습니다.")
        else:
            log("  이전에 받아 둔 명단도 없어 이번 빌드에서는 S&P500 을 건너뜁니다.")
            out = {}
    elif source == "SPY 보유 종목":
        # SPY 자료의 이름은 종종 지저분하다(예: "&"가 "+"로 깨져 나온다).
        # 위키백과에서 더 정갈한 이름을 한 번 더 받아 덮어쓴다 — 명단(핵심)은
        # 이미 SPY 로 확보했으니, 이 시도가 실패해도 무해하게 넘어간다.
        try:
            wiki = _fetch_sp500_from_wikipedia()
            fixed = 0
            for tk, m in out.items():
                if tk in wiki and wiki[tk]["name"] != m["name"]:
                    m["name"] = wiki[tk]["name"]
                    fixed += 1
            if fixed:
                log(f"  위키백과 이름으로 {fixed}개 종목명을 보정했습니다.")
        except Exception as e:                        # noqa: BLE001
            log(f"  이름 보정용 위키백과 조회 실패(무해함, SPY 쪽 이름 그대로 씀): {e}")
    if source is not None:
        log(f"  S&P500 명단 출처: {source}")
        if cache_path:
            tmp = cache_path + ".tmp"
            json.dump(out, open(tmp, "w"), ensure_ascii=False, sort_keys=True)
            os.replace(tmp, cache_path)
    log(f"S&P500 members: {len(out)}")
    return out


def fetch_sp500_quotes(tickers, workers):
    """나스닥100과 안 겹치는 S&P500 종목의 시가총액·현재가를 채운다.
    market_cap 을 못 구한 종목은 나중에 ETF 처럼 거래대금으로 대충
    가늠하는 대신, 실제 값을 여기서 바로 받아 둔다 — 유명 대형주들이라
    거래대금 근사가 눈에 띄게 틀려 보일 수 있어서다. 매일 새로 받는다
    (sectors.json 처럼 한 번 받고 영영 안 받으면 값이 굳어 버린다)."""
    if not tickers:
        return {}
    def one(tk):
        try:
            d = (get_json(f"https://api.nasdaq.com/api/quote/{tk}/summary?assetclass=stocks",
                          retries=2).get("data") or {}).get("summaryData") or {}
            cap = money((d.get("MarketCap") or {}).get("value"))
            px = money((d.get("PreviousClose") or {}).get("value"))
            return tk, cap, px
        except Exception as e:                        # noqa: BLE001
            log(f"  ! S&P500 quote lookup failed for {tk}: {e}")
            return tk, None, None
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tk, cap, px in ex.map(one, tickers):
            out[tk] = (cap, px)
    return out


# --------------------------------------------------------------------------
# 1c. 13F institutional filings — 유명 "슈퍼인베스터" 펀드들의 분기 보유내역
#     (SEC EDGAR 는 무료·공개이지만 CIK 기준이라 어떤 펀드를 볼지 미리
#     정해 둬야 한다 — 사용자가 고른 10개.)
# --------------------------------------------------------------------------
# SEC EDGAR 는 User-Agent 에 "이메일처럼 생긴 문자열"이 없으면 403으로 막는다
# (data.sec.gov 를 자동 요청으로부터 지키려는 필터로 보인다 — 실제로 검증되거나
# 메일이 발송되는 주소는 아니다). 실제 받는 사람이 없는 가짜 도메인을 쓴다.
SEC_UA = "money-connection-dashboard admin@money-connection.dev"

THIRTEENF_FUNDS = [
    ("Berkshire Hathaway", "0001067983"),
    ("Bridgewater Associates", "0001350694"),
    ("Renaissance Technologies", "0001037389"),
    ("Scion Asset Management", "0001649339"),
    ("Pershing Square", "0001336528"),
    ("Duquesne Family Office", "0001536411"),
    ("Third Point", "0001040273"),
    ("Baupost Group", "0001061768"),
    ("Appaloosa Management", "0001656456"),
    ("Tiger Global Management", "0001167483"),
]

_13F_NS = {"t": "http://www.sec.gov/edgar/document/thirteenf/informationtable"}

# 르네상스 테크놀로지스 같은 펀드는 보유 종목이 3천 개를 넘는다 — 평가액 기준
# 상위 몇 개만 보여준다(퀀트 펀드의 꼬리는 개별 종목 의미가 거의 없다).
HOLDINGS_CAP = 150

# 포트폴리오 성향(테마/시장/성격 배분)이 분기마다 어떻게 바뀌는지 보여주려고
# 이번 분기 포함 최근 몇 개 분기를 더 받는다 — 4개면 대략 1년치 추이.
HIST_QUARTERS = 4

# 완전히 나가진 않았지만 비중을 꽤 줄인 포지션을 "비중 축소"로 잡을 기준선.
# 직전 분기 대비 비중이 이 비율 이상 줄어야("30% 감소"면 0.30) 노이즈성
# 등락이 아니라 의미 있는 축소로 본다.
TRIM_THRESHOLD = 0.30


def _13f_filings(cik, n=2):
    """이 펀드의 최근 13F-HR n건(최신순)의 accession 번호."""
    d = json.loads(get_text(f"https://data.sec.gov/submissions/CIK{cik}.json",
                            headers={"User-Agent": SEC_UA}))
    recent = d["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        if form == "13F-HR":
            out.append({"accession": recent["accessionNumber"][i], "filed": recent["filingDate"][i]})
            if len(out) >= n:
                break
    return out


def _13f_holdings_url(cik, accession):
    """이 filing 안에서 보유내역표(정보표) xml 을 찾는다 — primary_doc.xml 은
    표지일 뿐이고, 실제 보유 종목은 다른(대개 훨씬 큰) xml 파일에 있다.
    파일 이름이 제출자마다 달라서(예: "56757.xml", "xyz_holding.xml") 이름이
    아니라 "primary_doc.xml 이 아닌 xml 중 가장 큰 것"으로 찾는다."""
    acc_nodash = accession.replace("-", "")
    idx = json.loads(get_text(
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/index.json",
        headers={"User-Agent": SEC_UA}))
    items = (idx.get("directory") or {}).get("item") or []
    xmls = [it for it in items if it["name"].endswith(".xml") and it["name"] != "primary_doc.xml"]
    if not xmls:
        return None
    best = max(xmls, key=lambda it: int(it.get("size") or 0))
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{best['name']}"


def _13f_period(cik, accession):
    acc_nodash = accession.replace("-", "")
    xml = get_text(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/primary_doc.xml",
                   headers={"User-Agent": SEC_UA})
    m = re.search(r"<periodOfReport>([^<]+)</periodOfReport>", xml)
    return m.group(1) if m else None


def _13f_parse(url):
    """CUSIP별로 합산한 보유내역: {cusip: {name, value(달러), shares}}.
    같은 종목이 (같은 CUSIP으로) 여러 매니저 명의로 나뉘어 여러 줄 나올 수
    있어 더한다. value 는 2023년 규정 개정 이후 천달러 단위가 아니라
    달러 그대로 나온다(버크셔 최근 filing 의 tableValueTotal 이 실제
    포트폴리오 규모(~3천억 달러)와 맞아떨어지는 것으로 확인함)."""
    raw = get_text(url, headers={"User-Agent": SEC_UA})
    root = ET.fromstring(raw)
    entries = root.findall(".//t:infoTable", _13F_NS)
    if not entries:                                   # 네임스페이스 없는 옛 파일 대비
        entries = root.findall(".//infoTable")
    out = {}
    for e in entries:
        def txt(tag):
            el = e.find(f"t:{tag}", _13F_NS)
            if el is None:
                el = e.find(tag)
            return el.text if el is not None else None
        cusip = txt("cusip")
        if not cusip:
            continue
        name = txt("nameOfIssuer") or cusip
        try:
            value = float(txt("value") or 0)
        except ValueError:
            value = 0.0
        shares = 0.0
        shrs_el = e.find("t:shrsOrPrnAmt", _13F_NS)
        if shrs_el is None:
            shrs_el = e.find("shrsOrPrnAmt")
        if shrs_el is not None:
            sh = shrs_el.find("t:sshPrnamt", _13F_NS)
            if sh is None:
                sh = shrs_el.find("sshPrnamt")
            if sh is not None and sh.text:
                try:
                    shares = float(sh.text)
                except ValueError:
                    pass
        rec = out.setdefault(cusip, {"name": name, "value": 0.0, "shares": 0.0})
        rec["value"] += value
        rec["shares"] += shares
    return out


def _resolve_cusips(cusips, cusip_to_ticker):
    """CUSIP -> 티커. 이미 아는 것(S&P500 보유 목록에서 얻은 CUSIP)은 그대로
    쓰고, 모르는 것만(펀드가 우리 638종목 유니버스 밖의 뭔가를 들고 있을 때)
    OpenFIGI(무료, 키 없이도 됨)에 배치로 물어본다."""
    out = {c: cusip_to_ticker[c] for c in cusips if cusip_to_ticker.get(c)}
    unknown = [c for c in cusips if c not in out]
    for i in range(0, len(unknown), 10):               # 인증 없이는 요청당 최대 10개
        batch = unknown[i:i + 10]
        body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in batch]).encode()
        req = urllib.request.Request("https://api.openfigi.com/v3/mapping", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                results = json.loads(r.read())
        except Exception as e:                          # noqa: BLE001
            log(f"  ! OpenFIGI CUSIP 조회 실패: {e}")
            time.sleep(2)
            continue
        for cusip, res in zip(batch, results):
            data = res.get("data") or []
            us = next((x for x in data if x.get("exchCode") == "US"), None) or (data[0] if data else None)
            if us and us.get("ticker"):
                out[cusip] = us["ticker"]
        time.sleep(1.5)                                 # 무료 한도(분당 25회) 안에서 여유 있게
    return out


def _13f_deadline_crossed(since, today):
    """13F-HR 은 분기 종료 뒤 45일 안에 내야 한다 — 분기 종료일(3/31, 6/30,
    9/30, 12/31)에 45일을 더한 달력 날짜로 근사하면 2/14, 5/15, 8/14, 11/14
    무렵이다. since 와 today 사이에 이 마감일 중 하나라도 지났으면, 새
    분기 공시가 나왔을 만하니 캐시가 며칠 안 됐어도 다시 받아야 한다."""
    deadlines = set()
    for year in range(since.year, today.year + 2):
        for md in ((2, 14), (5, 15), (8, 14), (11, 14)):
            try:
                deadlines.add(datetime.date(year, *md))
            except ValueError:                            # noqa: BLE001
                pass
    return any(since < d <= today for d in deadlines)


def fetch_13f(cusip_to_ticker, cache_path=None, max_age_days=20):
    """유명 '슈퍼인베스터' 펀드 10곳의 최근 13F-HR 보유내역 + 직전 분기
    대비 변화. 분기에 한 번(45일 지연)만 바뀌는 자료라 매일 새로 받을
    필요는 없지만, 그렇다고 날짜 수만 세면 분기 마감일 직후에 새 공시가
    나왔는데도 며칠을 더 묵은 캐시를 쓰게 될 수 있다 — 그래서 캐시가
    max_age_days 안이어도, 마지막으로 받은 뒤로 분기 마감일(대략 2/14,
    5/15, 8/14, 11/14)을 하나라도 지났으면 무조건 다시 받는다."""
    if cache_path and os.path.exists(cache_path):
        try:
            cached = json.load(open(cache_path))
            checked = datetime.date.fromisoformat(cached.get("checked_at", "2000-01-01"))
            today = datetime.date.today()
            # 전부 실패한 결과(펀드 0개)는 신선해도 안 쓴다 — 그걸 캐시로 믿으면
            # 진짜 장애(예: User-Agent 차단)가 계속 숨겨진다.
            if (cached.get("funds") and (today - checked).days < max_age_days
                    and not _13f_deadline_crossed(checked, today)):
                log(f"13F: 캐시가 최근({cached['checked_at']})이고 그 뒤로 분기 마감일도 없어 그대로 씁니다")
                return cached
        except Exception:                                # noqa: BLE001
            pass

    funds_out = []
    # 공통 표에서 "직전 분기엔 몇 개 펀드가 들고 있었는지"를 보여주려고, 펀드
    # 루프를 도는 동안 각 펀드의 직전 분기 보유 종목(티커화된 것만)을 여기
    # 모아 둔다. OpenFIGI 호출 없이 이미 확보한 CUSIP 맵만 쓴다 — history와
    # 같은 이유로, 펀드마다 매번 느린 조회를 반복하지 않기 위해서다.
    prev_fund_membership = {}   # ticker -> {직전 분기에 이 종목을 들고 있던 펀드명}
    for name, cik in THIRTEENF_FUNDS:
        try:
            filings = _13f_filings(cik, n=HIST_QUARTERS)
            if not filings:
                log(f"  ! {name}: 13F-HR 파일링을 찾지 못함")
                continue
            cur, prev = filings[0], (filings[1] if len(filings) > 1 else None)
            cur_url = _13f_holdings_url(cik, cur["accession"])
            cur_period = _13f_period(cik, cur["accession"])
            cur_holdings = _13f_parse(cur_url) if cur_url else {}
            prev_holdings = {}
            prev_period = None
            if prev:
                prev_url = _13f_holdings_url(cik, prev["accession"])
                prev_period = _13f_period(cik, prev["accession"])
                if prev_url:
                    prev_holdings = _13f_parse(prev_url)
            for c in prev_holdings:
                tk = cusip_to_ticker.get(c)
                if tk:
                    prev_fund_membership.setdefault(tk, set()).add(name)

            # 포트폴리오 성향 추이(테마·시장·성격 배분)용 분기별 보유내역.
            # cur/prev 는 이미 받았으니 재사용하고, 그보다 더 예전 분기만 새로
            # 받는다. 여긴 표로 보여주는 게 아니라 대략적인 배분 비중만
            # 필요해서 — 르네상스처럼 보유 종목이 아무리 많아도 — OpenFIGI
            # 호출 없이 우리가 이미 아는(S&P500 명단에서 얻은) CUSIP만으로
            # 티커를 붙인다. 못 알아낸 자산은 배분 계산에서 "기타"로 빠진다.
            history = []
            for i, filing in enumerate(filings):
                if i == 0:
                    h, per = cur_holdings, cur_period
                elif i == 1:
                    h, per = prev_holdings, prev_period
                else:
                    hurl = _13f_holdings_url(cik, filing["accession"])
                    h = _13f_parse(hurl) if hurl else {}
                    per = _13f_period(cik, filing["accession"])
                tot = sum(x["value"] for x in h.values()) or 1
                hist_rows = [{"ticker": cusip_to_ticker[c], "value": x["value"],
                             "pct": round(x["value"] / tot * 100, 3)}
                            for c, x in h.items() if cusip_to_ticker.get(c)]
                history.append({"period": per, "total_value": tot, "holdings": hist_rows})
            history.reverse()   # 오래된 분기 -> 최신 분기 순으로, 추이 차트가 왼쪽부터 시간순이 되게

            # 르네상스처럼 보유 종목이 3천 개가 넘는 펀드도 있다 — 전부 CUSIP을
            # 풀면(OpenFIGI 배치 호출) 한 펀드에만 십수 분이 걸려 빌드가 안 끝난다.
            # 어차피 표로 보여줄 것도 아니니, 평가액 기준 상위 HOLDINGS_CAP개만
            # (실제 비중·총액은 전체 보유내역 기준으로 정확히 계산한 뒤) 추려서
            # 그것만 티커로 바꾼다 — 꼬리의 자잘한 포지션은 "공통 보유" 비교에도
            # 의미가 거의 없다.
            total = sum(h["value"] for h in cur_holdings.values()) or 1
            dropped_all = set(prev_holdings) - set(cur_holdings)
            # prev 가 없으면(비교할 직전 분기 filing 자체가 없으면) "신규 편입"을
            # 계산할 기준이 없다 — 이 경우 cur 전부를 "신규"로 보면 오해를 준다.
            added_all = (set(cur_holdings) - set(prev_holdings)) if prev_holdings else set()
            top_cusips = sorted(cur_holdings, key=lambda c: -cur_holdings[c]["value"])[:HOLDINGS_CAP]
            top_dropped_cusips = sorted(dropped_all, key=lambda c: -prev_holdings[c]["value"])[:HOLDINGS_CAP]
            top_added_cusips = sorted(added_all, key=lambda c: -cur_holdings[c]["value"])[:HOLDINGS_CAP]

            # 완전히 나가진 않았지만("이탈"엔 안 잡힘) 비중을 크게 줄인 포지션 —
            # 실제로 팔아치운 건지, 그냥 다른 종목이 더 올라 상대적으로만
            # 줄어든 건지는 프론트에서 주식수·주가와 같이 보여줘 판단하게 하고,
            # 여기선 "비중이 TRIM_THRESHOLD 이상 줄었다"만 기준으로 추린다.
            prev_total = sum(h["value"] for h in prev_holdings.values()) or 1
            held_both = set(cur_holdings) & set(prev_holdings)
            trimmed_all = set()
            for c in held_both:
                prev_pct_c = prev_holdings[c]["value"] / prev_total * 100
                cur_pct_c = cur_holdings[c]["value"] / total * 100
                if prev_pct_c > 0 and cur_pct_c <= prev_pct_c * (1 - TRIM_THRESHOLD):
                    trimmed_all.add(c)
            top_trimmed_cusips = sorted(
                trimmed_all,
                key=lambda c: (cur_holdings[c]["value"] / total * 100)
                             - (prev_holdings[c]["value"] / prev_total * 100)
            )[:HOLDINGS_CAP]

            tk_map = _resolve_cusips(
                set(top_cusips) | set(top_dropped_cusips) | set(top_added_cusips) | set(top_trimmed_cusips),
                cusip_to_ticker)

            # 직전 분기 대비 비중·평가액·주식수가 어떻게 바뀌었는지 — 그냥
            # "덜 갖고 있다"가 아니라 "주가가 올라 비중만 줄었나, 실제로
            # 주식을 팔았나"를 프론트에서 가늠해 보려면 필요한 기준값들이다.
            added_set = set(top_added_cusips)
            rows = []
            for c in top_cusips:
                row = {
                    "cusip": c, "ticker": tk_map.get(c), "name": cur_holdings[c]["name"],
                    "value": cur_holdings[c]["value"], "shares": cur_holdings[c]["shares"],
                    "pct": round(cur_holdings[c]["value"] / total * 100, 3),
                    "added": c in added_set,      # 이번 분기 신규 편입이면 true (표에서 배지로 표시)
                }
                if c in prev_holdings:             # 직전 분기에도 있던 종목이면 비교값을 같이 싣는다
                    ph = prev_holdings[c]
                    row["prev_value"] = ph["value"]
                    row["prev_shares"] = ph["shares"]
                    row["prev_pct"] = round(ph["value"] / prev_total * 100, 3)
                rows.append(row)

            dropped = [{"cusip": c, "ticker": tk_map.get(c), "name": prev_holdings[c]["name"],
                       "prev_value": prev_holdings[c]["value"],
                       "prev_pct": round(prev_holdings[c]["value"] / prev_total * 100, 3)}
                      for c in top_dropped_cusips]

            added = [{"cusip": c, "ticker": tk_map.get(c), "name": cur_holdings[c]["name"],
                     "value": cur_holdings[c]["value"],
                     "pct": round(cur_holdings[c]["value"] / total * 100, 3)}
                    for c in top_added_cusips]

            trimmed = []
            for c in top_trimmed_cusips:
                ph, ch = prev_holdings[c], cur_holdings[c]
                shares_delta_pct = (round((ch["shares"] - ph["shares"]) / ph["shares"] * 100, 2)
                                    if ph["shares"] else None)
                trimmed.append({
                    "cusip": c, "ticker": tk_map.get(c), "name": ch["name"],
                    "value": ch["value"], "prev_value": ph["value"],
                    "pct": round(ch["value"] / total * 100, 3),
                    "prev_pct": round(ph["value"] / prev_total * 100, 3),
                    "shares": ch["shares"], "prev_shares": ph["shares"],
                    "shares_delta_pct": shares_delta_pct,
                })

            funds_out.append({
                "name": name, "cik": cik, "period": cur_period, "filed": cur["filed"],
                "prev_period": prev_period, "total_value": total,
                "holdings_count": len(cur_holdings), "dropped_count": len(dropped_all),
                "added_count": len(added_all), "trimmed_count": len(trimmed_all),
                "holdings": rows, "dropped": dropped, "added": added, "trimmed": trimmed,
                "history": history,
            })
            log(f"  13F {name}: 보유 {len(cur_holdings)}개(상위 {len(rows)}개 표시), "
                f"신규 {len(added_all)}개(상위 {len(added)}개 표시), "
                f"이탈 {len(dropped_all)}개(상위 {len(dropped)}개 표시), "
                f"비중축소 {len(trimmed_all)}개(상위 {len(trimmed)}개 표시), "
                f"추이 {len(history)}개 분기 ({cur_period})")
        except Exception as e:                           # noqa: BLE001
            log(f"  ! 13F {name} 조회 실패: {e}")

    # 공통 보유 / 최근 공통 이탈: 티커를 알아낸 것만 센다(CUSIP 만 있고 티커를
    # 못 찾은 자산은 우리 유니버스와 비교할 수 없어 제외).
    def _common(field, weight_field="pct"):
        holder = {}
        name_of = {}
        weights = {}
        for f in funds_out:
            for row in f[field]:
                tk = row.get("ticker")
                if not tk:
                    continue
                holder.setdefault(tk, []).append(f["name"])
                name_of.setdefault(tk, row["name"])
                w = row.get(weight_field)
                if w is not None:
                    weights.setdefault(tk, []).append(w)
        return sorted(
            ({"ticker": tk, "name": name_of[tk], "funds": funds,
              # 직전 분기엔 우리 펀드 유니버스 중 몇 곳이 이 종목을 들고 있었는지
              # — "지난 분기 3개 -> 이번 분기 6개" 식으로 늘었는지 줄었는지 비교용.
              "prev_fund_count": len(prev_fund_membership.get(tk, ())),
              # 이 종목을 들고 있는(있었던) 펀드들의 평균 비중 — 개별 펀드
              # 기준 비중·평가액 변화는 안 보이니, 대략의 감을 잡는 용도.
              "avg_pct": round(sum(weights[tk]) / len(weights[tk]), 3) if weights.get(tk) else None}
             for tk, funds in holder.items() if len(funds) >= 2),
            key=lambda r: -len(r["funds"]))

    common_holdings = _common("holdings", "pct")
    common_dropped = _common("dropped", "prev_pct")
    common_added = _common("added", "pct")
    common_trimmed = _common("trimmed", "pct")

    out = {
        "checked_at": datetime.date.today().isoformat(),
        "funds": funds_out,
        "common_holdings": common_holdings,
        "common_dropped": common_dropped,
        "common_added": common_added,
        "common_trimmed": common_trimmed,
    }
    log(f"13F: {len(funds_out)}/{len(THIRTEENF_FUNDS)}개 펀드 · "
        f"공통 보유 {len(common_holdings)}개 · 공통 신규 {len(common_added)}개 · "
        f"공통 이탈 {len(common_dropped)}개 · 공통 비중축소 {len(common_trimmed)}개")
    if cache_path and funds_out:
        tmp = cache_path + ".tmp"
        json.dump(out, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, cache_path)
    elif not funds_out and cache_path and os.path.exists(cache_path):
        # 이번엔 전부 실패했지만, 예전에 성공해 둔 캐시가 있으면 그걸 그대로 쓴다
        # (오늘 하루 13F 탭이 텅 비는 것보다 조금 오래된 데이터가 낫다).
        log("13F: 이번 조회가 전부 실패해 예전 캐시를 그대로 씁니다")
        return json.load(open(cache_path))
    return out


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


# 나스닥이 그날 종가를 몇 시간 늦게 확정하는 일이 있다 — 야후는 실시간
# 시세를 쓰는 쪽이라 그 사이를 먼저 채워 줄 때가 많다. 최근 창에만 적용한다
# (야후는 종가 전용이라 과거 전체를 이걸로 받으면 시가·고가·저가를 잃는다).
TOPOFF_SOURCES = ("nasdaq_stock", "nasdaq_etf")
# 나스닥에서 받은 가장 최근 날짜가 오늘보다 이만큼 넘게 오래되었을 때만
# 야후를 추가로 부른다. 주말 이틀 정도는 정상이니 여유를 둔다 — 매번
# 부르면(캐시 재사용의 요청 수가 얼마 안 되는데 여기서 절반 가까이를
# 다시 늘려 버려) 캐싱으로 아낀 시간을 도로 까먹는다.
TOPOFF_STALE_DAYS = 3

# 캐시에 있는 종목은 이 기간만 새로 받는다 — 주말·연휴가 이어져도
# 겹치도록 넉넉히 잡는다. 새 종목(캐시에 없음)은 여전히 5년 전체를 받는다.
INCR_DAYS = 12


def _load_price_cache(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        raw = json.load(open(path))
    except Exception as e:                            # noqa: BLE001
        log(f"가격 캐시를 읽지 못해 전체를 새로 받습니다: {e}")
        return {}
    out = {}
    for tk, rows in raw.items():
        d = {}
        for ds, row in rows.items():
            try:
                d[datetime.date.fromisoformat(ds)] = row
            except ValueError:
                continue
        if d:
            out[tk] = d
    return out


def _save_price_cache(path, series, members):
    """다음 실행이 다시 5년 전체를 받지 않도록 저장해 둔다. 티커별로
    굴러가는 5년 창을 유지하기 위해 그보다 오래된 날짜는 잘라 낸다 —
    그러지 않으면 캐시가 매일 조금씩 무한히 자란다."""
    if not path:
        return
    cutoff = datetime.date.today() - datetime.timedelta(days=int(YEARS * 365.25) + 10)
    out = {}
    for tk, rows in series.items():
        if tk not in members:                          # 지수에서 빠진 종목은 캐시에서도 뺀다
            continue
        out[tk] = {d.isoformat(): row for d, row in rows.items() if d >= cutoff}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)


def fetch_all_prices(members, workers, cache_path=None):
    to = datetime.date.today()
    full_frm = to - datetime.timedelta(days=int(YEARS * 365.25) + 5)
    cache = _load_price_cache(cache_path)
    if cache:
        log(f"가격 캐시: {len(cache)}개 종목 — 캐시에 있는 종목은 최근 {INCR_DAYS}일만 새로 받습니다")
    series, problems = {}, []

    def fetch_fresh(tk, m, frm):
        """기본 소스로 받고, 그 결과가 눈에 띄게 오래됐을 때만(TOPOFF_STALE_DAYS)
        미국 주식·ETF 에 한해 야후로 최근 며칠을 추가로 받아 빈 날짜를 채운다.
        나스닥 값이 있으면 그대로 둔다 — 야후는 종가만 있어 시가·고가·저가가
        부정확할 수 있기 때문이다. 매번 야후까지 부르면 캐시로 아낀 요청 수를
        도로 절반 가까이 늘리게 되므로, 정말 뒤처졌을 때만 부른다."""
        rows = dict(fetch_one_series(tk, m, frm, to))
        if m.get("source") in TOPOFF_SOURCES:
            newest = max(rows) if rows else None
            if newest is None or (to - newest).days > TOPOFF_STALE_DAYS:
                try:
                    extra = fetch_yahoo(tk, frm, to)
                except Exception:                      # noqa: BLE001
                    extra = {}
                for d, row in extra.items():
                    rows.setdefault(d, row)
        return rows

    def one(tk):
        m = members[tk]
        cached = cache.get(tk)
        frm = (to - datetime.timedelta(days=INCR_DAYS)) if cached else full_frm
        try:
            fresh = fetch_fresh(tk, m, frm)
        except Exception as e:                        # noqa: BLE001
            fresh, err = {}, f"historical failed: {e}"
        else:
            err = None if (fresh or cached) else "no rows"
        rows = {**cached, **fresh} if cached else fresh
        if not rows:
            return tk, {}, err or "no rows"
        # 오늘 갱신은 실패했지만 캐시로 버틴 경우 — 종목은 그대로 내보내되
        # 무슨 일이 있었는지는 바깥 루프가 (조용히) 알 수 있게 err 를 함께 준다.
        if err:
            return tk, rows, ("stale:" + err)
        # Sanity gate: the historical series must agree with today's quote.
        # Nasdaq's endpoint has resolved a symbol to an unrelated instrument
        # before, and a silently wrong series would poison every correlation.
        # Skipped when quote_px is unknown (ETF/crypto/gold: no separate quote
        # call for those, so nothing to cross-check against, and none of them
        # share Nasdaq's stock-symbol-collision failure mode anyway).
        quote = m.get("quote_px")
        last = rows[max(rows)][3]
        if quote and last and abs(last - quote) / quote > 0.25:
            return tk, rows, f"series/quote mismatch (hist {last} vs quote {quote})"
        return tk, rows, None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for tk, rows, problem in ex.map(one, list(members)):
            if problem and problem.startswith("stale:"):
                # 오늘 갱신은 실패했지만 캐시로 이어 간다 — 사용자에게 보이는
                # "보정" 경고는 진짜 새 문제일 때만 남기고, 이건 로그에만 적는다.
                log(f"  ! {tk}: {problem[6:]} (어제까지 값으로 유지)")
                series[tk] = rows
                continue
            if problem and "mismatch" in problem:
                log(f"  ! {tk}: {problem} -> trying Yahoo")
                cached = cache.get(tk)
                frm = (to - datetime.timedelta(days=INCR_DAYS)) if cached else full_frm
                try:
                    alt = fetch_yahoo(tk, frm, to)
                except Exception as e:                # noqa: BLE001
                    alt = {}
                    log(f"    Yahoo failed too: {e}")
                alt = {**cached, **alt} if cached else alt
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
    _save_price_cache(cache_path, series, members)

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
            p_rets_for_count = w_rets
        else:
            cut = dates[-1] - datetime.timedelta(days=pd_["days"])
            i0 = next((i for i, d in enumerate(dates) if d > cut), 0)
            p_dates = dates[i0:]
            p_rets = {t: rets5y[t][i0:] for t in tickers}
            # a 3-month window has ~63 sessions: the 1y minimum would void it
            min_ov = min(MIN_OVERLAP, max(20, int(len(p_dates) * 0.25)))
            period_matrices[pd_["key"]] = corr_matrix(tickers, p_rets, p_dates, min_ov)
            p_from, p_to = p_dates[0], p_dates[-1]
            p_rets_for_count = p_rets
        # 헤더의 "거래일". 날짜 축은 모든 종목의 합집합이라 암호화폐 때문에
        # 달력일에 가깝다 — 종목별 실제 관측 수의 중앙값이 맞는 값이다.
        counts = sorted(sum(1 for v in p_rets_for_count[t] if v is not None) for t in tickers)
        p_sessions = counts[len(counts) // 2] if counts else 0
        period_meta.append({"key": pd_["key"], "label": pd_["label"],
                            "from": p_from.isoformat(), "to": p_to.isoformat(),
                            "sessions": p_sessions})
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


def render(corr, px, oh, leaders, fund, macro, members, themes, index_date, problems, out_dir, usdkrw=None,
           thirteenf=None):
    tpl = open(os.path.join(ROOT, "build", "template.html")).read()
    engine = open(os.path.join(ROOT, "build", "candle_engine.js")).read()
    # 브리지 스크립트를 페이지에 함께 싣는다. 사용자가 저장소를 clone 하지
    # 않아도 연동 마법사가 파일을 그대로 내려줄 수 있어야 한다.
    bridge_src = open(os.path.join(ROOT, "build", "toss_bridge.py")).read()
    # 확장 프로그램도 통째로 싣는다 — 파이썬이 없는 사람을 위한 경로
    ext_dir = os.path.join(ROOT, "build", "extension")
    ext_files = {f: open(os.path.join(ext_dir, f)).read()
                 for f in sorted(os.listdir(ext_dir)) if not f.startswith(".")}
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
    # 시장마다 마지막 거래일이 다르다. 미국이 휴장한 날(노동절 등)에는
    # 한국만 새 봉이 생기는데, 그때 "왜 미국은 안 갱신되지?" 로 보이지 않도록
    # 시장별 최신 거래일을 페이지에 함께 싣는다.
    def latest_session(pred):
        best = None
        for n in nodes:
            if not pred(n):
                continue
            row = oh["data"].get(n["id"]) or []
            for k in range(len(row) - 1, -1, -1):
                if row[k]:
                    d = oh["dates"][k]
                    if best is None or d > best:
                        best = d
                    break
        return best

    sessions = {
        "us": latest_session(lambda n: n["currency"] == "USD"
                             and n["assetClass"] in ("us_stock", "etf")),
        "kr": latest_session(lambda n: n["currency"] == "KRW"),
        "crypto": latest_session(lambda n: n["assetClass"] == "crypto"),
    }
    meta = {
        "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "index_date": index_date,
        "sessions": {k: v for k, v in sessions.items() if v},
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
    log(f"bridge script embedded: {len(bridge_src):,} bytes")
    log(f"extension embedded: {len(ext_files)} files, "
        f"{sum(len(v) for v in ext_files.values()):,} bytes")

    # 스크립트 블록 안에 심는 문자열에서 "<" 를 통째로 \u003c 로 바꾼다.
    # "</script>" 는 그 자리에서 블록을 닫아 버리고, "<!--" 뒤의 "<script" 는
    # 파서를 이중 이스케이프 상태로 밀어 넣어 그다음 "</script>" 를 무시하게 만든다.
    # 확장 프로그램의 options.html 이 두 경우에 모두 해당한다.
    # JSON 문자열 안에서 \u003c 는 "<" 와 같은 값이므로 내용은 변하지 않는다.
    blob = lambda o: (json.dumps(o, separators=(",", ":"), ensure_ascii=False)
                        .replace("<", "\\u003c"))
    body = (tpl.replace("/*__CANDLE_ENGINE__*/", engine, 1)
               .replace("/*__CORR__*/", blob(corr))
               .replace("/*__PX__*/", blob(px))
               .replace("/*__OH__*/", blob(oh))
               .replace("/*__NODES__*/", blob(nodes))
               .replace("/*__FUND__*/", blob(fund))
               .replace("/*__THIRTEENF__*/", blob(thirteenf or {}))
               .replace("/*__MACRO__*/", blob(macro))
               .replace("/*__META__*/", blob(meta))
               .replace("/*__AUTH__*/", blob(auth))
               .replace("/*__BRIDGE__*/", blob(bridge_src), 1)
               .replace("/*__EXT__*/", blob(ext_files), 1))

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
    # 데이터를 통째로 심는 페이지라, 심은 내용이 스크립트 블록을 일찍 닫아
    # 버리는 사고가 조용히 일어날 수 있다. 쓰기 전에 세어 본다.
    opens = len(re.findall(r"<script\b", html))
    closes = html.count("</script>")
    if opens != closes:
        raise SystemExit(f"스크립트 태그 수가 맞지 않습니다: <script {opens}개 / </script> {closes}개. "
                         "심어 넣은 문자열 안에 </script> 가 들어갔을 수 있습니다.")

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

    # ---- membership: Nasdaq-100 + S&P500 + KOSPI100 + ETF/crypto/gold ----
    nasdaq_members, index_date = fetch_constituents()
    nasdaq_tickers = set(nasdaq_members)
    sp500_members = fetch_sp500(os.path.join(a.cache, "sp500_roster.json"))
    # SPY 보유 목록에서 얻은 CUSIP -> 티커 매핑 — 13F 보유내역(CUSIP 기준)을
    # 우리 유니버스의 티커로 바꿀 때 1차로 쓴다(모르는 CUSIP만 OpenFIGI로 보충).
    cusip_to_ticker = {m["cusip"]: t for t, m in sp500_members.items() if m.get("cusip")}
    # 나스닥100과 겹치는 종목은 이미 더 풍부한 데이터(시가총액·현재가)를
    # 받아 둔 nasdaq_members 쪽을 그대로 쓴다 — 위키백과 쪽은 이름뿐이다.
    new_sp500 = {t: m for t, m in sp500_members.items() if t not in nasdaq_members}
    quotes = fetch_sp500_quotes(list(new_sp500), a.max_workers)
    for t, m in new_sp500.items():
        cap, px = quotes.get(t, (None, None))
        if cap: m["market_cap"] = cap
        if px: m["quote_px"] = px
    usdkrw = fetch_usdkrw_rate()
    kospi_members = fetch_kospi100(usdkrw)
    extra_members = fetch_extra_assets()

    members = {}
    members.update(nasdaq_members)
    members.update(new_sp500)
    members.update(kospi_members)
    members.update(extra_members)
    log(f"total universe: {len(members)} "
        f"({len(nasdaq_members)} Nasdaq-100 + {len(new_sp500)} S&P500 only + "
        f"{len(kospi_members)} KOSPI100 + {len(extra_members)} ETF/crypto/gold)")

    # theme: Nasdaq-100·S&P500·KOSPI100 모두 같은 8개 섹터 테마로 분류된다
    # (KOSPI는 자체 WICS 업종 라벨 -> 테마 매핑을 거친다); ETF/crypto/gold는
    # 고정된 테마 하나씩(11/12/13) — "업종"이라는 개념이 안 맞는 자산이라서다.
    us_stocks = list(nasdaq_members) + list(new_sp500)
    themes, _ = resolve_themes(us_stocks, cfg, os.path.join(a.cache, "sectors.json"))
    themes.update(resolve_kospi_themes(kospi_members, cfg, os.path.join(a.cache, "kospi_industries.json")))
    themes.update({t: (12 if m["asset_class"] == "crypto" else 13 if m["asset_class"] == "commodity" else 11)
                   for t, m in extra_members.items()})

    series, problems = fetch_all_prices(members, a.max_workers,
                                        cache_path=os.path.join(a.cache, "prices_cache.json"))

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

    # fundamentals (PER/EPS/재무제표) only exist for US equities (Nasdaq100 +
    # S&P500) — Nasdaq's financials endpoints do not cover KOSPI/ETF/crypto/gold
    us_fund_scope = set(us_stocks)
    fund_scope = [t for t in corr["tickers"] if t in us_fund_scope]
    fund = fetch_fundamentals(fund_scope, os.path.join(a.cache, "fundamentals.json"),
                              a.fundamentals_refresh, a.max_workers)
    thirteenf = fetch_13f(cusip_to_ticker, os.path.join(a.cache, "thirteenf_cache.json"))
    render(corr, px, oh, leaders, fund, macro, members, themes, index_date, problems, a.out, usdkrw,
           thirteenf=thirteenf)


if __name__ == "__main__":
    main()
