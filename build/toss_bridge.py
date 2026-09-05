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
      "client_id":     "발급받은 클라이언트 ID",
      "client_secret": "발급받은 시크릿",
      "account_seq":   0            // 생략하면 첫 BROKERAGE 계좌를 자동 사용
    }

실행
----
    python3 build/toss_bridge.py            # 127.0.0.1:8787
    python3 build/toss_bridge.py --port 9000 --origin https://example.github.io

127.0.0.1 에만 바인딩한다. 외부에 노출하지 말 것 — 이 포트에 닿을 수 있는
모든 프로그램이 당신의 계좌로 주문을 낼 수 있다.
"""
import argparse, gzip, json, os, sys, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://openapi.tossinvest.com"
CONFIG = os.path.expanduser("~/.money-connection/toss.json")
DEFAULT_ORIGINS = ["https://eloyyang.github.io", "http://localhost:8778", "http://127.0.0.1:8778"]

_lock = threading.Lock()
_token = {"value": None, "expires": 0}
_account = {"seq": None, "no": None}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit(f"설정 파일이 없습니다: {CONFIG}\n"
                 '{"client_id": "...", "client_secret": "...", "account_seq": 0} 형태로 만들어 주세요.')
    with open(CONFIG) as f:
        cfg = json.load(f)
    for k in ("client_id", "client_secret"):
        if not cfg.get(k):
            sys.exit(f"{CONFIG} 에 {k} 가 없습니다.")
    return cfg


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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

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
        try:
            if path == "/health":
                # 키가 실제로 통하는지까지 확인한다. account_seq 가 설정에 박혀 있으면
                # 계좌 조회를 건너뛰므로, 토큰을 받아 봐야 "연결됨" 이 거짓말이 아니게 된다.
                token(self.cfg)
                seq = account_seq(self.cfg)
                return self._send(200, {"ok": True, "accountSeq": seq, "accountNo": _account["no"]})
            if path == "/accounts":
                return self._send(200, call("GET", "/api/v1/accounts", self.cfg, account=False))
            if path == "/holdings":
                res = call("GET", "/api/v1/holdings", self.cfg)
                r = res.get("result") or {}
                return self._send(200, {"items": r.get("items") or [], "overview": r})
            if path == "/orders":
                return self._send(200, call("GET", "/api/v1/orders", self.cfg))
            return self._send(404, {"error": "not found"})
        except Exception as e:                        # noqa: BLE001
            log(f"GET {path} failed: {e}")
            return self._send(502, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
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
    a = ap.parse_args()

    Handler.cfg = load_config()
    Handler.origins = a.origin + DEFAULT_ORIGINS
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    addr = f"http://localhost:{a.port}"
    print()
    print("  " + "─" * 56)
    print(f"  브리지 주소:  {addr}")
    print("  이 주소를 대시보드 → 포트폴리오 → 설정 → '브리지 주소' 에 넣고")
    print("  [연결 확인] 을 누르세요.")
    print("  " + "─" * 56)
    print()
    log(f"listening on 127.0.0.1:{a.port}  (origins: {', '.join(Handler.origins)})")
    log("주문은 대시보드에서 확인 버튼을 눌러야 전송됩니다. 종료: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("bye")


if __name__ == "__main__":
    main()
