const $ = id => document.getElementById(id);
const msg = (t, c) => { $('msg').textContent = t; $('msg').style.color = c || 'inherit'; };

chrome.storage.local.get(['client_id', 'client_secret', 'account_seq'], c => {
  $('id').value = c.client_id || '';
  $('secret').value = c.client_secret || '';
  $('seq').value = c.account_seq || '';
});

$('save').addEventListener('click', () => {
  const v = { client_id: $('id').value.trim(), client_secret: $('secret').value.trim() };
  const s = parseInt($('seq').value, 10);
  v.account_seq = Number.isFinite(s) ? s : '';
  if(!v.client_id || !v.client_secret){ msg('client_id 와 client_secret 을 모두 넣어 주세요.', '#c00'); return; }
  chrome.storage.local.set(v, () => msg('저장했습니다. [연결 확인]을 눌러 보세요.', '#087'));
});

$('test').addEventListener('click', () => {
  msg('확인 중…');
  chrome.runtime.sendMessage({ op: 'health' }, res => {
    if(chrome.runtime.lastError){ msg(chrome.runtime.lastError.message, '#c00'); return; }
    if(res?.ok) msg(`연결됨 · 계좌 ${res.data.accountNo || '?'} (seq ${res.data.accountSeq})`, '#087');
    else msg(res?.error || '실패했습니다.', '#c00');
  });
});

$('clear').addEventListener('click', () => {
  if(!confirm('저장된 키를 지웁니다. 계속할까요?')) return;
  chrome.storage.local.clear(() => {
    $('id').value = $('secret').value = $('seq').value = '';
    msg('지웠습니다.', '#087');
  });
});
