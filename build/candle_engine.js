/* ---------------------------------------------------------------------
   Chart geometry from the last render — drag handlers convert pixels back
   into (index, price) through this, so drawings stay anchored to the data.
   --------------------------------------------------------------------- */
let geom = null, cSel = null, yManual = null, activeDrag = null;

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
  if(modal.hidden) return;
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
function drawCandle(){
  if(!cTk || modal.hidden) return;
  const host = document.querySelector('.modal-body');
  const W = host.clientWidth, H = host.clientHeight;
  if(W < 40 || H < 40) return;
  bigSvg.attr('viewBox', `0 0 ${W} ${H}`);
  bigSvg.selectAll('*').remove();

  const rows = ohlcOf(cTk);
  const i0 = Math.max(0, Math.round(cI0)), i1 = Math.min(rows.length-1, Math.round(cI1));
  const vis = [];
  for(let i=i0;i<=i1;i++) if(rows[i]) vis.push({ i, d: rows[i] });
  if(!vis.length) return;

  const plotW = W - CM.l - CM.r;
  const plotH = H - CM.t - CM.b, volH = plotH*CM.volH, priceH = plotH - volH - CM.gap;
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

  const maxVol = Math.max(1, ...vis.map(v=>v.d[4]));
  const vy = v => CM.t + priceH + CM.gap + volH - (v/maxVol)*volH;
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

  /* crosshair */
  const cross = bigSvg.append('g').style('display','none').style('pointer-events','none');
  const chX = cross.append('line').attr('y1',CM.t).attr('y2',CM.t+priceH+CM.gap+volH)
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
    .on('mousedown', ev => beginDrag('zoomx', ev, { i0:cI0, i1:cI1 }))
    .on('dblclick', ()=>{ setRange(252); document.querySelectorAll('#m-range button')
        .forEach(b=>b.classList.toggle('on', b.dataset.d==='252')); })
    .append('title').text('좌우로 드래그하면 기간 확대/축소 · 더블클릭 = 1년');
  bigSvg.append('rect').attr('x',W-CM.r).attr('y',CM.t).attr('width',CM.r).attr('height',priceH)
    .attr('fill','transparent').style('cursor','ns-resize')
    .on('mousedown', ev => beginDrag('zoomy', ev, { lo, hi }))
    .on('dblclick', ()=>{ yManual = null; drawCandle(); })
    .append('title').text('위아래로 드래그하면 가격축 확대/축소 · 더블클릭 = 자동 맞춤');

  positionSelBar();

  const last = vis[vis.length-1].d, prev = vis.length > 1 ? vis[vis.length-2].d : null;
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

window.addEventListener('resize', ()=>{ if(!modal.hidden) drawCandle(); });
