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
  if(!c.client_id || !c.client_secret){
    const e = new Error('확장 프로그램에 키가 없습니다. 설정 창에서 client_id / client_secret 을 넣어 주세요.');
    e.code = 'NO_KEYS';
    throw e;
  }
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


/* =====================================================================
   실시간 스트림 (웹소켓)
   토스는 handshake 에 Authorization 헤더를 요구하는데 WebSocket 생성자로는
   헤더를 붙일 수 없다. 그래서 declarativeNetRequest 로 그 요청에만 헤더를
   덧붙인 뒤 연결한다 — 확장이기에 가능한 일이다.

   구독은 선언형 full-replace 다. 지금 화면에 열린 종목 배열을 그대로 보내면
   빠진 종목은 자동 해제된다. 그래서 "보고 있는 것만" 이 자연스럽다.
   ===================================================================== */
const WS_URL = 'wss://openapi-ws.tossinvest.com/ws/v1';
const DNR_RULE_ID = 8931;
let ws = null, wsSubs = [], wsBackoff = 1000, wsPingTimer = null, wsRetryTimer = null;

/* 이 확장이 여는 웹소켓 요청에만 Authorization 헤더를 붙인다 */
async function armWsHeader(){
  const t = await token();
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [DNR_RULE_ID],
    addRules: [{
      id: DNR_RULE_ID,
      priority: 1,
      action: { type: 'modifyHeaders',
                requestHeaders: [{ header: 'Authorization', operation: 'set', value: 'Bearer ' + t }] },
      condition: { urlFilter: '||openapi-ws.tossinvest.com/', resourceTypes: ['websocket'] },
    }],
  });
}
async function disarmWsHeader(){
  try { await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [DNR_RULE_ID] }); }
  catch(e){}
}

/* 대시보드 탭들이 열어 둔 포트. tabs 권한 없이 푸시하려는 목적이다. */
const streamPorts = new Set();
chrome.runtime.onConnect.addListener(port => {
  if(port.name !== 'mc-stream') return;
  streamPorts.add(port);
  port.onDisconnect.addListener(() => {
    streamPorts.delete(port);
    if(!streamPorts.size) wsSetSymbols([]);      // 아무도 안 보면 연결을 놓는다
  });
});
function wsPush(msg){
  for(const p of streamPorts){
    try { p.postMessage({ __stream: true, ...msg }); } catch(e){ streamPorts.delete(p); }
  }
}

async function wsConnect(){
  if(ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(wsRetryTimer); wsRetryTimer = null;
  await armWsHeader();
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    wsBackoff = 1000;
    wsPush({ event: 'open' });
    wsDeclare();
    clearInterval(wsPingTimer);
    // 서버는 클라이언트 수신이 180초 없으면 끊는다. 받는 중이어도 타이머는 안 준다.
    wsPingTimer = setInterval(() => { try { ws.send('PING'); } catch(e){} }, 60000);
  };
  ws.onmessage = ev => {
    let d; try { d = JSON.parse(ev.data); } catch(e){ return; }   // "pong" 등 비 JSON 무시
    if(d.type === 'pong') return;
    wsPush({ event: 'frame', frame: d });
  };
  ws.onclose = () => {
    clearInterval(wsPingTimer); wsPingTimer = null;
    ws = null;
    wsPush({ event: 'close' });
    if(wsSubs.length) wsRetryTimer = setTimeout(wsConnect, wsBackoff = Math.min(wsBackoff * 2, 30000));
  };
  ws.onerror = () => wsPush({ event: 'error' });
}

/* 배열이 곧 구독 전체 — 빠진 항목은 서버가 알아서 해제한다 */
function wsDeclare(){
  if(!ws || ws.readyState !== WebSocket.OPEN) return;
  const kr = [], us = [];
  wsSubs.forEach(tk => (/^\d{6}$/.test(tk) ? kr : us).push(tk));
  const decl = [{ id: 'mc-' + Date.now() }];
  if(kr.length) decl.push({ type: 'trade:kr', codes: kr });
  if(us.length) decl.push({ type: 'trade:us', codes: us });
  try { ws.send(JSON.stringify(decl)); } catch(e){}
}

async function wsSetSymbols(list){
  const next = [...new Set((list || []).filter(Boolean))].slice(0, 40);
  const same = next.length === wsSubs.length && next.every(t => wsSubs.includes(t));
  wsSubs = next;
  if(!next.length){                       // 볼 것이 없으면 연결을 놓아 준다
    clearInterval(wsPingTimer); wsPingTimer = null;
    clearTimeout(wsRetryTimer); wsRetryTimer = null;
    if(ws){ try { ws.close(); } catch(e){} ws = null; }
    await disarmWsHeader();
    return { subscribed: [] };
  }
  if(!ws || ws.readyState !== WebSocket.OPEN) await wsConnect();
  else if(!same) wsDeclare();
  return { subscribed: next };
}

