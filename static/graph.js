/* Plain SVG/HTML, same polling transport as the task list. No external graph or image dependency. */
"use strict";
function dependencyLayout(tasks, edges, vertical = false) {
  const ids = new Set(tasks.map(t => t.id)), parents = new Map(), children = new Map();
  for (const id of ids) { parents.set(id, []); children.set(id, []); }
  for (const e of edges) if (ids.has(e.parent) && ids.has(e.child) && e.parent !== e.child) {
    if (!parents.get(e.child).includes(e.parent)) { parents.get(e.child).push(e.parent); children.get(e.parent).push(e.child); }
  }
  const degrees = new Map([...ids].map(id => [id, parents.get(id).length]));
  const levels = new Map([...ids].map(id => [id, 0]));
  const queue = [...ids].filter(id => !degrees.get(id)).sort();
  for (let i = 0; i < queue.length; i++) for (const child of children.get(queue[i])) {
    levels.set(child, Math.max(levels.get(child), levels.get(queue[i]) + 1));
    degrees.set(child, degrees.get(child) - 1); if (!degrees.get(child)) queue.push(child);
  }
  const columns = [];
  for (const id of ids) (columns[levels.get(id)] ||= []).push(id);
  const order = new Map(); columns.forEach(col => col.sort().forEach((id, i) => order.set(id, i)));
  // Barycentre sweeps keep forks/joins together and reduce crossings, with stable ID tie breaks.
  for (let pass = 0; pass < 4; pass++) {
    const indexes = columns.map((_, i) => i); if (pass % 2) indexes.reverse();
    for (const i of indexes) {
      const links = pass % 2 ? children : parents;
      const score = id => { const n = links.get(id); return n.length ? n.reduce((s, p) => s + order.get(p), 0) / n.length : order.get(id); };
      columns[i].sort((a, b) => score(a) - score(b) || a.localeCompare(b));
      columns[i].forEach((id, n) => order.set(id, n));
    }
  }
  const positions = new Map();
  columns.forEach((col, stage) => col.forEach((id, row) => positions.set(id,
    vertical ? { x: 60 + row * 340, y: 60 + stage * 240, stage } : { x: 60 + stage * 470, y: 60 + row * 160, stage })));
  return positions;
}
// Task ids reachable from `id` through prerequisites and successors; depth 0 means unlimited.
function focusSubgraph(edges, id, depth = 0) {
  const shown = new Set([id]);
  for (const [from, to] of [['child', 'parent'], ['parent', 'child']]) {
    let frontier = [id];
    for (let hop = 1; frontier.length && (!depth || hop <= depth); hop++) {
      const next = [];
      for (const e of edges) if (frontier.includes(e[from]) && !shown.has(e[to])) { shown.add(e[to]); next.push(e[to]); }
      frontier = next;
    }
  }
  return shown;
}
if (typeof module !== 'undefined') module.exports = { dependencyLayout, focusSubgraph };
if (typeof document !== 'undefined' && document.body.dataset.page === 'graph') {
  const q = s => document.querySelector(s);
  const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const request = async (method, url, body) => {
    const r = await fetch(url, {method, headers: body ? {'Content-Type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
    const data = await r.json(); if (!r.ok) throw new Error(data.detail?.message || data.detail || '処理に失敗しました'); return data;
  };
  const states = {queued:['…','実行待ち'], starting:['◷','開始中'], running:['▶','実行中'],
    waiting_dependencies:['⌛','依存待ち'], retry_wait:['↻','再開待ち'], completed:['✓','完了'], failed:['!','失敗'],
    stopped:['■','停止'], blocked:['⊘','保留'], interrupted:['Ⅱ','一時停止'], 'waiting-for-quota':['Ⅱ','上限待ち']};
  const scheduleStates = {waiting_dependencies:'依存待ち',waiting_thread:'thread待ち',ready:'送信待ち',running:'実行中',completed:'完了',blocked:'保留',failed:'失敗'};
  const relation = e => e.settings.map(s=>s.kind==='task'?'タスク開始':`${s.label} · ${scheduleStates[s.status] || s.status}`).join(' / ');
  let data = {tasks:[],edges:[],errors:[]}, edges = [], positions = new Map(), signature = '', vertical = false;
  let focusId = null, selected = null, scale = 1, panX = 20, panY = 20, first = true, polling = false;
  const world = q('#graph-world'), viewport = q('#graph-viewport');
  const message = text => { q('#graph-message').textContent = text; };
  const transform = () => world.setAttribute('transform', `translate(${panX} ${panY}) scale(${scale})`);
  function fit() {
    if (!positions.size) return;
    const bounds=world.getBBox(), width=bounds.width+80, height=bounds.height+80;
    scale = Math.max(.02, Math.min(1.2, viewport.clientWidth / width, viewport.clientHeight / height));
    panX = (viewport.clientWidth - width * scale) / 2 + (40-bounds.x) * scale;
    panY = (viewport.clientHeight - height * scale) / 2 + (40-bounds.y) * scale;
    transform();
  }
  function visible() {
    if (focusId && !data.tasks.some(t => t.id === focusId)) focusId = null;
    if (focusId) {
      const shown = focusSubgraph(edges, focusId, Number(q('#graph-focus-depth').value) || 0);
      return {direct: shown, tasks: data.tasks.filter(t=>shown.has(t.id)), edges: edges.filter(e=>shown.has(e.parent) && shown.has(e.child))};
    }
    const search = q('#graph-search').value.toLowerCase(), project = q('#graph-project').value;
    const direct = new Set(data.tasks.filter(t => (!project || t.repository === project) && `${t.id} ${t.name}`.toLowerCase().includes(search)).map(t=>t.id));
    const shown = new Set(direct);
    // Include all transitive prerequisites as explicit contextual cards when filtering.
    let changed = true;
    while (changed) { changed = false; for (const e of edges) if (shown.has(e.child) && !shown.has(e.parent)) { shown.add(e.parent); changed = true; } }
    return {direct, tasks:data.tasks.filter(t=>shown.has(t.id)), edges:edges.filter(e=>shown.has(e.parent) && shown.has(e.child))};
  }
  function render(force = false) {
    edges = data.edges;
    const v = visible();
    const next = JSON.stringify([vertical, v.tasks.map(t=>t.id).sort(), v.edges.map(e=>[e.parent,e.child,e.key]).sort()]);
    if (force || next !== signature) { positions = dependencyLayout(v.tasks, v.edges, vertical); signature = next; }
    const related = new Set(selected?.type === 'node' ? [selected.id] : []), highlighted = new Set();
    if (selected?.type === 'node') {
      for (const e of v.edges) if (e.parent === selected.id || e.child === selected.id) { related.add(e.parent); related.add(e.child); highlighted.add(e.key); }
    } else if (selected?.type === 'edge') {
      const e = edges.find(e=>e.key===selected.id); if (e) { related.add(e.parent); related.add(e.child); highlighted.add(e.key); }
    }
    const paths = v.edges.map((e,i) => {
      const a = positions.get(e.parent), b = positions.get(e.child); if (!a || !b) return '';
      let d, lx, ly;
      if (vertical) {
        const x1=a.x+140, y1=a.y+104, x2=b.x+140, y2=b.y, middle=(y1+y2)/2;
        d=`M${x1},${y1} C${x1},${middle} ${x2},${middle} ${x2},${y2}`; lx=(x1+x2)/2; ly=middle-8;
      } else {
        const x1=a.x+280,y1=a.y+52,x2=b.x,y2=b.y+52, middle=(x1+x2)/2;
        if (b.stage - a.stage > 1 || b.stage <= a.stage) {
          // Route long and cyclic projections above cards. These are saved edges, never execution changes.
          const lane=25-(i%12)*15, offset=45+(i%3)*20;
          d=`M${x1},${y1} C${x1+offset},${y1} ${x1+offset},${lane} ${x1+offset+15},${lane} L${x2-offset-15},${lane} C${x2-offset},${lane} ${x2-offset},${y2} ${x2},${y2}`;
          lx=middle; ly=lane-7;
        } else {
          d=`M${x1},${y1} C${middle},${y1} ${middle},${y2} ${x2},${y2}`; lx=middle; ly=(y1+y2)/2-9+(i%2)*20;
        }
      }
      const fullLabel = relation(e), label = e.settings.length > 1 ? `手動設定 ${e.settings.length}件` : fullLabel.length > 19 ? fullLabel.slice(0,17)+'…' : fullLabel;
      return `<g class="graph-edge ${highlighted.has(e.key)?'selected':''}" data-edge="${escape(e.key)}" tabindex="0" role="button" aria-label="${escape(`${e.parent} → ${e.child} ${label}`)}"><title>${escape(fullLabel)}</title><path class="edge-hit" d="${d}"/><path class="edge-line" d="${d}" marker-end="url(#graph-arrow)"/><text x="${lx}" y="${ly}" text-anchor="middle">${escape(label)}</text></g>`;
    }).join('');
    const cards = v.tasks.map(t=> {
      const p=positions.get(t.id), state=states[t.status] || ['?',t.status], ghost=!v.direct.has(t.id);
      return `<g class="graph-node ${related.has(t.id)?'selected':''} ${ghost?'contextual':''}" transform="translate(${p.x},${p.y})" data-node="${escape(t.id)}" tabindex="0" role="button" aria-label="${escape(t.id+' '+t.name+' '+state[1])}"><title>${escape(t.name+'\n'+t.repository)}</title><foreignObject width="280" height="104"><div xmlns="http://www.w3.org/1999/xhtml" class="graph-card"><span class="graph-icon">${state[0]}</span><div class="graph-card-title"><small>${escape(t.id)}</small><strong title="${escape(t.name)}">${escape(t.name)}</strong><small title="${escape(t.repository)}">${ghost?'絞込み外の前提 · ':''}${escape(t.repository.split('/').pop())}</small></div><span class="graph-badge">${escape(state[1])}</span></div></foreignObject></g>`;
    }).join('');
    const focused = focusId && data.tasks.find(t => t.id === focusId);
    q('#graph-focus').hidden = !focused; q('#graph-focus-label').textContent = focused ? `1タスク表示: ${focused.id} ${focused.name}` : '';
    world.innerHTML = paths + cards; transform(); q('#graph-empty').hidden = Boolean(v.tasks.length);
    q('#graph-count').textContent = `${v.tasks.length}/${data.tasks.length} タスク · ${v.edges.length} 本の依存線`;
    message(data.errors.map(e=>e.message).join(' / '));
    detail();
  }
  function detail() {
    const panel=q('#graph-detail');
    if (!selected) return;
    const taskName=id=>data.tasks.find(t=>t.id===id)?.name || id;
    const scheduleLink=(id,sid)=>`/tasks/${encodeURIComponent(id)}#${sid?'scheduled-'+sid:'schedule-box'}`;
    if (selected.type === 'node') {
      const t=data.tasks.find(t=>t.id===selected.id);
      if (!t) { panel.textContent='このタスクは現在の設定に存在しません。'; return; }
      const prerequisites=edges.filter(e=>e.child===t.id), successors=edges.filter(e=>e.parent===t.id);
      const list=es=>es.map(e=>`<li><button data-select-edge="${escape(e.key)}">${escape(taskName(e.parent===t.id?e.child:e.parent))} · ${escape(relation(e))}</button></li>`).join('') || '<li>なし</li>';
      panel.innerHTML=`<h2>${escape(t.id)} · ${escape(states[t.status]?.[1] || t.status)}</h2><h3>${escape(t.name)}</h3><p class="graph-repo">${escape(t.repository)}</p>
        <p><button class="graph-focus-btn" data-focus="${focusId===t.id?'':escape(t.id)}">${focusId===t.id?'全体に戻す':'このタスクだけ表示'}</button></p>
        <p><a href="/tasks/${encodeURIComponent(t.id)}">タスク詳細へ →</a></p><p><a href="${scheduleLink(t.id)}">既存スケジュールを開く →</a></p>
        ${t.status_detail?`<p>${escape(t.status_detail)}</p>`:''}
        <h2>設定された前提</h2><ul>${list(prerequisites)}</ul><h2>後続</h2><ul>${list(successors)}</ul>
        <p class="muted">予約指示の矢印は追加ターンの条件です。初回タスクの開始条件とは別に表示します。実行の可否は既存スケジューラが判定します。</p>`;
    } else {
      const e=edges.find(e=>e.key===selected.id);
      if (!e) { panel.textContent='この依存線は現在の設定に存在しません。'; return; }
      panel.innerHTML=`<h2>手動設定された依存関係</h2><p>${escape(e.parent)} → ${escape(e.child)}</p>
        <p>${escape(taskName(e.parent))} → ${escape(taskName(e.child))}</p>
        ${e.settings.map(setting=>`<section><h3>${escape(setting.label)}</h3>
          ${setting.status?`<p>予約状態: ${escape(scheduleStates[setting.status] || setting.status)}</p>`:''}
          ${setting.prompt?`<pre class="graph-prompt">${escape(setting.prompt)}</pre>`:''}
          ${setting.blocked_reason?`<p>${escape(setting.blocked_reason)}</p>`:''}
          <a href="${setting.kind==='task'?'/tasks/'+encodeURIComponent(e.child)+'#deps-section':scheduleLink(e.child,setting.id)}">該当スケジュールを開く →</a></section>`).join('')}
        <p class="graph-repo">前提: ${escape(data.tasks.find(t=>t.id===e.parent).repository)}<br>後続: ${escape(data.tasks.find(t=>t.id===e.child).repository)}</p>`;
    }
  }
  async function refresh() {
    if (polling) return; polling=true;
    try {
      data=await request('GET','/api/dependency-graph');
      const projects=[...new Set(data.tasks.map(t=>t.repository))].sort(), select=q('#graph-project'), current=select.value;
      const html='<option value="">全プロジェクト / リポジトリ</option>'+projects.map(p=>`<option value="${escape(p)}">${escape(p)}</option>`).join('');
      if (select.innerHTML!==html) { select.innerHTML=html; select.value=current; }
      render(); if(first && positions.size) {fit();first=false;}
    } catch(e) {message(e.message);} finally {polling=false;}
  }
  world.addEventListener('click', e=> {
    const node=e.target.closest('[data-node]'), edge=e.target.closest('[data-edge]');
    if(node && e.detail >= 2) { setFocus(node.dataset.node); return; } // double click: show only this task's lineage
    if(node) selected={type:'node',id:node.dataset.node}; else if(edge) selected={type:'edge',id:edge.dataset.edge};
    render();
  });
  world.addEventListener('keydown', e=>{if(e.key==='Enter'||e.key===' ') {e.preventDefault();e.target.dispatchEvent(new MouseEvent('click',{bubbles:true}));}});
  q('#graph-detail').addEventListener('click', e=> {
    const link=e.target.closest('[data-select-edge]'); if(link) {selected={type:'edge',id:link.dataset.selectEdge};render();return;}
    const focus=e.target.closest('[data-focus]'); if(focus) setFocus(focus.dataset.focus||null);
  });
  function setFocus(id) { focusId=id; if(id) selected={type:'node',id}; render(true); fit(); }
  q('#graph-focus-clear').onclick=()=>setFocus(null); q('#graph-focus-depth').onchange=()=>{if(focusId){render(true);fit();}};
  q('#graph-search').oninput=q('#graph-project').onchange=()=>{focusId=null;render();fit();refresh();};
  q('#graph-direction').onclick=()=>{vertical=!vertical;q('#graph-direction').textContent=vertical?'横配置':'縦配置';render(true);fit();};
  q('#graph-layout').onclick=()=>{render(true);fit();}; q('#graph-fit').onclick=fit;
  function zoom(factor,x=viewport.clientWidth/2,y=viewport.clientHeight/2) {const next=Math.max(.02,Math.min(3,scale*factor)),f=next/scale;panX=x-(x-panX)*f;panY=y-(y-panY)*f;scale=next;transform();}
  q('#graph-zoom-in').onclick=()=>zoom(1.2);q('#graph-zoom-out').onclick=()=>zoom(1/1.2);
  viewport.addEventListener('wheel',e=>{e.preventDefault();const r=viewport.getBoundingClientRect();zoom(Math.exp(-e.deltaY*.001),e.clientX-r.left,e.clientY-r.top);},{passive:false});
  let drag=null;
  viewport.addEventListener('pointerdown',e=>{if(e.target.closest('[data-node],[data-edge]')||e.button!==0)return;drag={x:e.clientX,y:e.clientY,px:panX,py:panY};viewport.setPointerCapture(e.pointerId);});
  viewport.addEventListener('pointermove',e=>{if(drag){panX=drag.px+e.clientX-drag.x;panY=drag.py+e.clientY-drag.y;transform();}});
  viewport.addEventListener('pointerup',()=>drag=null);viewport.addEventListener('pointercancel',()=>drag=null);
  viewport.addEventListener('keydown',e=>{const moves={ArrowLeft:[40,0],ArrowRight:[-40,0],ArrowUp:[0,40],ArrowDown:[0,-40]};if(e.target!==viewport)return;if(moves[e.key]){e.preventDefault();panX+=moves[e.key][0];panY+=moves[e.key][1];transform();}if(e.key==='+'||e.key==='=')zoom(1.2);if(e.key==='-')zoom(1/1.2);if(e.key==='0')fit();});
  // Resizable graph zone: height (bottom grip) and detail-panel width (side grip), remembered per browser.
  const root=document.documentElement, store={
    get(k){try{return Number(localStorage.getItem('graph-'+k))||null;}catch(e){return null;}},
    set(k,v){try{v===null?localStorage.removeItem('graph-'+k):localStorage.setItem('graph-'+k,v);}catch(e){}}};
  const sizes={
    h:{min:240,max:()=>Math.max(400,innerHeight*3),prop:'--graph-h',get:()=>viewport.getBoundingClientRect().height},
    w:{min:220,max:()=>Math.max(300,innerWidth-360),prop:'--graph-detail-w',get:()=>q('#graph-detail').getBoundingClientRect().width}};
  const setSize=(k,px,save=true)=>{const s=sizes[k],v=px===null?null:Math.round(Math.max(s.min,Math.min(s.max(),px)));
    v===null?root.style.removeProperty(s.prop):root.style.setProperty(s.prop,v+'px'); if(save)store.set(k,v);};
  for(const k of ['h','w']) if(store.get(k)) setSize(k,store.get(k),false);
  function grip(el,k,sign,axis,keys){
    let start=null;
    el.addEventListener('pointerdown',e=>{if(e.button!==0)return;start={p:e[axis],v:sizes[k].get()};el.setPointerCapture(e.pointerId);el.classList.add('dragging');e.preventDefault();});
    el.addEventListener('pointermove',e=>{if(start)setSize(k,start.v+sign*(e[axis]-start.p));});
    const end=()=>{start=null;el.classList.remove('dragging');}; el.addEventListener('pointerup',end); el.addEventListener('pointercancel',end);
    el.addEventListener('dblclick',()=>setSize(k,null));
    el.addEventListener('keydown',e=>{const d=keys[e.key];if(d){e.preventDefault();setSize(k,sizes[k].get()+sign*d*(e.shiftKey?200:40));}});
  }
  grip(q('#graph-split-y'),'h',1,'clientY',{ArrowUp:-1,ArrowDown:1});
  grip(q('#graph-split-x'),'w',-1,'clientX',{ArrowLeft:-1,ArrowRight:1});
  refresh(); const timer=setInterval(refresh,2000);window.addEventListener('pagehide',()=>clearInterval(timer));
}
