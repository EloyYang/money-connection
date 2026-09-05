/* 페이지 ↔ 확장 사이의 유일한 통로.
   페이지는 postMessage 로 "이 일을 해 줘" 라고만 말할 수 있고,
   키는 이 경로로 절대 되돌아가지 않는다. */
const TAG_IN = 'mc-page', TAG_OUT = 'mc-ext';

window.addEventListener('message', ev => {
  if(ev.source !== window) return;                     // 다른 프레임 무시
  const d = ev.data;
  if(!d || d.source !== TAG_IN || !d.id) return;
  if(d.op === 'ping'){ hello(); return; }        // 페이지가 먼저 물어볼 수 있게
  chrome.runtime.sendMessage({ op: d.op, path: d.path, body: d.body }, res => {
    const err = chrome.runtime.lastError;
    window.postMessage({ source: TAG_OUT, id: d.id,
      ok: !err && res?.ok,
      data: res?.data,
      code: res?.code || null,
      error: err ? err.message : res?.error }, window.location.origin);
  });
});

/* 실시간 프레임은 요청-응답이 아니라 푸시라 별도 포트로 받는다 */
let streamPort = null;
function openStream(){
  try {
    streamPort = chrome.runtime.connect({ name: 'mc-stream' });
    streamPort.onMessage.addListener(m => {
      if(m && m.__stream) window.postMessage({ source: TAG_OUT, stream: m }, window.location.origin);
    });
    streamPort.onDisconnect.addListener(() => { streamPort = null; setTimeout(openStream, 1500); });
  } catch(e){ streamPort = null; }
}
openStream();

/* 설치되어 있다는 사실을 페이지에 알린다 */
function hello(){
  window.postMessage(
    { source: TAG_OUT, hello: true, version: chrome.runtime.getManifest().version },
    window.location.origin);
}
hello();
document.addEventListener('DOMContentLoaded', hello);
window.addEventListener('load', hello);
