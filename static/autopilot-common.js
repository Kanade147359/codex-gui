/* Shared Autopilot presentation. All provider content is rendered as text. */
(() => {
  const notes = new Map();
  const text = (tag, value = '') => { const node = document.createElement(tag); node.textContent = value; return node; };
  const link = (label, url) => { const node = text('a', label); node.href = url; return node; };
  const workflowURL = id => `/autopilot/workflows/${encodeURIComponent(id)}`;
  const workflowAPI = id => `/api/autopilot/workflows/${encodeURIComponent(id)}`;
  async function api(url, body) {
    const response = await fetch(url, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail?.message || (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) || `HTTP ${response.status}`);
    return data;
  }
  function details(label, value, key) {
    const box = document.createElement('details'); box.dataset.persist = key || label;
    box.append(text('summary', label), text('pre', JSON.stringify(value, null, 2)));
    return box;
  }
  function replace(target, children) {
    const opened = new Set([...target.querySelectorAll('details[open]')].map(node => node.dataset.persist));
    const focused = target.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused?.dataset.focusKey;
    const selection = focused && typeof focused.selectionStart === 'number' ? [focused.selectionStart, focused.selectionEnd] : null;
    target.replaceChildren(...children);
    for (const box of target.querySelectorAll('details')) if (opened.has(box.dataset.persist)) box.open = true;
    if (focusKey) {
      const restored = [...target.querySelectorAll('[data-focus-key]')].find(node => node.dataset.focusKey === focusKey);
      if (restored) { restored.focus({preventScroll: true}); if (selection) restored.setSelectionRange(...selection); }
    }
  }
  function actionButton(label, operation, onError) {
    const button = text('button', label); button.type = 'button';
    button.onclick = async () => {
      if (button.disabled) return;
      button.disabled = true;
      try { await operation(); } catch (error) { onError(error); } finally { button.disabled = false; }
    };
    return button;
  }
  function controls(workflow, refresh, onError) {
    const group = document.createElement('div'); group.className = 'ap-actions';
    const labels = {start: '開始', pause: '一時停止', resume: '再開', stop: '停止', reconcile: '状態を再確認'};
    const actions = workflow.state === 'draft' ? ['start', 'stop', 'reconcile'] : ['pause', 'resume', 'stop', 'reconcile'];
    for (const action of actions) {
      const button = actionButton(labels[action], async () => { await api(`${workflowAPI(workflow.id)}/${action}`, {}); await refresh(); }, onError);
      button.dataset.action = action;
      button.disabled = (workflow.state === 'stopped' && action !== 'reconcile') ||
        (action === 'pause' && workflow.state !== 'running') || (action === 'resume' && workflow.state === 'running');
      if (action === 'stop') button.classList.add('danger');
      group.append(button);
    }
    return group;
  }
  function progress(workflow) {
    const counts = workflow.progress || {};
    const total = counts.total ?? workflow.nodes?.length ?? 0;
    const accepted = counts.accepted ?? workflow.nodes?.filter(node => node.state === 'accepted').length ?? 0;
    const box = document.createElement('div'); box.className = 'ap-progress';
    const bar = document.createElement('progress'); bar.max = total || 1; bar.value = accepted;
    bar.setAttribute('aria-label', `完了 ${accepted} / ${total} 子タスク`);
    box.append(bar, text('p', `${accepted} / ${total} 完了 · 実行中 ${counts.active ?? 0} · 待機 ${counts.waiting ?? 0} · 要対応 ${counts.blocked ?? 0}`));
    return box;
  }
  function settingsLabel(settings) {
    const model = settings?.actual_model || settings?.model || 'Codex CLIの設定';
    const effort = settings?.actual_reasoning_effort || settings?.reasoning_effort || 'high';
    return `${model} / ${effort.toUpperCase()}${settings?.actual_model ? '（実行報告）' : ''}`;
  }
  function settingsTable(settings) {
    const table = document.createElement('table'); table.className = 'ap-table';
    const head = document.createElement('tr');
    for (const label of ['子タスク', '実効モデル・推論', '指定元']) head.append(text('th', label));
    table.append(head);
    const sourceLabel = value => ({workflow: '基本設定', task: '子タスク設定', 'CLI configuration': 'CLI設定', 'default high': '既定High'}[value] || value || '基本設定');
    for (const [name, value] of Object.entries(settings || {})) {
      const row = document.createElement('tr');
      row.append(text('td', name), text('td', settingsLabel(value)),
        text('td', `モデル: ${sourceLabel(value.model_source)} / 推論: ${sourceLabel(value.reasoning_source)}`));
      table.append(row);
    }
    return table;
  }
  function ownerNote(key, label) {
    const input = document.createElement('textarea'); input.rows = 2; input.placeholder = '判断の理由や追加条件';
    input.setAttribute('aria-label', label); input.dataset.focusKey = key;
    input.value = notes.get(key) || ''; input.oninput = () => notes.set(key, input.value);
    return input;
  }
  function brief(value) {
    if (typeof value === 'string') return value;
    if (!value || typeof value !== 'object') return '';
    return value.summary || value.reason || value.description || value.observed || value.classification || '';
  }
  function graphRevision(revision, workflow, refresh, onError, withActions) {
    const card = document.createElement('article'); card.className = 'ap-revision';
    const payload = revision.payload;
    card.append(text('h3', `新タスク生成 / 根本原因の再計画: ${revision.state}`),
      text('p', `元タスク: ${payload.source_node}。追加する子タスク: ${payload.added.join(', ')}。`));
    const analysis = brief(payload.proposal.analysis);
    if (analysis) card.append(text('p', analysis));
    const comparison = document.createElement('table'); comparison.className = 'ap-table';
    const heading = document.createElement('tr');
    for (const value of ['タスク', '変更前の依存', '変更後の依存']) heading.append(text('th', value));
    comparison.append(heading);
    const oldNodes = new Map(workflow.plan.nodes.map(node => [node.id, node]));
    for (const [id, spec] of Object.entries(payload.before)) oldNodes.set(id, spec);
    for (const spec of payload.plan.nodes) {
      const row = document.createElement('tr');
      row.append(text('td', payload.added.includes(spec.id) ? `${spec.id} (新規)` : spec.id),
        text('td', payload.added.includes(spec.id) ? '—' : (oldNodes.get(spec.id)?.depends_on || []).join(', ') || 'なし'),
        text('td', spec.depends_on.join(', ') || 'なし'));
      comparison.append(row);
    }
    card.append(comparison);
    if (withActions && workflow.state !== 'stopped') {
      const note = ownerNote(`graph:${revision.id}:${revision.decision_target}`, 'graph revision owner rationale');
      card.append(note);
      for (const action of ['approve', 'reject']) {
        const button = actionButton(`Owner ${action} new tasks`, async () => {
          await api(`${workflowAPI(workflow.id)}/graph-revisions/${encodeURIComponent(revision.id)}/decision`,
            {owner_action: true, expected_target: revision.decision_target, action, note: note.value});
          await refresh();
        }, onError);
        button.disabled = action === 'approve' && payload.owner_changes.length > 0;
        card.append(button);
      }
      if (payload.owner_changes.length) card.append(text('p', '所有者の追加設定が必要です。要求された変更は詳細データで確認できます。'));
    }
    card.append(details('再計画の詳細データ', {root_cause: payload.proposal.analysis, tasks: payload.proposal.tasks,
      graph_before: payload.before, graph_after: payload.plan.nodes.map(node => ({id: node.id, depends_on: node.depends_on})),
      requires_owner: [...payload.expansions, ...payload.owner_changes], digest: revision.decision_target}, `graph:${revision.id}`));
    return card;
  }
  function renderWorkflow(target, workflow, refresh, onError, {compact = false} = {}) {
    const children = [];
    if (compact) {
      const title = text('h3'); title.append(link(`${workflow.plan.name} — 大タスク詳細を開く`, workflowURL(workflow.id)));
      children.push(title, progress(workflow));
    }
    const review = document.createElement('section'); review.className = 'ap-review-summary'; review.id = 'ap-review-summary';
    review.append(text('h2', `Needs Review — 確認事項 ${workflow.review_summary?.count ?? 0}件`));
    const narrative = text('p', workflow.review_summary?.text || '現在、人の確認が必要な事項はありません。');
    narrative.className = 'ap-review-text'; review.append(narrative);
    for (const node of workflow.nodes.filter(node => node.state === 'blocked_decision')) {
      const card = document.createElement('fieldset'); card.append(text('legend', `${node.name} の判断`));
      if (workflow.state !== 'stopped') {
        const note = ownerNote(`${node.id}:${node.decision_target}`, `${node.name} owner rationale`); card.append(note);
        for (const action of ['approve', 'reject', 'resume']) card.append(actionButton(`Owner ${action}`, async () => {
          await api(`${workflowAPI(workflow.id)}/nodes/${encodeURIComponent(node.name)}/decision`,
            {owner_action: true, expected_target: node.decision_target, action, note: note.value}); await refresh();
        }, onError));
      }
      card.append(details('判断対象・根拠データ', {target: node.decision_target, reason: node.reason, spec: node.spec}, `decision:${node.id}`));
      review.append(card);
    }
    for (const revision of workflow.graph_revisions || []) if (revision.state === 'awaiting_owner') {
      review.append(graphRevision(revision, workflow, refresh, onError, true));
    }
    for (const revision of workflow.plan_revisions || []) if (revision.state === 'awaiting_owner') {
      const card = document.createElement('fieldset'); card.append(text('legend', '計画変更の判断'));
      if (workflow.state !== 'stopped') for (const action of ['approve', 'reject']) card.append(actionButton(`Owner ${action} plan revision`, async () => {
        await api(`${workflowAPI(workflow.id)}/revisions/${encodeURIComponent(revision.id)}/decision`,
          {owner_action: true, expected_target: revision.decision_target, action}); await refresh();
      }, onError));
      card.append(details('計画変更の詳細データ', revision.payload, `plan:${revision.id}`)); review.append(card);
    }
    if (workflow.review_summary?.count) review.append(details('集約した確認事項の詳細データ', workflow.review_summary.items, 'review-items'));
    children.push(review);
    const goals = document.createElement('section'); goals.append(text('h2', '全体目標 / Goal-Aware Review'));
    for (const goal of workflow.plan.goals || []) goals.append(text('p', `${goal.id}: ${brief(goal)}`));
    goals.append(text('p', `再計画: ${workflow.plan.policy.replanning || 'propose_only'} · 追加可能な残りタスク数: ${workflow.replanning_limits?.remaining_nodes ?? '—'}`));
    goals.append(details('目標・再計画上限の詳細データ', {goals: workflow.plan.goals || [], limits: workflow.replanning_limits}, 'goals'));
    children.push(goals, text('h2', '子タスク'));
    for (const node of workflow.nodes) {
      const card = document.createElement('article'); card.className = 'ap-node'; card.id = `ap-node-${node.name}`;
      const repairLabel = {fix_queued: '自動修復待機', reserving_fix: '自動修復を準備中', fixing: '自動修復中'}[node.state];
      card.append(text('h3', `${node.name} (${node.spec.kind}) — ${repairLabel || node.state}`));
      if (repairLabel) card.append(text('p', 'LLMが停止理由と既存の成果を確認し、同じタスクで修正・再検証を進めます。'));
      else if (node.reason && !node.state.startsWith('blocked_')) card.append(text('p', node.reason));
      card.append(text('p', `依存: ${node.spec.depends_on.join(', ') || 'なし'} · 修正残: ${Math.max(0, workflow.plan.policy.max_fix_rounds - node.fix_rounds)}`),
        text('p', `実行: ${settingsLabel(node.execution_settings)} · 目標ID: ${(node.spec.goal_ids || []).join(', ') || '—'}`));
      if (node.task_id) card.append(link('Task詳細', `/tasks/${encodeURIComponent(node.task_id)}`));
      const history = document.createElement('details'); history.dataset.persist = `node-history:${node.id}`;
      history.append(text('summary', '実装・検証・レビューの履歴'));
      for (const candidate of node.candidates || []) {
        history.append(text('h4', `候補 ${candidate.generation}: ${candidate.state}`));
        for (const result of candidate.validation || []) history.append(text('p', `検証: ${result.state}`));
        for (const result of candidate.reviews || []) {
          const payload = result.payload || {};
          history.append(text('p', `独立レビュー: ${result.provider} / ${payload.verdict || result.state}${payload.reason ? ` — ${payload.reason}` : ''}`));
          for (const finding of payload.blocking || []) history.append(text('p', `${finding.id}: ${finding.reason}`));
          if (payload.goal_review) {
            history.append(text('h4', 'Goal Alignment'));
            for (const cause of payload.goal_review.root_causes || []) history.append(text('p', `${cause.classification}: ${cause.observed}`));
          }
        }
      }
      for (const operation of node.operations || []) if (operation.payload?.semantic_recovery) {
        const result = operation.payload.implementation_result;
        const outcome = result || operation.payload.source_result || {};
        history.append(text('p', `LLM自動修復 ${operation.generation}: ${operation.state} — ${result ? '修復結果' : '開始理由'}: ${outcome.status || ''} ${outcome.reason || ''}`));
      }
      if (repairLabel && node.reason) history.append(details('現在の修正指示', node.reason, `repair:${node.id}`));
      for (const mentor of node.mentors || []) history.append(text('p', `メンター: ${mentor.provider || ''} / ${mentor.state} — ${brief(mentor.payload)}`));
      history.append(details('固定入力・レビュー・操作の詳細データ', {pinned_inputs: node.inputs, candidates: node.candidates,
        operations: node.operations, owner_decisions: node.decisions, mentors: node.mentors, execution_settings: node.execution_settings}, `node-raw:${node.id}`));
      card.append(history); children.push(card);
    }
    const revisions = document.createElement('details'); revisions.dataset.persist = 'revisions';
    revisions.append(text('summary', '計画・再計画の履歴'));
    for (const revision of workflow.graph_revisions || []) if (revision.state !== 'awaiting_owner') {
      revisions.append(graphRevision(revision, workflow, refresh, onError, false));
    }
    for (const revision of workflow.plan_revisions || []) if (revision.state !== 'awaiting_owner') {
      revisions.append(details(`計画revision: ${revision.state}`, revision.payload, `plan-history:${revision.id}`));
    }
    for (const replan of workflow.replans || []) revisions.append(details(`独立Codex再分析: ${replan.state}`, replan, `replan:${replan.id}`));
    children.push(revisions);
    replace(target, children);
  }
  window.AutopilotUI = {api, text, link, details, replace, controls, progress, settingsLabel, settingsTable,
    renderWorkflow, workflowURL, workflowAPI, actionButton};
})();
