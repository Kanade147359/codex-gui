(() => {
  const ui = window.AutopilotUI, {api, text} = ui;
  const el = id => document.getElementById(id);
  let preview = null, imported = null, nodeEditors = [], documentEditors = [], backlogSelections = [];
  let generatedManifest = false;
  let selected = new URLSearchParams(location.search).get('workflow'), refreshing = false, selectedVersion = '';
  let options = {models: [], repos: []};
  const showError = error => { el('ap-message').textContent = error.message; el('ap-message').className = 'error'; };
  const message = value => { el('ap-message').textContent = value; el('ap-message').className = ''; };
  const invalidate = () => { preview = null; el('ap-start').disabled = true; el('ap-opt-in').checked = false; };
  function executionDefaults() {
    return {implementation_model: el('ap-model').value === '__custom__' ? el('ap-model-custom').value.trim() : el('ap-model').value,
      reasoning_effort: el('ap-effort').value, mentor_enabled: el('ap-mentor-enabled').checked,
      mentor_max_rounds: Number(el('ap-mentor-rounds').value)};
  }
  function showPreview(result) {
    preview = result; el('ap-preview-result').textContent = JSON.stringify(result, null, 2);
    const nodes = result.plan.nodes;
    const settings = result.execution_settings || Object.fromEntries(nodes.map(node => [node.id,
      {model: node.model || result.plan.policy.implementation_model, reasoning_effort: node.reasoning_effort || result.plan.policy.reasoning_effort || 'high'}]));
    el('ap-preview-summary').replaceChildren(text('h3', `${result.plan.name} · ${nodes.length} 子タスク`),
      text('p', `メンター: ${result.plan.policy.mentor_enabled === false ? '無効' : '有効'} · 最大 ${result.plan.policy.mentor_max_rounds ?? 2} 回`), ui.settingsTable(settings));
    for (const warning of result.warnings || []) el('ap-preview-summary').append(text('p', warning));
    el('ap-start').disabled = false; el('ap-opt-in').checked = false;
  }
  function changeExecutionDefaults() {
    invalidate();
    if (generatedManifest) {
      const manifest = JSON.parse(el('ap-manifest').value);
      manifest.policy = {...manifest.policy, ...executionDefaults()};
      el('ap-manifest').value = JSON.stringify(manifest, null, 2);
    }
  }
  for (const control of el('ap-execution-settings').querySelectorAll('input, select')) control.addEventListener('change', () => {
    changeExecutionDefaults(); el('ap-model-custom-label').hidden = el('ap-model').value !== '__custom__';
    el('ap-mentor-rounds').disabled = !el('ap-mentor-enabled').checked;
  });
  el('ap-model-custom').addEventListener('input', changeExecutionDefaults);
  el('ap-import').onclick = async () => {
    try {
      const form = new FormData(); for (const file of el('ap-files').files) form.append('files', file);
      const response = await fetch('/api/autopilot/import', {method: 'POST', body: form}); const data = await response.json();
      if (!response.ok) throw new Error(data.detail?.message || JSON.stringify(data.detail));
      imported = data; nodeEditors = []; documentEditors = []; invalidate();
      el('ap-documents').replaceChildren(); el('ap-import-nodes').replaceChildren();
      for (const [name, body] of Object.entries(data.inputs)) {
        const area = document.createElement('textarea'); area.rows = 5; area.value = body; area.setAttribute('aria-label', name); area.oninput = invalidate;
        const box = document.createElement('details'); box.append(text('summary', `${data.common.includes(name) ? 'COMMON' : '担当指示'}: ${name}`), area);
        el('ap-documents').append(box); documentEditors.push({name, area});
      }
      for (const node of data.nodes) {
        const card = document.createElement('fieldset'); card.append(text('legend', node.id)); const fields = {};
        for (const key of ['id', 'depends_on', 'paths', 'validation', 'manual_gates', 'draft', 'envelope', 'integration_mode', 'goal_ids', 'acceptance_ids']) {
          const input = document.createElement(key === 'validation' || key === 'envelope' ? 'textarea' : 'input');
          input.value = ['id', 'integration_mode'].includes(key) ? (node[key] || 'merge-only') : JSON.stringify(node[key] ?? (key === 'envelope' ? {} : []));
          input.setAttribute('aria-label', `${node.id} ${key}`); input.oninput = invalidate;
          const label = text('label', key); label.append(input); card.append(label); fields[key] = input;
        }
        el('ap-import-nodes').append(card); nodeEditors.push({node, fields});
      }
      el('ap-convert').disabled = false; message('取込済み。本文・依存・編集範囲・検証・承認条件を確認してください。未実行です。');
    } catch (error) { showError(error); }
  };
  for (const id of ['ap-import-replanning', 'ap-import-commit', 'ap-import-billing', 'ap-base', 'ap-name']) el(id).addEventListener('change', invalidate);
  el('ap-convert').onclick = async () => {
    try {
      const inputs = Object.fromEntries(documentEditors.map(({name, area}) => [name, area.value]));
      const nodes = nodeEditors.map(({node, fields}) => ({...node, ...Object.fromEntries(Object.entries(fields).map(([key, input]) =>
        [key, ['id', 'integration_mode'].includes(key) ? input.value : JSON.parse(input.value)]))}));
      const result = await api('/api/autopilot/import/convert', {...imported, inputs, nodes,
        name: el('ap-name').value.trim() || 'Markdown workflow', repository: el('ap-repository').value, base_ref: el('ap-base').value,
        permissions: {commit_owned_changes: el('ap-import-commit').checked, publish_main: false},
        policy: {...executionDefaults(), extra_usage_disabled_confirmed: el('ap-import-billing').checked, replanning: el('ap-import-replanning').value}});
      el('ap-manifest').value = JSON.stringify(result.manifest, null, 2); generatedManifest = true; showPreview(result.preview);
    } catch (error) { showError(error); }
  };
  el('ap-manifest').addEventListener('input', () => {generatedManifest = false; invalidate();});
  el('ap-preview').onclick = async () => {
    try { showPreview(await api('/api/autopilot/dry-run', {manifest: el('ap-manifest').value, execution_defaults: executionDefaults()})); }
    catch (error) { invalidate(); showError(error); }
  };
  el('ap-start').onclick = async () => {
    if (!preview) return;
    const button = el('ap-start'); button.disabled = true;
    try {
      const workflow = await api('/api/autopilot/workflows', {manifest: el('ap-manifest').value, execution_defaults: executionDefaults(),
        expected_digest: preview.input_digest, opt_in: el('ap-opt-in').checked, start: true});
      selectWorkflow(workflow.id); message('開始しました。大タスク詳細ページから進捗と追加指示を管理できます。'); await refresh();
    } catch (error) { button.disabled = false; showError(error); }
  };
  function selectWorkflow(id) {
    selected = id; selectedVersion = '';
    const url = new URL(location.href); url.searchParams.set('workflow', id); history.replaceState(null, '', url);
  }
  async function refresh() {
    if (refreshing) return; refreshing = true;
    try {
      const data = await api('/api/autopilot/workflows'); const rows = [];
      for (const workflow of data.workflows) {
        const row = document.createElement('article'); row.className = 'ap-workflow-row';
        const heading = text('h3'); heading.append(ui.link(workflow.name, ui.workflowURL(workflow.id)));
        row.append(heading, text('p', `${workflow.state} · ${workflow.repository || ''}`), ui.progress(workflow));
        const narrative = text('p', workflow.review_summary?.text || workflow.reason || '進捗を確認できます。'); narrative.className = 'ap-review-text'; row.append(narrative);
        const view = text('button', 'Graph / 履歴'); view.onclick = () => {selectWorkflow(workflow.id); refresh();}; row.append(view);
        row.append(ui.controls(workflow, refresh, showError)); rows.push(row);
      }
      ui.replace(el('ap-workflows'), rows.length ? rows : [text('p', '登録された大タスクはありません。')]);
      el('ap-selected-section').hidden = !selected;
      if (selected) {
        const workflow = await api(ui.workflowAPI(selected)); const version = JSON.stringify(workflow);
        if (version !== selectedVersion) {ui.renderWorkflow(el('ap-graph'), workflow, refresh, showError, {compact: true}); selectedVersion = version;}
      }
    } catch (error) { showError(error); } finally { refreshing = false; }
  }
  el('ap-backlog-load').onclick = async () => {
    try {
      const data = await api('/api/autopilot/backlog'); el('ap-backlog').replaceChildren(); backlogSelections = [];
      el('ap-backlog').append(text('p', data.tasks.length ? `既存タスク ${data.tasks.length} 件が確認待ちです。登録先と検証条件を選んで一括整理できます。` : '既存タスクの確認待ちはありません。'));
      for (const task of data.tasks) {
        const row = document.createElement('fieldset'); row.append(text('legend', `${task.name}: ${task.category}`));
        const check = document.createElement('input'); check.type = 'checkbox'; check.disabled = task.category === 'already_registered';
        const label = text('label', 'このタスクを選択'); label.className = 'ap-check'; label.prepend(check); row.append(label);
        const paths = document.createElement('input'); paths.value = JSON.stringify(task.paths); paths.setAttribute('aria-label', `編集担当path ${task.task_id}`);
        const validation = document.createElement('textarea'); validation.rows = 3; validation.value = JSON.stringify(task.validation); validation.setAttribute('aria-label', `必須validation ${task.task_id}`);
        row.append(ui.details('既存の完了条件', task.contract, `contract:${task.task_id}`), paths, validation); el('ap-backlog').append(row);
        backlogSelections.push({task, check, paths, validation});
      }
    } catch (error) { showError(error); }
  };
  el('ap-backlog-prepare').onclick = async () => {
    try {
      const tasks = backlogSelections.filter(value => value.check.checked).map(value => ({task_id: value.task.task_id, paths: JSON.parse(value.paths.value), validation: JSON.parse(value.validation.value)}));
      const result = await api('/api/autopilot/backlog/dry-run', {tasks, policy: executionDefaults()});
      el('ap-manifest').value = JSON.stringify(result.manifest, null, 2); generatedManifest = true; showPreview(result.preview);
      message('一括登録のprofile・contract・権限を確認してopt-inしてください。');
    } catch (error) { showError(error); }
  };
  // Same catalog, recent-repository, folder-browser and base-ref APIs as New Task.
  async function loadOptions() {
    try {
      options = await api('/api/options');
      const model = el('ap-model'), previous = model.value;
      const defaultOption = text('option', `Codex CLIの設定を使用${options.default_model ? ` (${options.default_model})` : ''}`); defaultOption.value = '';
      model.replaceChildren(defaultOption);
      for (const value of options.models || []) {const option = text('option', `${value.name} — ${value.slug}`); option.value = value.slug; model.append(option);}
      const custom = text('option', 'モデルIDを入力…'); custom.value = '__custom__'; model.append(custom); model.value = previous;
      for (const repo of options.repos || []) {
        const option = text('option'); option.value = repo; el('ap-repo-candidates').append(option);
      }
      for (const repo of (options.repos || []).slice(0, 6)) {
        const button = text('button', repo.split('/').filter(Boolean).pop() || repo); button.type = 'button'; button.className = 'chip'; button.title = repo;
        button.onclick = () => chooseRepository(repo); el('ap-recent-repos').append(button);
      }
      if (options.error) el('ap-catalog-message').textContent = `${options.error}。モデルIDを直接入力できます。`;
    } catch (error) { el('ap-catalog-message').textContent = `モデル候補を取得できませんでした。モデルIDを直接入力できます。${error.message}`; }
  }
  async function loadRefs() {
    const repository = el('ap-repository').value.trim(); el('ap-ref-candidates').replaceChildren();
    if (!repository) return;
    try {
      const refs = await api(`/api/refs?repository=${encodeURIComponent(repository)}`);
      if (repository !== el('ap-repository').value.trim()) return;
      const candidates = [...(refs.branches || []), ...(refs.remotes || []), ...(refs.tags || []), ...(refs.worktrees || [])];
      for (const value of candidates) {const option = text('option'); option.value = typeof value === 'string' ? value : value.ref || value.name || value.branch || ''; if (option.value) el('ap-ref-candidates').append(option);}
      el('ap-repo-message').textContent = `Git Repository: ${refs.repository}`;
    } catch (error) { el('ap-repo-message').textContent = error.message; }
  }
  function chooseRepository(repository) {el('ap-repository').value = repository; invalidate(); loadRefs();}
  el('ap-repository').addEventListener('input', () => {invalidate(); clearTimeout(loadRefs.timer); loadRefs.timer = setTimeout(loadRefs, 350);});
  el('ap-repository').addEventListener('change', loadRefs);
  let pickerPath = '', pickerIsGit = false;
  async function browse(path) {
    el('ap-picker-select').disabled = true; pickerIsGit = false;
    try {
      const folder = await api(`/api/fs?path=${encodeURIComponent(path)}&hidden=${el('ap-picker-hidden').checked}`);
      pickerPath = folder.path; pickerIsGit = folder.is_git; el('ap-picker-path').value = folder.path; el('ap-picker-list').replaceChildren();
      const entries = folder.parent ? [{name: '..', path: folder.parent}, ...folder.entries] : folder.entries;
      for (const entry of entries) {
        const row = document.createElement('li');
        const open = text('button', entry.name); open.type = 'button'; open.className = 'name'; open.onclick = () => browse(entry.path); row.append(open);
        if (entry.is_git) {row.append(text('span', 'git')); const select = text('button', 'Select'); select.type = 'button'; select.onclick = () => {chooseRepository(entry.path); el('ap-picker').close();}; row.append(select);}
        el('ap-picker-list').append(row);
      }
      el('ap-picker-info').textContent = folder.is_git ? 'このフォルダーはGit Repositoryです。' : 'Git Repositoryのフォルダーを選択してください。';
      el('ap-picker-select').disabled = !folder.is_git;
    } catch (error) { el('ap-picker-info').textContent = error.message; }
  }
  el('ap-browse').onclick = () => {el('ap-picker').showModal(); browse(el('ap-repository').value.trim() || options.repos?.[0] || '');};
  el('ap-picker-go').onclick = () => browse(el('ap-picker-path').value);
  el('ap-picker-path').onkeydown = event => {if (event.key === 'Enter') {event.preventDefault(); browse(event.target.value);}};
  el('ap-picker-hidden').onchange = () => browse(pickerPath);
  el('ap-picker-back').onclick = () => el('ap-picker').close();
  el('ap-picker-select').onclick = () => {if (pickerIsGit) {chooseRepository(pickerPath); el('ap-picker').close();}};
  loadOptions(); refresh(); setInterval(refresh, 3000);
})();
