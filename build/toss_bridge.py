#!/usr/bin/env python3
"""토스증권 Open API 브리지 — 머니 커넥션 대시보드의 실거래용.

왜 브리지가 필요한가
--------------------
토스증권 Open API 는 OAuth 2.0 client credentials 방식이고, 토스 문서 자체가
`client_secret` 을 "노출되지 않도록 서버 측에서만 사용" 하라고 명시한다.
대시보드는 GitHub Pages 에 공개된 정적 페이지라 소스가 전부 열려 있으므로
키를 넣을 수 없다. 그래서 키는 이 파일이 도는 본인 PC 에만 두고, 브라우저는
localhost 의 이 프로세스에만 요청한다.

    ~/.money-connection/toss.json
    {
      "client_id":        "발급받은 클라이언트 ID",
      "client_secret":    "발급받은 시크릿",
      "account_seq":      0,        // 생략하면 첫 BROKERAGE 계좌를 자동 사용
      "google_client_id": "대시보드가 쓰는 OAuth 웹 클라이언트 ID",
      "allowed_email":    "이 브리지를 쓸 내 구글 계정"
    }

누가 쓸 수 있나
--------------
모든 요청은 X-MC-Auth 헤더에 구글 ID 토큰을 실어야 하고, 브리지는 그것을
구글에 직접 물어 검증한다(서명·만료는 구글이, aud 와 이메일은 우리가).
allowed_email 과 다른 계정이면 거절한다.

이 검증이 있어야 Tailscale Funnel 로 인터넷에 열어도 안전하다 —
주소를 아는 것만으로는 아무것도 할 수 없다.

실행
----
    python3 build/toss_bridge.py            # 127.0.0.1:8787
    python3 build/toss_bridge.py --port 9000 --origin https://example.github.io

127.0.0.1 에만 바인딩한다. 외부에 노출하지 말 것 — 이 포트에 닿을 수 있는
모든 프로그램이 당신의 계좌로 주문을 낼 수 있다.
"""
import argparse, gzip, hmac, json, os, re, shutil, signal, subprocess, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://openapi.tossinvest.com"
CONFIG = os.path.expanduser("~/.money-connection/toss.json")
DEFAULT_ORIGINS = ["https://eloyyang.github.io", "http://localhost:8778", "http://127.0.0.1:8778"]
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo?id_token="

_gcache = {}          # id_token -> exp(초). 검증에 성공한 토큰만 만료까지 재사용한다

_lock = threading.Lock()
_token = {"value": None, "expires": 0}
_account = {"seq": None, "no": None}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# 맥은 앱 안에 CLI 가 들어 있어 PATH 에 없을 수 있다
TS_PATHS = ["tailscale",
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale",
            r"C:\Program Files\Tailscale\tailscale.exe"]


def tailscale_bin():
    for c in TS_PATHS:
        found = shutil.which(c) if os.sep not in c else (c if os.path.exists(c) else None)
        if found:
            return found
    return None


