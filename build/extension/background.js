/* 토스증권 Open API 중계 — 확장 프로그램판.
   브라우저 페이지는 CORS 때문에 openapi.tossinvest.com 을 직접 부를 수 없다.
   확장은 host_permissions 로 그 제약을 정식으로 벗어난다. 그래서 파이썬 없이도
   같은 일을 할 수 있다.

   키는 chrome.storage.local 에만 있고 페이지로 절대 넘기지 않는다.
   페이지는 "주문해 줘" 라고 부탁할 수 있을 뿐, 키를 읽을 수는 없다. */
const API = 'https://openapi.tossinvest.com';
let tok = { value: null, expires: 0 };
let acct = { seq: null, no: null };

const cfg = () => chrome.storage.local.get(['client_id', 'client_secret', 'account_seq']);

async function token(){
  if(tok.value && Date.now() < tok.expires) return tok.value;
  const c = await cfg();
  if(!c.client_id || !c.client_secret)
    throw new Error('키가 설정되지 않았습니다. 확장 프로그램 옵션에서 client_id / client_secret 을 넣어 주세요.');
  const r = await fetch(API + '/oauth2/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'client_credentials',
                                client_id: c.client_id, client_secret: c.client_secret }),
  });
  const text = await r.text();
  if(!r.ok){
    tok = { value: null, expires: 0 };
    throw new Error(`토큰 발급 실패 ${r.status}: ${text.slice(0, 200)}`
      + (r.status === 400 || r.status === 401 ? ' ← client_id / client_secret 을 확인하세요' : ''));
  }
  const d = JSON.parse(text);
  tok = { value: d.access_token, expires: Date.now() + Math.max(30, (d.expires_in || 600) - 60) * 1000 };
  return tok.value;
}

async function seq(){
  if(acct.seq != null) return acct.seq;
  const c = await cfg();
  if(c.account_seq){ acct.seq = Number(c.account_seq); return acct.seq; }
  const res = await call('GET', '/api/v1/accounts', null, false);
  const items = Array.isArray(res.result) ? res.result : (res.result?.accounts || []);
  const b = items.filter(a => a.accountType === 'BROKERAGE');
  const pick = (b.length ? b : items)[0];
  if(!pick) throw new Error('계좌를 찾을 수 없습니다. 옵션에서 계좌 번호를 직접 지정해 보세요.');
  acct = { seq: Number(pick.accountSeq), no: pick.accountNo };
  return acct.seq;
}

async function call(method, path, body, withAccount = true){
  for(const attempt of [1, 2]){
    const headers = { Authorization: `Bearer ${await token()}`, Accept: 'application/json' };
    if(withAccount) headers['X-Tossinvest-Account'] = String(await seq());
    if(body) headers['Content-Type'] = 'application/json';
    const r = await fetch(API + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
    const text = await r.text();
    if(r.status === 401 && attempt === 1){ tok = { value: null, expires: 0 }; continue; }
    if(!r.ok) throw new Error(`${r.status} ${text.slice(0, 240)}`);
    return text ? JSON.parse(text) : {};
  }
}

/* 페이지가 부를 수 있는 것만 노출한다 — 키 읽기는 목록에 없다 */
async function handle(msg){
  const { op, path, body } = msg;
  if(op === 'health'){
    await token();                       // 키가 실제로 통하는지까지 확인
    const s = await seq();
    return { ok: true, accountSeq: s, accountNo: acct.no };
  }
  if(op === 'get' && path === '/accounts') return await call('GET', '/api/v1/accounts', null, false);
  if(op === 'get' && path === '/holdings'){
    const res = await call('GET', '/api/v1/holdings');
    const r = res.result || {};
    return { items: r.items || [], overview: r };
  }
  if(op === 'get' && path === '/orders') return await call('GET', '/api/v1/orders');
  if(op === 'order'){
    for(const k of ['symbol', 'side', 'orderType', 'quantity'])
      if(!body?.[k]) throw new Error(`${k} 가 필요합니다.`);
    const o = { symbol: body.symbol, side: body.side, orderType: body.orderType,
                quantity: String(body.quantity) };
    if(body.price) o.price = String(body.price);
    if(body.timeInForce) o.timeInForce = body.timeInForce;
    if(body.clientOrderId) o.clientOrderId = body.clientOrderId;
    const res = await call('POST', '/api/v1/orders', o);
    return res.result || res;
  }
  if(op === 'cancel'){
    const res = await call('POST', `/api/v1/orders/${encodeURIComponent(body.orderId)}/cancel`, {});
    return res.result || res;
  }
  throw new Error('알 수 없는 요청: ' + op);
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  handle(msg).then(data => reply({ ok: true, data }))
             .catch(e => reply({ ok: false, error: String(e.message || e) }));
  return true;                            // 비동기 응답
});

/* 키가 바뀌면 캐시를 버린다 */
chrome.storage.onChanged.addListener(() => { tok = { value: null, expires: 0 }; acct = { seq: null, no: null }; });
