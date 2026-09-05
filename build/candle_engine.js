/* ---------------------------------------------------------------------
   Chart geometry from the last render — drag handlers convert pixels back
   into (index, price) through this, so drawings stay anchored to the data.

   The chart can be split into up to four panes. Rather than thread a context
   object through 350 lines, each pane OWNS its {tk, i0, i1, yManual, svg} and
   the working globals below are the active pane's copy; focusPane() swaps them.
   Every handler is created inside drawCandle(), so it closes over the index of
   the pane it belongs to and focuses it before doing anything.
   --------------------------------------------------------------------- */
let geom = null, cSel = null, yManual = null, activeDrag = null;
const PANES = [];              // { tk, i0, i1, yManual, host, sel }
let activeP = 0;

function stashPane(){
  const p = PANES[activeP];
  if(p){ p.tk = cTk; p.i0 = cI0; p.i1 = cI1; p.yManual = yManual; p.ind = IND; }
}
function loadPane(i){
  const p = PANES[i];
  if(!p) return;
  activeP = i;
  cTk = p.tk; cI0 = p.i0; cI1 = p.i1; yManual = p.yManual;
  // the same object, not a copy: toggling an indicator writes straight through
  // to the pane it belongs to
  if(p.ind) IND = p.ind;
  bigSvg = p.sel;
}
function focusPane(i){
  if(i === activeP || !PANES[i]) return;
  stashPane();
  cSel = null; selBar.hidden = true;
  loadPane(i);
  markActivePane();
  syncChartHeader();
}
function markActivePane(){
  PANES.forEach((p, i) => p.host.classList.toggle('active', i === activeP && PANES.length > 1));
}
/* draw every pane; the header only ever reflects the active one */
function drawAllPanes(){
  stashPane();
  const keep = activeP;
  PANES.forEach((_, i) => { loadPane(i); drawCandle(true); });
  loadPane(keep);
  markActivePane();
  syncChartHeader();
}

/* Everything that belongs to "the chart you are working on" is rebound here:
   the header, the indicator toolbar, the summary and the order ticket. */
function syncChartHeader(){
  const tk = cTk, node = nodeIndex.get(tk);
  if(!tk) return;
  document.getElementById('m-tk').textContent = tk;
  document.getElementById('m-tk').style.color = node ? node.color : 'var(--text-primary)';
  document.getElementById('m-nm').textContent = node ? node.name : '';
  // the quote used to be written only at the end of drawCandle(), so switching
  // panes left the previous chart's price beside the new ticker
  const rows = ohlcOf(tk);
  let li = rows.length - 1; while(li >= 0 && !rows[li]) li--;
  let pi = li - 1;          while(pi >= 0 && !rows[pi]) pi--;
  if(li >= 0){
    const last = rows[li], prev = pi >= 0 ? rows[pi] : null;
    document.getElementById('m-px').textContent = fmtPx(last[3], node?.currency || 'USD');
    const d = prev && prev[3] ? (last[3] - prev[3]) / prev[3] * 100 : 0;
    const el = document.getElementById('m-chg');
    el.textContent = `${d >= 0 ? '+' : ''}${d.toFixed(2)}%  (${OH.dates[li]})`;
    el.style.color = d >= 0 ? '#3ddc84' : '#ff5c72';
  }
  selectedTk = tk;
  // a comparison in progress is the user's, not the pane's — leave it alone
  if(compareSet.length <= 1) compareSet = [tk];
  if(typeof renderIndButtons === 'function') renderIndButtons();
  if(typeof renderTvSummary === 'function') renderTvSummary();
  if(typeof updateTicket === 'function') updateTicket();
  if(typeof updateMiniTicket === 'function') updateMiniTicket();
  if(typeof renderTvList === 'function') renderTvList();
}

function updateToolButtons(){
  document.getElementById('tool-trend').classList.toggle('on', cTool === 'trend');
  document.getElementById('tool-hline').classList.toggle('on', cTool === 'hline');
}