def funnel_up(port):
    """tailscale funnel 을 켜고 공개 주소를 돌려준다. 실패해도 브리지는 계속 돈다."""
    ts = tailscale_bin()
    if not ts:
        log("tailscale 을 찾지 못했습니다 — 폰 접속은 건너뜁니다. https://tailscale.com/download")
        return None
    cmd = [ts, "funnel", "--bg", "--https=443", f"localhost:{port}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:                                # noqa: BLE001
        log(f"tailscale 실행 실패: {e}")
        return None
    out = (r.stdout or "") + (r.returncode and (r.stderr or "") or "")
    if r.returncode != 0:
        log("tailscale funnel 을 켜지 못했습니다:")
        for line in (r.stderr or r.stdout or "").strip().splitlines()[:6]:
            log("  " + line)
        return None
    m = re.search(r"https://[\w.-]+\.ts\.net", out)
    if not m:                                             # 이미 켜져 있으면 조용할 수 있다
        try:
            st = subprocess.run([ts, "funnel", "status"], capture_output=True, text=True, timeout=20)
            m = re.search(r"https://[\w.-]+\.ts\.net", (st.stdout or "") + (st.stderr or ""))
        except Exception:                                 # noqa: BLE001
            m = None
    return m.group(0) if m else None


def funnel_down(port):
    ts = tailscale_bin()
    if not ts:
        return
    try:
        subprocess.run([ts, "funnel", "--https=443", f"localhost:{port}", "off"],
                       capture_output=True, text=True, timeout=30)
        log("tailscale funnel 을 껐습니다.")
    except Exception:                                     # noqa: BLE001
        log("funnel 을 끄지 못했습니다 — 'tailscale funnel --https=443 off' 로 직접 꺼 주세요.")


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit(f"설정 파일이 없습니다: {CONFIG}\n"
                 '{"client_id": "...", "client_secret": "...", "account_seq": 0} 형태로 만들어 주세요.')
    with open(CONFIG) as f:
        cfg = json.load(f)
    for k in ("client_id", "client_secret"):
        if not cfg.get(k):
            sys.exit(f"{CONFIG} 에 {k} 가 없습니다.")
    # 인터넷에 노출될 수 있으므로(터널) 어느 구글 계정만 쓸 수 있는지 반드시 정한다
    for k in ("google_client_id", "allowed_email"):
        if not cfg.get(k):
            sys.exit(
                f"{CONFIG} 에 {k} 가 없습니다.\n\n"
                "이 브리지는 구글 계정으로만 열립니다. 아래 두 값을 추가해 주세요:\n"
                '  "google_client_id": "대시보드에 쓰는 OAuth 웹 클라이언트 ID",\n'
                '  "allowed_email":    "허용할 내 구글 계정 이메일"\n\n'
                "클라이언트 ID 는 대시보드 → 포트폴리오 → 설정 → 토스증권 연결 의 안내에 있습니다.")
    return cfg


def verify_google(id_token, cfg):
    """구글 ID 토큰을 구글에 직접 물어 검증한다.
       서명·만료는 구글이 확인해 주고, 우리는 이 대시보드용 토큰인지(aud)와
       허용한 계정인지(email)를 확인한다. 표준 라이브러리만으로 가능한 방법이다."""
    if not id_token:
        return False, "구글 로그인이 필요합니다."
    now = time.time()
    with _lock:
        exp = _gcache.get(id_token)
        for t, e in list(_gcache.items()):
            if e < now:
                _gcache.pop(t, None)
    if exp and exp > now:
        return True, None
    try:
        with urllib.request.urlopen(TOKENINFO + urllib.parse.quote(id_token), timeout=10) as r:
            d = json.loads(read_body(r))
    except urllib.error.HTTPError:
        return False, "구글 토큰이 유효하지 않습니다. 대시보드에서 다시 로그인해 주세요."
    except Exception:                                    # noqa: BLE001
        return False, "구글 검증 서버에 연결하지 못했습니다."
    if d.get("aud") != cfg["google_client_id"]:
        return False, "다른 앱에서 발급된 토큰입니다."
    if d.get("email_verified") not in (True, "true"):
        return False, "이메일이 확인되지 않은 구글 계정입니다."
    want = str(cfg["allowed_email"]).strip().lower()
    got = str(d.get("email", "")).strip().lower()
    if not hmac.compare_digest(want, got):
        return False, f"허용되지 않은 계정입니다. 이 브리지는 {want} 로만 열립니다."
    try:
        exp = float(d.get("exp", 0))
    except (TypeError, ValueError):
        exp = 0
    if exp <= now:
        return False, "구글 토큰이 만료되었습니다. 다시 로그인해 주세요."
    with _lock:
        _gcache[id_token] = exp
    return True, None


def read_body(resp):
    """토스는 gzip 으로 응답할 수 있다(오류 응답 포함). 압축을 풀어 문자열로."""
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in enc or raw[:2] == b"\x1f\x8b":
        try: raw = gzip.decompress(raw)
        except Exception: pass                       # noqa: BLE001
    return raw.decode("utf-8", errors="replace")


def call(method, path, cfg, body=None, account=True, timeout=20):
    """토스 API 호출. 401 이면 토큰을 한 번 새로 받아 재시도한다."""
    for attempt in (1, 2):
        headers = {"Authorization": f"Bearer {token(cfg)}", "Accept": "application/json"}
        if account:
            headers["X-Tossinvest-Account"] = str(account_seq(cfg))
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(read_body(r) or "{}")
        except urllib.error.HTTPError as e:
            raw = read_body(e)
            if e.code == 401 and attempt == 1:
                with _lock:
                    _token["value"] = None          # 만료로 보고 한 번만 재발급
                continue
            try:
                detail = json.loads(raw)
            except Exception:                        # noqa: BLE001
                detail = {"raw": raw[:400]}
            raise RuntimeError(f"{e.code} {detail}") from None


def token(cfg):
    with _lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"]
    # 스펙상 이 엔드포인트만 form-urlencoded 이다 (나머지는 JSON)
    body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "client_id": cfg["client_id"],
                                   "client_secret": cfg["client_secret"]}).encode()
    req = urllib.request.Request(API + "/oauth2/token", data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(read_body(r))
    except urllib.error.HTTPError as e:
        msg = read_body(e)[:300]
        hint = ("  ← client_id / client_secret 을 확인하세요 (토스증권 WTS → 설정 → Open API)"
                if e.code in (400, 401) else "")
        raise RuntimeError(f"토큰 발급 실패 {e.code}: {msg}{hint}") from None
    with _lock:
        _token["value"] = d["access_token"]
        # 만료 60초 전에 갱신되도록 여유를 둔다
        _token["expires"] = time.time() + max(30, int(d.get("expires_in", 600)) - 60)
    log("access token issued")
    return _token["value"]


def account_seq(cfg):
    if _account["seq"] is not None:
        return _account["seq"]
    if cfg.get("account_seq"):
        _account["seq"] = int(cfg["account_seq"])
        return _account["seq"]
    res = call("GET", "/api/v1/accounts", cfg, account=False)
    items = (res.get("result") or {}).get("accounts") or res.get("result") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    brokerage = [a for a in items if a.get("accountType") == "BROKERAGE"] or items
    if not brokerage:
        raise RuntimeError("계좌를 찾을 수 없습니다. toss.json 에 account_seq 를 직접 지정하세요.")
    _account["seq"] = int(brokerage[0]["accountSeq"])
    _account["no"] = brokerage[0].get("accountNo")
    log(f"account resolved: {_account['no']} (seq {_account['seq']})")
    return _account["seq"]


class Handler(BaseHTTPRequestHandler):
    cfg = None
    origins = DEFAULT_ORIGINS

    def log_message(self, *a):                       # 기본 액세스 로그는 끈다
        pass

    # ---- helpers ----
    def _cors(self):
        origin = self.headers.get("Origin", "")
        allow = origin if origin in self.origins else self.origins[0]
        self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-MC-Auth")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _guard(self):
        """구글 계정이 맞아야 통과한다. 터널로 인터넷에 열려 있어도
           주소만 알아서는 아무것도 할 수 없게 하는 유일한 장치다."""
        ok, why = verify_google(self.headers.get("X-MC-Auth"), self.cfg)
        if not ok:
            self._send(401, {"error": why, "code": "auth"})
        return ok

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or "{}") if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ---- routes ----
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._guard():
            return
        try:
            if path == "/health":
                # 키가 실제로 통하는지까지 확인한다. account_seq 가 설정에 박혀 있으면
                # 계좌 조회를 건너뛰므로, 토큰을 받아 봐야 "연결됨" 이 거짓말이 아니게 된다.
                token(self.cfg)
                seq = account_seq(self.cfg)
                return self._send(200, {"ok": True, "accountSeq": seq, "accountNo": _account["no"],
                                        "allowedEmail": self.cfg.get("allowed_email")})
            if path == "/accounts":
                return self._send(200, call("GET", "/api/v1/accounts", self.cfg, account=False))
            if path == "/holdings":
                res = call("GET", "/api/v1/holdings", self.cfg)
                r = res.get("result") or {}
                return self._send(200, {"items": r.get("items") or [], "overview": r})
            if path == "/orders":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                qs = urllib.parse.urlencode({k: v[0] for k, v in q.items()}) or "status=OPEN"
                return self._send(200, call("GET", f"/api/v1/orders?{qs}", self.cfg))
            if path == "/candles":
                q = {k: v[0] for k, v in
                     urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                q.setdefault("interval", "1m")
                q.setdefault("count", "200")
                return self._send(200, call("GET", "/api/v1/candles?" + urllib.parse.urlencode(q),
                                            self.cfg, account=False))
            if path == "/orderbook":
                q = {k: v[0] for k, v in
                     urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                return self._send(200, call("GET", "/api/v1/orderbook?"
                                            + urllib.parse.urlencode(q), self.cfg, account=False))
            if path.startswith("/flow/"):
                # /flow/{symbol}/{kind}  kind: investor-trading | short-selling | credit-trades
                parts = path.split("/")
                if len(parts) < 4:
                    return self._send(400, {"error": "symbol 과 kind 가 필요합니다."})
                sym, kind = parts[2], parts[3]
                if kind not in ("investor-trading", "short-selling", "credit-trades"):
                    return self._send(400, {"error": f"알 수 없는 수급 항목: {kind}"})
                q = {k: v[0] for k, v in
                     urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                q.setdefault("count", "40")
                return self._send(200, call(
                    "GET", f"/api/v1/stocks/{urllib.parse.quote(sym)}/{kind}?"
                           + urllib.parse.urlencode(q), self.cfg, account=False))
            if path == "/buying-power":
                q = {k: v[0] for k, v in
                     urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
                q.setdefault("currency", "KRW")
                return self._send(200, call("GET", "/api/v1/buying-power?"
                                            + urllib.parse.urlencode(q), self.cfg))
            if path == "/commissions":
                return self._send(200, call("GET", "/api/v1/commissions", self.cfg, account=False))
            if path.startswith("/order/"):
                oid = path[len("/order/"):]
                return self._send(200, call("GET", f"/api/v1/orders/{urllib.parse.quote(oid)}", self.cfg))
            return self._send(404, {"error": "not found"})
        except Exception as e:                        # noqa: BLE001
            log(f"GET {path} failed: {e}")
            return self._send(502, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._guard():
            return
        try:
            if path == "/order":
                b = self._body()
                for k in ("symbol", "side", "orderType", "quantity"):
                    if not b.get(k):
                        return self._send(400, {"error": f"{k} 가 필요합니다."})
                order = {"symbol": b["symbol"], "side": b["side"],
                         "orderType": b["orderType"], "quantity": str(b["quantity"])}
                if b.get("price"):
                    order["price"] = str(b["price"])
                if b.get("timeInForce"):
                    order["timeInForce"] = b["timeInForce"]
                if b.get("clientOrderId"):
                    order["clientOrderId"] = b["clientOrderId"]
                log(f"ORDER {order['side']} {order['symbol']} x{order['quantity']} "
                    f"{order['orderType']}{' @' + order['price'] if order.get('price') else ''}")
                res = call("POST", "/api/v1/orders", self.cfg, body=order)
                return self._send(200, (res.get("result") or res))
            if path.startswith("/order/") and path.endswith("/cancel"):
                oid = path[len("/order/"):-len("/cancel")]
                log(f"CANCEL {oid}")
                res = call("POST", f"/api/v1/orders/{urllib.parse.quote(oid)}/cancel", self.cfg, body={})
                return self._send(200, (res.get("result") or res))
            return self._send(404, {"error": "not found"})
        except Exception as e:                        # noqa: BLE001
            log(f"POST {path} failed: {e}")
            return self._send(502, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="토스증권 Open API 로컬 브리지")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--origin", action="append", default=[],
                    help="허용할 브라우저 origin (여러 번 지정 가능)")
    ap.add_argument("--funnel", action="store_true",
                    help="Tailscale Funnel 을 함께 켜서 폰·태블릿에서도 접속 (인터넷에 열립니다)")
    ap.add_argument("--no-funnel", dest="funnel", action="store_false",
                    help="--funnel 을 끕니다")
    ap.set_defaults(funnel=None)
    a = ap.parse_args()

    Handler.cfg = load_config()
    Handler.origins = a.origin + DEFAULT_ORIGINS
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    who = Handler.cfg.get("allowed_email")

    # 설정 파일에 적어 두면 매번 옵션을 붙이지 않아도 된다
    want_funnel = a.funnel
    if want_funnel is None:
        want_funnel = bool(Handler.cfg.get("funnel"))
    public = funnel_up(a.port) if want_funnel else None
    print()
    print("  " + "─" * 62)
    if public:
        print(f"  브리지 주소 (어디서나):  {public}")
        print(f"  이 컴퓨터에서만:         http://localhost:{a.port}")
    else:
        print(f"  브리지 주소:  http://localhost:{a.port}")
    print(f"  허용된 구글 계정:  {who}")
    print("  " + "─" * 62)
    print("  대시보드 → 포트폴리오 → 설정 → 브리지 주소 에 넣고 [연결 확인].")
    print("  같은 구글 계정으로 로그인되어 있어야 합니다.")
    if public:
        print()
        print("  이 주소는 인터넷에 열려 있지만, 위 구글 계정으로 로그인한")
        print("  브라우저만 통과합니다. 창을 닫으면 함께 닫힙니다.")
    elif want_funnel:
        print()
        print("  폰 접속(Funnel)은 켜지 못했습니다 — 위 로그를 확인하세요.")
    else:
        print()
        print(f"  폰·태블릿에서도 쓰려면:  python3 {os.path.basename(sys.argv[0])} --funnel")
    print("  " + "─" * 62)
    print()
    log(f"listening on 127.0.0.1:{a.port}  (origins: {', '.join(Handler.origins)})")
    log(f"구글 계정 확인이 켜져 있습니다 — {who} 로 로그인한 브라우저만 통과합니다.")
    log("주문은 대시보드에서 확인 버튼을 눌러야 전송됩니다. 종료: Ctrl+C")
    # Ctrl+C 뿐 아니라 터미널을 닫거나 kill 로 끝낼 때도 funnel 을 정리한다.
    # 노출을 켠 채로 프로세스만 사라지는 상황을 만들지 않기 위해서다.
    def _stop(*_):
        threading.Thread(target=srv.shutdown, daemon=True).start()
    for sig in ("SIGTERM", "SIGHUP", "SIGINT"):
        try:
            signal.signal(getattr(signal, sig), _stop)
        except (AttributeError, ValueError, OSError):
            pass                                   # 윈도우에는 없는 신호가 있다

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if public:
            funnel_down(a.port)          # 창을 닫으면 인터넷 노출도 함께 닫는다
        log("bye")


if __name__ == "__main__":
    main()
