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
  chrome.storage.local.set(v, () => {
    msg('저장했습니다. 이어서 [연결 확인]을 눌러 보세요.', '#087');
    $('test').click();                       // 저장했으면 바로 확인까지 해 준다
  });
});

$('test').addEventListener('click', () => {
  msg('확인 중…');
  chrome.runtime.sendMessage({ op: 'health' }, res => {
    if(chrome.runtime.lastError){ msg(chrome.runtime.lastError.message, '#c00'); return; }
    if(res?.ok){
      msg(`연결됨 · 계좌 ${res.data.accountNo || '?'} (seq ${res.data.accountSeq})`, '#087');
      const d = $('done');
      d.hidden = false;
      d.innerHTML = '설정이 끝났습니다. '
        + '<a href="https://eloyyang.github.io/money-connection/" target="_blank" rel="noopener noreferrer">'
        + '머니 커넥션으로 돌아가기 ↗</a><br>'
        + '대시보드를 이미 열어 두셨다면 <b>새로고침</b>하면 인식됩니다. '
        + '이 창은 닫으셔도 됩니다.';
      $('how').hidden = true;
    }
    else { msg(res?.error || '실패했습니다.', '#c00'); $('done').hidden = true; }
  });
});

$('clear').addEventListener('click', () => {
  if(!confirm('저장된 키를 지웁니다. 계속할까요?')) return;
  chrome.storage.local.clear(() => {
    $('id').value = $('secret').value = $('seq').value = '';
    msg('지웠습니다.', '#087');
    $('done').hidden = true;
    $('how').hidden = false;
  });
});
