(() => {
  const el = id => document.getElementById(id);
  let preview = null, selected = null;
  async function api(url, body) {
    const r = await fetch(url, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail?.message || JSON.stringify(data.detail));
    return data;
  }
  function text(tag, value) { const n = document.createElement(tag); n.textContent = value; return n; }
  function showError(e) { el('ap-message').textContent = e.message; }
  el('ap-manifest').addEventListener('input', () => {preview = null; el('ap-start').disabled = true;});
  el('ap-preview').onclick = async () => {
    try {preview = await api('/api/autopilot/dry-run',{manifest:el('ap-manifest').value}); el('ap-preview-result').textContent = JSON.stringify(preview,null,2); el('ap-start').disabled = false;} catch(e) {showError(e);}
  };
  el('ap-start').onclick = async () => {
    try { const w = await api('/api/autopilot/workflows',{manifest:el('ap-manifest').value,expected_digest:preview.input_digest,opt_in:el('ap-opt-in').checked,start:true}); selected = w.id; el('ap-start').disabled = true; await refresh(); } catch(e) {showError(e);}
  };
  async function refresh() {
    try {
      const data = await api('/api/autopilot/workflows');
      el('ap-workflows').replaceChildren();
      for (const w of data.workflows) {
        const row = text('div',`${w.name}: ${w.state} ${w.reason}`);
        const view = text('button','Graph / 履歴'); view.onclick = () => {selected = w.id; refresh();}; row.append(view);
        for (const action of ['pause','resume','stop','reconcile']) {
          const button = text('button',action); button.disabled = w.state === 'stopped' && action !== 'reconcile';
          button.onclick = async () => {try {await api(`/api/autopilot/workflows/${w.id}/${action}`,{}); await refresh();} catch(e) {showError(e);}}; row.append(button);
        }
        el('ap-workflows').append(row);
      }
      if (selected) {
        const w = await api(`/api/autopilot/workflows/${selected}`); el('ap-graph').replaceChildren();
        for (const node of w.nodes) {
          const card = document.createElement('article'); card.className = 'ap-node';
          card.append(text('h3',`${node.name} (${node.spec.kind}) — ${node.state}`),text('p',node.reason),text('p',`依存: ${node.spec.depends_on.join(', ')} / 修正残: ${w.plan.policy.max_fix_rounds-node.fix_rounds}`));
          if (node.task_id) {const link = text('a','Task詳細'); link.href = `/tasks/${node.task_id}`; card.append(link);}
          card.append(text('pre',JSON.stringify({pinned_inputs:node.inputs,candidates:node.candidates,operations:node.operations},null,2)));
          el('ap-graph').append(card);
        }
      }
    } catch(e) {showError(e);}
  }
  el('ap-backlog-load').onclick = async () => {
    try { const data = await api('/api/autopilot/backlog'); el('ap-backlog').replaceChildren();
      for (const task of data.tasks) {const row = text('label',`${task.name}: ${task.category} `); const check = document.createElement('input'); check.type = 'checkbox'; check.disabled = task.category === 'already_registered'; check.onchange = () => {el('ap-message').textContent = `Manifest task_id: ${task.task_id}; contract: ${JSON.stringify(task.contract)}`;}; row.prepend(check); el('ap-backlog').append(row);}
    } catch(e) {showError(e);}
  };
  refresh(); setInterval(refresh,3000);
})();