/* ---- selection toolbar (복사 / 삭제) ---- */
const selBar = document.getElementById('sel-bar');
function positionSelBar(){
  if(!cSel || !geom){ selBar.hidden = true; return; }
  let px, py;
  if(cSel.type === 'hline'){ px = geom.W*0.5; py = geom.y(cSel.y); }
  else { px = (geom.x(cSel.x1)+geom.x(cSel.x2))/2; py = (geom.y(cSel.y1)+geom.y(cSel.y2))/2; }
  selBar.hidden = false;
  selBar.style.left = Math.max(6, Math.min(geom.W-140, px-64)) + 'px';
  selBar.style.top  = Math.max(6, Math.min(geom.H-40, py-42)) + 'px';
}
function selectDrawing(d){ cSel = d; drawCandle(); }
function clearSelection(){ cSel = null; selBar.hidden = true; drawCandle(); }
function deleteSelected(){
  if(!cSel) return;
  const arr = drawings.get(cTk) || [];
  const i = arr.indexOf(cSel);
  if(i >= 0) arr.splice(i, 1);
  cSel = null; selBar.hidden = true; drawCandle();
}
function copySelected(){
  if(!cSel || !geom) return;
  const off = (geom.hi - geom.lo) * 0.04;           // paste slightly above the original
  const copy = cSel.type === 'hline'
    ? { type:'hline', y: cSel.y + off }
    : { type:'trend', x1:cSel.x1, y1:cSel.y1+off, x2:cSel.x2, y2:cSel.y2+off };
  pushDrawing(copy);
  cSel = copy;
  drawCandle();
}
document.getElementById('sel-copy').addEventListener('click', ev=>{ ev.stopPropagation(); copySelected(); });
document.getElementById('sel-del').addEventListener('click', ev=>{ ev.stopPropagation(); deleteSelected(); });
document.addEventListener('keydown', ev=>{
  if(!document.body.classList.contains('view-trade')) return;
  if((ev.key === 'Delete' || ev.key === 'Backspace') && cSel){ ev.preventDefault(); deleteSelected(); }
  if((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === 'd' && cSel){ ev.preventDefault(); copySelected(); }
});

/* ---- drag machinery: listeners live on window so a drag survives leaving the plot ---- */
function beginDrag(kind, ev, extra){
  activeDrag = Object.assign({ kind, px: ev.clientX, py: ev.clientY }, extra || {});
  window.addEventListener('mousemove', onDragMove);
  window.addEventListener('mouseup', endDrag);
  ev.preventDefault();
}
function endDrag(){
  activeDrag = null;
  window.removeEventListener('mousemove', onDragMove);
  window.removeEventListener('mouseup', endDrag);
}
function onDragMove(ev){
  if(!activeDrag || !geom) return;
  const dx = ev.clientX - activeDrag.px, dy = ev.clientY - activeDrag.py;
  const rows = ohlcOf(cTk), maxSpan = rows.length - 1;
  const a = activeDrag;

  if(a.kind === 'pan'){
    const dIdx = -dx / geom.step;
    const span = a.i1 - a.i0;
    let n0 = a.i0 + dIdx, n1 = a.i1 + dIdx;
    if(n0 < 0){ n0 = 0; n1 = span; }
    if(n1 > maxSpan){ n1 = maxSpan; n0 = n1 - span; }
    cI0 = n0; cI1 = n1;

  } else if(a.kind === 'zoomx'){
    // drag right = 확대, drag left = 축소; the newest candle stays pinned
    const f = Math.max(0.15, 1 + dx/220);
    let span = (a.i1 - a.i0) / f;
    span = Math.max(12, Math.min(maxSpan, span));
    cI1 = a.i1; cI0 = Math.max(0, cI1 - span);
    if(cI0 === 0) cI1 = Math.min(maxSpan, span);

  } else if(a.kind === 'zoomy'){
    // drag down = 가격축 축소(넓게), drag up = 확대
    const f = Math.max(0.15, 1 + dy/220);
    const c = (a.lo + a.hi)/2, half = (a.hi - a.lo)/2 * f;
    yManual = { lo: c - half, hi: c + half };

  } else if(a.kind === 'move-line'){
    const dIdx = dx / geom.step, dP = geom.invY(geom.py0 + dy) - geom.invY(geom.py0);
    if(a.obj.type === 'hline'){ a.obj.y = a.orig.y + dP; }
    else {
      a.obj.x1 = a.orig.x1 + dIdx; a.obj.x2 = a.orig.x2 + dIdx;
      a.obj.y1 = a.orig.y1 + dP;   a.obj.y2 = a.orig.y2 + dP;
    }
  } else if(a.kind === 'move-end'){
    const idx = geom.invX(ev.clientX - geom.rect.left);
    const price = geom.invY(ev.clientY - geom.rect.top);
    a.obj['x'+a.end] = Math.max(0, Math.min(maxSpan, idx));
    a.obj['y'+a.end] = price;
  }
  drawCandle();
}

/* ---- render ---- */
function drawCandle(silent){
  if(!cTk || !PANES[activeP]) return;
  // the globals ARE the active pane's state; write them back on every render so
  // switching panes never loses a range someone just zoomed into
  stashPane();
  const myPane = activeP;
  const host = PANES[activeP].host;
  const W = host.clientWidth, H = host.clientHeight;
  if(W < 40 || H < 40) return;      // 다른 탭에 있으면 0px 이다
  bigSvg.attr('viewBox', `0 0 ${W} ${H}`);
  bigSvg.selectAll('*').remove();

  const rows = ohlcOf(cTk);
  const i0 = Math.max(0, Math.round(cI0)), i1 = Math.min(rows.length-1, Math.round(cI1));
  const vis = [];
  for(let i=i0;i<=i1;i++) if(rows[i]) vis.push({ i, d: rows[i] });
  if(!vis.length) return;

  const plotW = W - CM.l - CM.r;
  const plotH = H - CM.t - CM.b;
  const subOn = IND.sub !== 'none';
  const volH = plotH * CM.volH;
  const subH = subOn ? plotH * 0.20 : 0;
  const priceH = plotH - volH - subH - CM.gap * (subOn ? 2 : 1);
  let lo, hi;
  if(yManual){ lo = yManual.lo; hi = yManual.hi; }
  else {
    const dLo = Math.min(...vis.map(v=>v.d[2])), dHi = Math.max(...vis.map(v=>v.d[1]));
    const pad = (dHi-dLo)*0.06 || 1;
    lo = dLo - pad; hi = dHi + pad;
  }
  const x = i => CM.l + ((i - i0)/Math.max(1,(i1-i0))) * plotW;
  const y = p => CM.t + priceH - ((p - lo)/(hi - lo))*priceH;
  const invX = px => i0 + (px - CM.l)/plotW*(i1-i0);
  const invY = py => hi - ((py - CM.t)/priceH)*(hi - lo);
  const step = plotW/Math.max(1,(i1-i0));
  geom = { x, y, invX, invY, i0, i1, W, H, plotW, priceH, lo, hi, step,
           py0: CM.t + priceH/2, rect: bigSvg.node().getBoundingClientRect() };

  const volTop = CM.t + priceH + CM.gap;
  const subTop = volTop + volH + CM.gap;
  const maxVol = Math.max(1, ...vis.map(v=>v.d[4]));
  const vy = v => volTop + volH - (v/maxVol)*volH;
  const bw = Math.max(1, Math.min(14, step*0.7));
  const UP = '#3ddc84', DOWN = '#ff5c72';

  d3.ticks(lo, hi, 6).forEach(t=>{
    svgLine(CM.l, y(t), W-CM.r, y(t), 'rgba(255,255,255,0.055)');
    bigSvg.append('text').attr('x',W-CM.r+7).attr('y',y(t)+3.5).attr('font-size',10)
      .attr('fill','#6b7380').attr('font-family',"'JetBrains Mono',monospace")
      .text(t >= 1000 ? Math.round(t) : t);
  });
  const nLab = Math.max(2, Math.min(8, Math.floor(plotW/110)));
  for(let k=0;k<=nLab;k++){
    const idx = Math.round(i0 + (i1-i0)*k/nLab), lab = OH.dates[idx];
    if(!lab) continue;
    bigSvg.append('text').attr('x',x(idx)).attr('y',H-8)
      .attr('text-anchor', k===0?'start':k===nLab?'end':'middle')
      .attr('font-size',10).attr('fill','#6b7380').attr('font-family',"'JetBrains Mono',monospace")
      .text((i1-i0) > 200 ? lab.slice(0,7) : lab.slice(2));
  }

  vis.forEach(v=>{
    if(!v.d[4]) return;
    bigSvg.append('rect').attr('x', x(v.i)-bw/2).attr('width', bw)
      .attr('y', vy(v.d[4])).attr('height', Math.max(0.5, CM.t+priceH+CM.gap+volH - vy(v.d[4])))
      .attr('fill', v.d[3] >= v.d[0] ? UP : DOWN).attr('opacity',0.30);
  });

  const closeOnly = (OH.closeOnly||[]).includes(cTk);
  if(closeOnly){
    bigSvg.append('path').datum(vis).attr('d', d3.line().x(d=>x(d.i)).y(d=>y(d.d[3])))
      .attr('fill','none').attr('stroke', nodeIndex.get(cTk)?.color || UP).attr('stroke-width',1.6);
  } else if(vis.length > 420){
    let pu = '', pd = '';
    vis.forEach(v=>{
      const seg = `M${x(v.i).toFixed(1)},${y(v.d[1]).toFixed(1)}L${x(v.i).toFixed(1)},${y(v.d[2]).toFixed(1)}`;
      (v.d[3] >= v.d[0] ? pu += seg : pd += seg);
    });
    bigSvg.append('path').attr('d',pu).attr('stroke',UP).attr('stroke-width',Math.max(1,bw)).attr('fill','none');
    bigSvg.append('path').attr('d',pd).attr('stroke',DOWN).attr('stroke-width',Math.max(1,bw)).attr('fill','none');
  } else {
    vis.forEach(v=>{
      const [o,h,l,c] = v.d, up = c >= o, col = up ? UP : DOWN, cx = x(v.i);
      svgLine(cx, y(h), cx, y(l), col, 1);
      const top = y(Math.max(o,c)), bot = y(Math.min(o,c));
      bigSvg.append('rect').attr('x',cx-bw/2).attr('y',top).attr('width',bw)
        .attr('height', Math.max(1, bot-top)).attr('fill',col).attr('opacity', up?0.9:0.95);
    });
  }

  drawIndicators({ rows, vis, i0, i1, x, y, bigSvg, W, plotW, volTop, volH, subTop, subH, maxVol, bw });

  /* crosshair */
  const cross = bigSvg.append('g').style('display','none').style('pointer-events','none');
  const chX = cross.append('line').attr('y1',CM.t).attr('y2',subTop + subH)
    .attr('stroke','rgba(255,255,255,0.3)').attr('stroke-dasharray','3,3');
  const chY = cross.append('line').attr('x1',CM.l).attr('x2',W-CM.r)
    .attr('stroke','rgba(255,255,255,0.3)').attr('stroke-dasharray','3,3');
  const chLab = cross.append('text').attr('x',W-CM.r+7).attr('font-size',10).attr('fill','var(--accent)')
    .attr('font-family',"'JetBrains Mono',monospace");
  const readout = document.getElementById('m-readout');
  const idxAt = px => Math.max(i0, Math.min(i1, Math.round(invX(px))));

  /* plot surface: pan + crosshair + drawing tools.
     Sits above the candles (so hovering a candle body still tracks) and below
     the drawn lines (so those stay clickable). */
  bigSvg.append('rect').attr('x',0).attr('y',0).attr('width',W).attr('height',H)
    .attr('fill','transparent')
    .style('cursor', cTool ? 'crosshair' : 'grab')
    .on('mouseleave', ()=> cross.style('display','none'))
    .on('mousemove', function(ev){
      const [mx,my] = d3.pointer(ev, this);
      let idx = idxAt(mx); while(idx > i0 && !rows[idx]) idx--;
      const r = rows[idx]; if(!r) return;
      cross.style('display',null);
      chX.attr('x1',x(idx)).attr('x2',x(idx));
      chY.attr('y1',my).attr('y2',my);
      chLab.attr('y',my+3.5).text(invY(my).toFixed(2));
      const chg = (r[3]-r[0])/r[0]*100;
      readout.innerHTML = `${OH.dates[idx]}  시가 <b>${r[0]}</b>  고가 <b>${r[1]}</b>  저가 <b>${r[2]}</b>  ` +
        `종가 <b style="color:${r[3]>=r[0]?UP:DOWN}">${r[3]}</b>  ` +
        `<span style="color:${chg>=0?UP:DOWN}">${chg>=0?'+':''}${chg.toFixed(2)}%</span>` +
        (r[4] ? `  거래량 ${(r[4]/1000).toFixed(1)}M` : '');
      if(cPending && cTool === 'trend'){ cPending.x2 = idxAt(mx); cPending.y2 = invY(my); drawCandle(); }
    })
    .on('mousedown', function(ev){
      focusPane(myPane);
      const [mx,my] = d3.pointer(ev, this);
      if(cTool === 'hline'){ pushDrawing({ type:'hline', y: invY(my) }); cTool = null; updateToolButtons(); drawCandle(); return; }
      if(cTool === 'trend'){
        if(!cPending){ cPending = { type:'trend', x1: idxAt(mx), y1: invY(my), x2: idxAt(mx), y2: invY(my) }; }
        else {
          cPending.x2 = idxAt(mx); cPending.y2 = invY(my);
          if(cPending.x1 !== cPending.x2 || cPending.y1 !== cPending.y2) pushDrawing(cPending);
          cPending = null;
          cTool = null; updateToolButtons();      // 한 번 그리면 도구 해제
        }
        drawCandle(); return;
      }
      if(cSel){ clearSelection(); return; }
      beginDrag('pan', ev, { i0:cI0, i1:cI1 });
    })
    .on('wheel', function(ev){
      ev.preventDefault();
      focusPane(myPane);
      const [mx] = d3.pointer(ev, this);
      const maxSpan = rows.length - 1, minSpan = 12;

      // Devices differ wildly: a mouse wheel sends one big notch, a trackpad a
      // stream of small deltas, and pinch-to-zoom arrives as ctrl+wheel. Scale
      // the zoom by the delta itself instead of applying a fixed step.
      const unit = ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? 400 : 1;
      const dy = ev.deltaY * unit, dx = ev.deltaX * unit;

      // two-finger horizontal swipe pans the time axis
      if(!ev.ctrlKey && Math.abs(dx) > Math.abs(dy) * 1.4){
        const span = cI1 - cI0;
        let n0 = cI0 + dx / step, n1 = cI1 + dx / step;
        if(n0 < 0){ n0 = 0; n1 = span; }
        if(n1 > maxSpan){ n1 = maxSpan; n0 = n1 - span; }
        cI0 = n0; cI1 = n1;
        drawCandle();
        return;
      }

      const k = ev.ctrlKey ? 0.012 : 0.0017;   // pinch deltas are much smaller
      const f = Math.min(3, Math.max(1/3, Math.exp(dy * k)));
      const anchor = idxAt(mx);
      let s0 = anchor - (anchor - cI0) * f, s1 = anchor + (cI1 - anchor) * f;
      if(s1 - s0 < minSpan){ const c = (s0 + s1) / 2; s0 = c - minSpan/2; s1 = c + minSpan/2; }
      if(s1 - s0 > maxSpan){ s0 = 0; s1 = maxSpan; }
      cI0 = Math.max(0, s0); cI1 = Math.min(maxSpan, s1);
      document.querySelectorAll('#m-range button').forEach(b => b.classList.remove('on'));
      drawCandle();
    });

  drawUserLines(x, y, W);   // above the surface: lines stay selectable

  /* axis drag zones */
  bigSvg.append('rect').attr('x',CM.l).attr('y',H-CM.b-6).attr('width',plotW).attr('height',CM.b+6)
    .attr('fill','transparent').style('cursor','ew-resize')
    .on('mousedown', ev => { focusPane(myPane); beginDrag('zoomx', ev, { i0:cI0, i1:cI1 }); })
    .on('dblclick', ()=>{ setRange(252); document.querySelectorAll('#m-range button')
        .forEach(b=>b.classList.toggle('on', b.dataset.d==='252')); })
    .append('title').text('좌우로 드래그하면 기간 확대/축소 · 더블클릭 = 1년');
  bigSvg.append('rect').attr('x',W-CM.r).attr('y',CM.t).attr('width',CM.r).attr('height',priceH)
    .attr('fill','transparent').style('cursor','ns-resize')
    .on('mousedown', ev => { focusPane(myPane); beginDrag('zoomy', ev, { lo, hi }); })
    .on('dblclick', ()=>{ yManual = null; drawCandle(); })
    .append('title').text('위아래로 드래그하면 가격축 확대/축소 · 더블클릭 = 자동 맞춤');

  positionSelBar();

  const last = vis[vis.length-1].d, prev = vis.length > 1 ? vis[vis.length-2].d : null;
  if(silent) return;      // 패널 이름표는 HTML 칩이 담당한다
  document.getElementById('m-px').textContent = fmtPx(last[3], nodeIndex.get(cTk)?.currency || 'USD');
  const dChg = prev ? (last[3]-prev[3])/prev[3]*100 : 0;
  const chgEl = document.getElementById('m-chg');
  chgEl.textContent = `${dChg>=0?'+':''}${dChg.toFixed(2)}%  (${OH.dates[vis[vis.length-1].i]})`;
  chgEl.style.color = dChg >= 0 ? UP : DOWN;
  document.getElementById('m-yfit').classList.toggle('on', !!yManual);
}

function svgLine(x1,y1,x2,y2,stroke,w){
  bigSvg.append('line').attr('x1',x1).attr('y1',y1).attr('x2',x2).attr('y2',y2)
    .attr('stroke',stroke).attr('stroke-width', w||1);
}
function pushDrawing(d){
  if(!drawings.has(cTk)) drawings.set(cTk, []);
  drawings.get(cTk).push(d);
}

function drawUserLines(x, y, W){
  const list = (drawings.get(cTk) || []).concat(cPending ? [cPending] : []);
  list.forEach(d=>{
    const isPending = cPending && d === cPending;
    const isSel = cSel === d;
    const g = bigSvg.append('g').style('cursor', isPending ? 'crosshair' : 'move');
    let x1,y1,x2,y2;
    if(d.type === 'hline'){ x1 = CM.l; x2 = W - CM.r; y1 = y2 = y(d.y); }
    else { x1 = x(d.x1); y1 = y(d.y1); x2 = x(d.x2); y2 = y(d.y2); }

    g.append('line').attr('x1',x1).attr('y1',y1).attr('x2',x2).attr('y2',y2)
      .attr('stroke','transparent').attr('stroke-width',14);
    g.append('line').attr('x1',x1).attr('y1',y1).attr('x2',x2).attr('y2',y2)
      .attr('stroke', isSel ? '#ffffff' : 'var(--accent)')
      .attr('stroke-width', isPending ? 1.2 : (isSel ? 2.2 : 1.6))
      .attr('stroke-dasharray', isPending ? '5,4' : (d.type==='hline' ? '6,4' : null))
      .attr('opacity', isPending ? 0.8 : 0.95);

    if(d.type === 'hline'){
      g.append('text').attr('x', W - CM.r - 6).attr('y', y1 - 5).attr('text-anchor','end')
        .attr('font-size',10).attr('fill', isSel ? '#ffffff' : 'var(--accent)')
        .attr('font-family',"'JetBrains Mono',monospace").text(d.y.toFixed(2));
    }
    if(isPending) return;

    // click selects; dragging the body moves the whole line
    g.on('mousedown', ev=>{
      ev.stopPropagation();
      cSel = d;
      beginDrag('move-line', ev, { obj:d, orig:Object.assign({}, d) });
      drawCandle();
    });

    if(isSel && d.type === 'trend'){
      [[1,x1,y1],[2,x2,y2]].forEach(([end,hx,hy])=>{
        bigSvg.append('circle').attr('cx',hx).attr('cy',hy).attr('r',5)
          .attr('fill','#0a0c10').attr('stroke','#ffffff').attr('stroke-width',1.8)
          .style('cursor','crosshair')
          .on('mousedown', ev=>{ ev.stopPropagation(); beginDrag('move-end', ev, { obj:d, end }); });
      });
    } else if(isSel && d.type === 'hline'){
      bigSvg.append('circle').attr('cx',(x1+x2)/2).attr('cy',y1).attr('r',5)
        .attr('fill','#0a0c10').attr('stroke','#ffffff').attr('stroke-width',1.8)
        .style('cursor','ns-resize')
        .on('mousedown', ev=>{ ev.stopPropagation();
          beginDrag('move-line', ev, { obj:d, orig:Object.assign({}, d) }); });
    }
  });
}

// resize is handled by the trading view, which knows when the pane is visible

/* =====================================================================
   INDICATORS

   Every series is computed over the FULL history and then sliced to the
   visible window, not computed on the visible slice — a 120-day moving
   average that restarts when you zoom in is not a 120-day moving average.
   ===================================================================== */
const IND_KEY = 'money-connection.indicators';
const MA_DEFS = [
  { n: 5,   c: '#5fd0ff' }, { n: 20,  c: '#ffb703' },
  { n: 60,  c: '#f072b6' }, { n: 120, c: '#7ef0a8' },
];
let IND = { ma: [20], bb: false, vma: false, sub: 'none' };
try { IND = { ...IND, ...JSON.parse(localStorage.getItem(IND_KEY) || '{}') }; } catch(e){}
const saveInd = () => { try { localStorage.setItem(IND_KEY, JSON.stringify(IND)); } catch(e){} };

function smaSeries(vals, n){
  const out = new Array(vals.length).fill(null);
  let sum = 0, count = 0;
  for(let i = 0; i < vals.length; i++){
    const v = vals[i];
    if(v == null){ sum = 0; count = 0; continue; }   // 결측이 나오면 창을 다시 채운다
    sum += v; count++;
    if(count > n) sum -= vals[i - n], count = n;
    if(count === n) out[i] = sum / n;
  }
  return out;
}
function stdevSeries(vals, n, mean){
  const out = new Array(vals.length).fill(null);
  for(let i = n - 1; i < vals.length; i++){
    if(mean[i] == null) continue;
    let s = 0, ok = true;
    for(let k = 0; k < n; k++){
      const v = vals[i - k];
      if(v == null){ ok = false; break; }
      s += (v - mean[i]) ** 2;
    }
    if(ok) out[i] = Math.sqrt(s / n);
  }
  return out;
}
function emaSeries(vals, n){
  const out = new Array(vals.length).fill(null);
  const k = 2 / (n + 1);
  let prev = null;
  for(let i = 0; i < vals.length; i++){
    const v = vals[i];
    if(v == null) continue;
    prev = prev == null ? v : v * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}
function rsiSeries(vals, n){
  const out = new Array(vals.length).fill(null);
  let ag = null, al = null, prev = null;
  let gains = [], losses = [];
  for(let i = 0; i < vals.length; i++){
    const v = vals[i];
    if(v == null) continue;
    if(prev != null){
      const ch = v - prev;
      const g = Math.max(0, ch), l = Math.max(0, -ch);
      if(ag == null){
        gains.push(g); losses.push(l);
        if(gains.length === n){
          ag = gains.reduce((a, b) => a + b, 0) / n;
          al = losses.reduce((a, b) => a + b, 0) / n;
        }
      } else {                                        // Wilder smoothing
        ag = (ag * (n - 1) + g) / n;
        al = (al * (n - 1) + l) / n;
      }
      if(ag != null) out[i] = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
    }
    prev = v;
  }
  return out;
}

/* cache per ticker: the whole-history maths only changes when the symbol does */
let _indCache = { tk: null };
function indicatorsFor(tk){
  if(_indCache.tk === tk) return _indCache;
  const rows = ohlcOf(tk);
  const close = rows.map(r => r ? r[3] : null);
  const vol = rows.map(r => r ? r[4] : null);
  const ma = {};
  MA_DEFS.forEach(d => { ma[d.n] = smaSeries(close, d.n); });
  const bbMid = ma[20] || smaSeries(close, 20);
  const sd = stdevSeries(close, 20, bbMid);
  const e12 = emaSeries(close, 12), e26 = emaSeries(close, 26);
  const macd = close.map((_, i) => (e12[i] == null || e26[i] == null) ? null : e12[i] - e26[i]);
  const signal = emaSeries(macd, 9);
  _indCache = { tk, close, ma,
    bbUp: bbMid.map((m, i) => (m == null || sd[i] == null) ? null : m + 2 * sd[i]),
    bbLo: bbMid.map((m, i) => (m == null || sd[i] == null) ? null : m - 2 * sd[i]),
    bbMid, vma: smaSeries(vol, 20), rsi: rsiSeries(close, 14), macd, signal,
    hist: macd.map((m, i) => (m == null || signal[i] == null) ? null : m - signal[i]) };
  return _indCache;
}

function drawIndicators(g){
  const { vis, i0, i1, x, y, bigSvg, W, volTop, volH, subTop, subH, maxVol, bw } = g;
  const I = indicatorsFor(cTk);
  const line = ser => d3.line().defined(d => ser[d.i] != null).x(d => x(d.i)).y(d => y(ser[d.i]));
  const put = (ser, col, w, dash) => {
    if(!ser) return;
    bigSvg.append('path').datum(vis).attr('d', line(ser)).attr('fill', 'none')
      .attr('stroke', col).attr('stroke-width', w).attr('opacity', 0.85)
      .attr('pointer-events', 'none')
      .attr('stroke-dasharray', dash || null);
  };

  if(IND.bb){
    put(I.bbUp, '#8a8f98', 1, '3,3');
    put(I.bbLo, '#8a8f98', 1, '3,3');
    put(I.bbMid, '#8a8f98', 1);
  }
  MA_DEFS.filter(d => IND.ma.includes(d.n)).forEach(d => put(I.ma[d.n], d.c, 1.3));

  if(IND.vma && I.vma){
    const vy = v => volTop + volH - (v / maxVol) * volH;
    bigSvg.append('path').datum(vis)
      .attr('d', d3.line().defined(d => I.vma[d.i] != null).x(d => x(d.i)).y(d => vy(I.vma[d.i])))
      .attr('fill', 'none').attr('stroke', '#ffb703').attr('stroke-width', 1).attr('opacity', 0.8)
      .attr('pointer-events', 'none');
  }

  if(IND.sub === 'none' || !subH) return;
  bigSvg.append('line').attr('x1', CM.l).attr('x2', W - CM.r).attr('y1', subTop).attr('y2', subTop)
    .attr('stroke', 'rgba(255,255,255,0.08)');

  if(IND.sub === 'rsi'){
    const sy = v => subTop + subH - (Math.max(0, Math.min(100, v)) / 100) * subH;
    [30, 50, 70].forEach(t => {
      bigSvg.append('line').attr('x1', CM.l).attr('x2', W - CM.r).attr('y1', sy(t)).attr('y2', sy(t))
        .attr('stroke', t === 50 ? 'rgba(255,255,255,0.07)' : 'rgba(255,255,255,0.13)')
        .attr('stroke-dasharray', t === 50 ? null : '3,3');
      bigSvg.append('text').attr('x', W - CM.r + 7).attr('y', sy(t) + 3.5).attr('font-size', 9)
        .attr('fill', '#6b7380').attr('font-family', "'JetBrains Mono',monospace").text(t);
    });
    bigSvg.append('path').datum(vis)
      .attr('d', d3.line().defined(d => I.rsi[d.i] != null).x(d => x(d.i)).y(d => sy(I.rsi[d.i])))
      .attr('fill', 'none').attr('stroke', '#c77dff').attr('stroke-width', 1.4).attr('pointer-events', 'none');
    const last = [...vis].reverse().find(v => I.rsi[v.i] != null);
    if(last) bigSvg.append('text').attr('x', CM.l + 4).attr('y', subTop + 12).attr('font-size', 10)
      .attr('fill', '#c77dff').attr('font-family', "'JetBrains Mono',monospace")
      .text(`RSI(14) ${I.rsi[last.i].toFixed(1)}`);
  } else if(IND.sub === 'macd'){
    const vals = vis.flatMap(v => [I.macd[v.i], I.signal[v.i], I.hist[v.i]]).filter(v => v != null);
    if(!vals.length) return;
    const m = Math.max(...vals.map(Math.abs)) || 1;
    const sy = v => subTop + subH / 2 - (v / m) * (subH / 2 - 3);
    bigSvg.append('line').attr('x1', CM.l).attr('x2', W - CM.r).attr('y1', sy(0)).attr('y2', sy(0))
      .attr('stroke', 'rgba(255,255,255,0.12)');
    vis.forEach(v => {
      const h = I.hist[v.i];
      if(h == null) return;
      const top = Math.min(sy(0), sy(h));
      bigSvg.append('rect').attr('x', x(v.i) - bw / 2).attr('width', bw)
        .attr('y', top).attr('height', Math.max(0.5, Math.abs(sy(h) - sy(0))))
        .attr('fill', h >= 0 ? '#3ddc84' : '#ff5c72').attr('opacity', 0.45)
        .attr('pointer-events', 'none');
    });
    bigSvg.append('path').datum(vis)
      .attr('d', d3.line().defined(d => I.macd[d.i] != null).x(d => x(d.i)).y(d => sy(I.macd[d.i])))
      .attr('fill', 'none').attr('stroke', '#5fd0ff').attr('stroke-width', 1.4).attr('pointer-events', 'none');
    bigSvg.append('path').datum(vis)
      .attr('d', d3.line().defined(d => I.signal[d.i] != null).x(d => x(d.i)).y(d => sy(I.signal[d.i])))
      .attr('fill', 'none').attr('stroke', '#ffb703').attr('stroke-width', 1.2).attr('pointer-events', 'none');
    bigSvg.append('text').attr('x', CM.l + 4).attr('y', subTop + 12).attr('font-size', 10)
      .attr('fill', '#6b7380').attr('font-family', "'JetBrains Mono',monospace")
      .text('MACD(12,26,9)');
  }
}
