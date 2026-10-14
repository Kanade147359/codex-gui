(() => {
  const el = id => document.getElementById(id);
  let preview = null, selected = null;
  let backlogSelections = [];
  const ownerNotes = new Map();
  async function api(url, body) {
    const r = await fetch(url, body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail?.message || JSON.stringify(data.detail));
    return data;
  }
  function text(tag, value) { const n = document.createElement(tag); n.textContent = value; return n; }
  function showError(e) { el('ap-message').textContent = e.message; }
  let imported = null, nodeEditors = [], documentEditors = [];
  const invalidate = () => {preview = null; el('ap-start').disabled = true;};
  el('ap-import').onclick = async () => {
    try {
      const form = new FormData(); for (const file of el('ap-files').files) form.append('files',file);
      const response = await fetch('/api/autopilot/import',{method:'POST',body:form}); const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || JSON.stringify(data.detail));
      imported = data; nodeEditors = []; documentEditors = []; invalidate();
      el('ap-documents').replaceChildren(); el('ap-import-nodes').replaceChildren();
      for (const [name,body] of Object.entries(data.inputs)) {
        const area = document.createElement('textarea'); area.rows = 5; area.value = body; area.setAttribute('aria-label',name); area.oninput = invalidate;
        el('ap-documents').append(text('h3',`${data.common.includes(name) ? 'COMMON' : '担当指示'}: ${name}`),area);
        documentEditors.push({name,area});
      }
      for (const node of data.nodes) {
        const card = document.createElement('fieldset'); card.append(text('legend',node.id)); const fields = {};
        for (const key of ['id','depends_on','paths','validation','manual_gates','draft','envelope','integration_mode']) {
          const input = document.createElement(key === 'validation' || key === 'envelope' ? 'textarea' : 'input');
          input.value = ['id','integration_mode'].includes(key) ? (node[key] || 'merge-only') : JSON.stringify(node[key] ?? (key === 'envelope' ? {} : []));
          input.setAttribute('aria-label',`${node.id} ${key}`); input.oninput = invalidate;
          const label = text('label',key); label.append(input); card.append(label); fields[key] = input;
        }
        el('ap-import-nodes').append(card); nodeEditors.push({node,fields});
      }
      el('ap-convert').disabled = false; el('ap-message').textContent = '取込済み。本文・依存・編集範囲・検証・承認条件を確認してください。未実行です。';
    } catch(e) {showError(e);}
  };
  el('ap-convert').onclick = async () => {
    try {
      const inputs = Object.fromEntries(documentEditors.map(({name,area}) => [name,area.value]));
      const nodes = nodeEditors.map(({node,fields}) => ({...node,...Object.fromEntries(Object.entries(fields).map(([key,input]) => [key,['id','integration_mode'].includes(key) ? input.value : JSON.parse(input.value)]))}));
      const result = await api('/api/autopilot/import/convert',{...imported,inputs,nodes,repository:el('ap-repository').value,base_ref:el('ap-base').value,permissions:{commit_owned_changes:el('ap-import-commit').checked,publish_main:false},policy:{extra_usage_disabled_confirmed:el('ap-import-billing').checked}});
      el('ap-manifest').value = JSON.stringify(result.manifest,null,2); preview = result.preview;
      el('ap-preview-result').textContent = JSON.stringify(preview,null,2); el('ap-start').disabled = false; el('ap-opt-in').checked = false;
    } catch(e) {showError(e);}
  };
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
        for (const revision of w.plan_revisions || []) {
          const card = text('article',`計画revision: ${revision.state}`); card.append(text('pre',JSON.stringify(revision.payload,null,2)));
          if (revision.state === 'awaiting_owner') for (const action of ['approve','reject']) {
            const button = text('button',`Owner ${action} plan revision`); button.onclick = async () => {try {await api(`/api/autopilot/workflows/${selected}/revisions/${revision.id}/decision`,{owner_action:true,expected_target:revision.decision_target,action}); await refresh();} catch(e) {showError(e);}}; card.append(button);
          }
          el('ap-graph').append(card);
        }
        for (const node of w.nodes) {
          const card = document.createElement('article'); card.className = 'ap-node';
          card.append(text('h3',`${node.name} (${node.spec.kind}) — ${node.state}`),text('p',node.reason),text('p',`依存: ${node.spec.depends_on.join(', ')} / 修正残: ${w.plan.policy.max_fix_rounds-node.fix_rounds}`));
          if (node.state === 'blocked_decision') {
            const note = document.createElement('input'); note.placeholder = '所有者判断の理由'; note.setAttribute('aria-label',`${node.name} owner rationale`);
            const noteKey = `${node.id}:${node.decision_target}`; note.value = ownerNotes.get(noteKey) || ''; note.oninput = () => ownerNotes.set(noteKey,note.value);
            card.append(note,text('p',`判断対象版: ${node.decision_target}`));
            for (const action of ['approve','reject','resume']) {
              const button = text('button',`Owner ${action}`);
              button.onclick = async () => {try {await api(`/api/autopilot/workflows/${selected}/nodes/${node.name}/decision`,{owner_action:true,expected_target:node.decision_target,action,note:note.value}); await refresh();} catch(e) {showError(e);}};
              card.append(button);
            }
          }
          if (node.task_id) {const link = text('a','Task詳細'); link.href = `/tasks/${node.task_id}`; card.append(link);}
          card.append(text('pre',JSON.stringify({pinned_inputs:node.inputs,candidates:node.candidates,operations:node.operations,owner_decisions:node.decisions},null,2)));
          el('ap-graph').append(card);
        }
      }
    } catch(e) {showError(e);}
  }
  el('ap-backlog-load').onclick = async () => {
    try { const data = await api('/api/autopilot/backlog'); el('ap-backlog').replaceChildren();
      backlogSelections = [];
      for (const task of data.tasks) {
        const row = text('div',`${task.name}: ${task.category} `);
        const check = document.createElement('input'); check.type = 'checkbox'; check.disabled = task.category === 'already_registered';
        const paths = document.createElement('input'); paths.value = JSON.stringify(task.paths); paths.setAttribute('aria-label',`編集担当path ${task.task_id}`);
        const validation = document.createElement('textarea'); validation.rows = 3; validation.value = JSON.stringify(task.validation); validation.setAttribute('aria-label',`必須validation ${task.task_id}`);
        row.prepend(check); row.append(text('p',`Contract: ${JSON.stringify(task.contract)}`),paths,validation); el('ap-backlog').append(row);
        backlogSelections.push({task,check,paths,validation});
      }
    } catch(e) {showError(e);}
  };
  el('ap-backlog-prepare').onclick = async () => {
    try {
      const tasks = backlogSelections.filter(s => s.check.checked).map(s => ({task_id:s.task.task_id,paths:JSON.parse(s.paths.value),validation:JSON.parse(s.validation.value)}));
      const result = await api('/api/autopilot/backlog/dry-run',{tasks});
      el('ap-manifest').value = JSON.stringify(result.manifest,null,2); preview = result.preview;
      el('ap-preview-result').textContent = JSON.stringify(preview,null,2); el('ap-start').disabled = false;
      el('ap-opt-in').checked = false; el('ap-message').textContent = '一括登録のprofile・contract・権限を確認してopt-inしてください。';
    } catch(e) {showError(e);}
  };
  refresh(); setInterval(refresh,3000);
})();
