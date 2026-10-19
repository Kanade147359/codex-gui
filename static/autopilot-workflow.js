(() => {
  const ui = window.AutopilotUI, {text, api} = ui;
  const el = id => document.getElementById(id), identity = document.body.dataset.workflowId;
  const endpoint = ui.workflowAPI(identity), draftKey = `autopilot-workflow-draft:${identity}`;
  let workflow = null, refreshing = false, version = '', submitting = false;
  let instructionKey = '', nodeKey = '';
  const uuid = () => window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const showError = error => {el('ap-message').textContent = error.message; el('ap-message').className = 'error';};
  const message = value => {el('ap-message').textContent = value; el('ap-message').className = '';};
  const fields = ['ap-instruction-node', 'ap-instruction-prompt', 'ap-node-name', 'ap-node-kind', 'ap-node-prompt',
    'ap-node-dependencies', 'ap-node-paths', 'ap-node-validation', 'ap-node-artifacts'];
  let draft = {};
  try {draft = JSON.parse(sessionStorage.getItem(draftKey) || '{}');} catch (_) {}
  for (const id of fields) if (draft[id] !== undefined && !el(id).multiple) el(id).value = draft[id];
  instructionKey = draft.instructionKey || ''; nodeKey = draft.nodeKey || '';
  function saveDraft() {
    for (const id of fields) draft[id] = el(id).multiple ? [...el(id).selectedOptions].map(option => option.value) : el(id).value;
    draft.instructionKey = instructionKey; draft.nodeKey = nodeKey;
    try {sessionStorage.setItem(draftKey, JSON.stringify(draft));} catch (_) {}
  }
  for (const id of fields) {
    el(id).addEventListener('input', () => {if (id.startsWith('ap-instruction-')) instructionKey = ''; else nodeKey = ''; saveDraft();});
    el(id).addEventListener('change', saveDraft);
  }
  function updateInstructionLabel() {
    el('ap-instruction-send').textContent = el('ap-instruction-node').value ? '子タスクへ追加指示を送る' : 'メモを保存';
  }
  el('ap-instruction-node').addEventListener('change', updateInstructionLabel);
  function populateTargets() {
    const target = el('ap-instruction-node'), wanted = target.value || draft['ap-instruction-node'] || '';
    const dependencies = el('ap-node-dependencies');
    const selected = dependencies.options.length ? [...dependencies.selectedOptions].map(option => option.value) : (draft['ap-node-dependencies'] || []);
    const note = text('option', '大タスクのメモとして保存（実行しない）'); note.value = ''; target.replaceChildren(note);
    dependencies.replaceChildren();
    for (const node of workflow.nodes) {
      const dependency = text('option', `${node.name} — ${node.state}`); dependency.value = node.name; dependency.selected = selected.includes(node.name); dependencies.append(dependency);
      if (node.task_id && !['superseded', 'awaiting_replacement', 'stopped'].includes(node.state)) {
        const unavailable = node.task?.thread_busy ? '（実行中）' : node.task && !node.task.codex_thread_id ? '（継続可能なsessionなし）' : '';
        const option = text('option', `${node.name} — ${node.state}${unavailable}`); option.value = node.name;
        option.disabled = Boolean(unavailable); target.append(option);
      }
    }
    if ([...target.options].some(option => option.value === wanted)) target.value = wanted;
    updateInstructionLabel();
    const stopped = workflow.state === 'stopped';
    el('ap-instruction-send').disabled = stopped || submitting; el('ap-node-add').disabled = stopped || submitting;
  }
  function renderHistory() {
    const entries = [];
    for (const event of workflow.events || []) {
      const row = document.createElement('article'); row.className = 'ap-history-entry';
      const payload = event.payload || {};
      const kind = event.kind || payload.kind, label = {note: '大タスクのメモ', instruction: '子タスクへの追加指示', add_node: '子タスクの追加'}[kind] || kind || '操作';
      row.append(text('h3', `${label}${event.created_at ? ` · ${new Date(event.created_at).toLocaleString()}` : ''}`));
      const narrative = payload.prompt || payload.note || payload.reason || payload.summary || payload.node?.prompt;
      if (narrative) {const paragraph = text('p', narrative); paragraph.className = 'ap-review-text'; row.append(paragraph);}
      const target = payload.node_id || event.node_id;
      if (target) row.append(text('p', `対象: ${workflow.nodes.find(node => node.id === target)?.name || target}`));
      row.append(ui.details('操作の詳細データ', event, `event:${event.id}`)); entries.push(row);
    }
    ui.replace(el('ap-workflow-history'), entries.length ? entries : [text('p', '追加操作の履歴はまだありません。実装・レビューの履歴は子タスクから確認できます。')]);
  }
  function renderInputs() {
    const settings = Object.fromEntries(workflow.nodes.map(node => [node.name, node.execution_settings]));
    const children = [ui.settingsTable(settings)];
    for (const [name, body] of Object.entries(workflow.plan.inputs || {})) {
      const details = document.createElement('details'); details.dataset.persist = `input:${name}`;
      details.append(text('summary', name), text('pre', body)); children.push(details);
    }
    children.push(ui.details('実行設定・受入条件・権限・固定SHA', workflow.plan, 'plan-raw'));
    ui.replace(el('ap-workflow-inputs'), children);
  }
  async function refresh() {
    if (refreshing) return; refreshing = true;
    try {
      const next = await api(endpoint), nextVersion = JSON.stringify(next);
      workflow = next;
      if (nextVersion !== version) {
        el('ap-workflow-name').textContent = workflow.plan.name; document.title = `${workflow.plan.name} — Autopilot — Codex GUI`;
        el('ap-workflow-state').textContent = workflow.state; el('ap-workflow-state').className = `status ${workflow.state}`;
        el('ap-workflow-progress').replaceChildren(ui.progress(workflow));
        el('ap-workflow-controls').replaceChildren(ui.controls(workflow, refresh, showError));
        const policy = workflow.plan.policy, meta = [];
        for (const [key, value] of [['Repository', workflow.plan.repository], ['Base ref', workflow.plan.base_ref],
          ['作成日時', new Date(workflow.created_at).toLocaleString()], ['基本モデル', policy.implementation_model || 'Codex CLIの設定'],
          ['推論の強さ', (policy.reasoning_effort || 'high').toUpperCase()], ['メンター', policy.mentor_enabled === false ? '無効' : `有効 · 最大 ${policy.mentor_max_rounds ?? 2} 回`]]) {
          meta.push(text('dt', key), text('dd', value));
        }
        el('ap-workflow-meta').replaceChildren(...meta);
        ui.renderWorkflow(el('ap-graph'), workflow, refresh, showError);
        populateTargets(); renderHistory(); renderInputs(); version = nextVersion;
      }
    } catch (error) { showError(error); } finally { refreshing = false; }
  }
  el('ap-instruction-form').onsubmit = async event => {
    event.preventDefault(); if (!workflow || submitting) return;
    const prompt = el('ap-instruction-prompt').value.trim(); if (!prompt) return;
    submitting = true; el('ap-instruction-send').disabled = true; instructionKey ||= uuid(); saveDraft();
    try {
      const target = el('ap-instruction-node').value;
      await api(`${endpoint}/instructions`, {owner_action: true, expected_target: workflow.control_target,
        request_key: instructionKey, node_id: target, prompt});
      el('ap-instruction-prompt').value = ''; instructionKey = ''; saveDraft();
      message(target ? '追加指示を子タスクへ送信しました。' : '大タスクのメモを保存しました。'); await refresh();
    } catch (error) {showError(error); await refresh();}
    finally {submitting = false; if (workflow) populateTargets();}
  };
  const lines = id => el(id).value.split('\n').map(value => value.trim()).filter(Boolean);
  el('ap-node-form').onsubmit = async event => {
    event.preventDefault(); if (!workflow || submitting) return;
    submitting = true; el('ap-node-add').disabled = true;
    try {
      const node = {id: el('ap-node-name').value.trim(), kind: el('ap-node-kind').value,
        prompt: el('ap-node-prompt').value.trim(), depends_on: [...el('ap-node-dependencies').selectedOptions].map(option => option.value),
        paths: lines('ap-node-paths'), validation: {commands: JSON.parse(el('ap-node-validation').value || '[]'), artifacts: lines('ap-node-artifacts')}};
      nodeKey ||= uuid(); saveDraft();
      await api(`${endpoint}/nodes`, {owner_action: true, expected_target: workflow.control_target, request_key: nodeKey, node});
      el('ap-node-form').reset(); nodeKey = ''; saveDraft();
      message(`子タスク ${node.id} を追加しました。`); await refresh();
    } catch (error) {showError(error); await refresh();}
    finally {submitting = false; if (workflow) populateTargets();}
  };
  refresh(); setInterval(refresh, 3000);
})();