/* 설정이 끝났는지 — 배지와 자동 열기의 기준 */
async function configured(){
  const c = await cfg();
  return !!(c.client_id && c.client_secret);
}

async function refreshBadge(){
  const ok = await configured();
  chrome.action.setBadgeText({ text: ok ? '' : '!' });
  if(!ok){
    chrome.action.setBadgeBackgroundColor({ color: '#e5484d' });
    chrome.action.setTitle({ title: '토스증권 브리지 — 키를 넣어야 합니다. 눌러서 설정하세요.' });
  } else {
    chrome.action.setTitle({ title: '토스증권 브리지 — 설정됨' });
  }
}

/* 설치 직후 설정 창을 바로 띄운다. 사용자가 세부정보를 뒤져 옵션을
   찾아 들어가야 할 이유가 없다. */
chrome.runtime.onInstalled.addListener(details => {
  refreshBadge();
  if(details.reason === 'install') chrome.runtime.openOptionsPage();
  else configured().then(ok => { if(!ok) chrome.runtime.openOptionsPage(); });
});
chrome.runtime.onStartup?.addListener(refreshBadge);

/* 툴바 아이콘을 누르면 설정 창 */
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

/* 페이지가 부를 수 있는 것만 노출한다 — 키 읽기는 목록에 없다 */
async function handle(msg){
  const { op, path, body } = msg;
  if(op === 'setup'){                    // 대시보드가 "설정 창 열어 줘" 라고 부탁
    chrome.runtime.openOptionsPage();
    return { opened: true };
  }
  if(op === 'status') return { configured: await configured() };
  if(op === 'health'){
    await token();                       // 키가 실제로 통하는지까지 확인
    const s = await seq();
    return { ok: true, accountSeq: s, accountNo: acct.no, hasKeys: true };
  }
  if(op === 'get' && path === '/accounts') return await call('GET', '/api/v1/accounts', null, false);
  if(op === 'get' && path === '/holdings'){
    const res = await call('GET', '/api/v1/holdings');
    const r = res.result || {};
    return { items: r.items || [], overview: r };
  }
  if(op === 'get' && path === '/orders') return await call('GET', '/api/v1/orders');
  if(op === 'candles'){                  // 분봉·일봉 — 화면에 열린 종목만 부른다
    const q = new URLSearchParams({ symbol: body.symbol, interval: body.interval || '1m',
                                    count: String(body.count || 200) });
    if(body.before) q.set('before', body.before);
    return await call('GET', `/api/v1/candles?${q}`, null, false);
  }
  if(op === 'orderList'){                // 체결 추적용 주문 목록
    const q = new URLSearchParams({ status: body?.status || 'OPEN' });
    if(body?.symbol) q.set('symbol', body.symbol);
    if(body?.limit) q.set('limit', String(body.limit));
    return await call('GET', `/api/v1/orders?${q}`);
  }
  if(op === 'orderDetail') return await call('GET', `/api/v1/orders/${encodeURIComponent(body.orderId)}`);
  if(op === 'commissions') return await call('GET', '/api/v1/commissions', null, false);
  if(op === 'orderbook')
    return await call('GET', `/api/v1/orderbook?symbol=${encodeURIComponent(body.symbol)}`, null, false);
  if(op === 'flow'){                     // 수급 — 국내 종목만
    const q = new URLSearchParams({ count: String(body.count || 40) });
    if(body.until) q.set('until', body.until);
    return await call('GET',
      `/api/v1/stocks/${encodeURIComponent(body.symbol)}/${body.kind}?${q}`, null, false);
  }
  if(op === 'buyingPower'){              // 통화별 현금 매수 가능 금액
    const cur = body?.currency || 'KRW';
    return await call('GET', `/api/v1/buying-power?currency=${encodeURIComponent(cur)}`);
  }
  if(op === 'stream') return await wsSetSymbols(body?.symbols);   // 화면에 열린 종목만
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
             .catch(e => reply({ ok: false, error: String(e.message || e), code: e.code || null }));
  return true;                            // 비동기 응답
});

/* 키가 바뀌면 캐시를 버리고 배지를 다시 칠한다 */
chrome.storage.onChanged.addListener(() => {
  tok = { value: null, expires: 0 };
  acct = { seq: null, no: null };
  refreshBadge();
});
refreshBadge();
