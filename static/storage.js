(() => {
  const $ = id => document.getElementById(id);
  const size = bytes => `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
  const date = value => value ? new Date(value).toLocaleString() : '不明';
  async function request(url, method = 'GET', data) {
    const r = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, body: data === undefined ? undefined : JSON.stringify(data)});
    const body = await r.json();
    if (!r.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail));
    return body;
  }
  function cell(row, text) {
    const node = document.createElement('td');
    node.textContent = text;
    row.append(node);
  }
  function report(data) {
    if (!data.items) return;
    $('gc-summary').textContent = `${data.dry_run ? 'Dry Run' : 'GC'}: 候補 ${data.candidate_count} 件 / ${size(data.candidate_bytes)}、検出 ${data.items.length} 件 / ${size(data.total_bytes)}、解放 ${size(data.released_bytes)}（${date(data.scanned_at)}）`;
    $('gc-reasons').textContent = Object.entries(data.excluded_reasons || {}).map(([reason, count]) => `${reason}: ${count}`).join(' / ');
    if (data.discovery_errors?.length) $('gc-reasons').textContent += ` / 調査できなかったパス: ${data.discovery_errors.length} 件`;
    $('gc-items').replaceChildren();
    for (const item of data.items) {
      const tr = document.createElement('tr');
      cell(tr, `${item.path} / ${(item.task_ids || []).join(', ')}`);
      cell(tr, size(item.bytes));
      cell(tr, date(item.last_used_at));
      cell(tr, item.deleted ? '削除済み' : item.candidate ? '削除候補' : item.reasons.join(', '));
      $('gc-items').append(tr);
    }
  }
  async function refresh(loadSettings = false) {
    const state = await request('/api/artifact-gc');
    if (loadSettings) {
      $('gc-days').value = state.settings.retention_days;
      $('gc-delete').checked = state.settings.auto_delete;
    }
    $('gc-run').disabled = !state.settings.auto_delete || state.running;
    $('gc-dry-run').disabled = state.running;
    if (state.running) $('gc-message').textContent = 'GC調査を実行中です。更新すると進捗を確認できます。';
    $('gc-history').replaceChildren();
    for (const run of state.history) {
      const button = document.createElement('button');
      button.textContent = `#${run.id} ${date(run.started_at)} / ${run.dry_run ? 'Dry Run' : '削除'} / ${run.trigger} / ${run.status} / 解放 ${size(run.released_bytes)}`;
      button.addEventListener('click', () => report(run.report));
      const line = document.createElement('p');
      line.append(button);
      $('gc-history').append(line);
    }
    const latest = state.history.find(run => run.report.items);
    if (latest) report(latest.report);
  }
  async function action(fn) {
    try { await fn(); } catch (error) { $('gc-message').textContent = error.message; }
  }
  $('gc-settings').addEventListener('submit', event => {
    event.preventDefault();
    action(async () => {
      await request('/api/artifact-gc/settings', 'PUT', {retention_days: Number($('gc-days').value), auto_delete: $('gc-delete').checked});
      $('gc-message').textContent = '設定を保存しました。';
      await refresh();
    });
  });
  async function run(dry_run) {
    $('gc-dry-run').disabled = $('gc-run').disabled = true;
    $('gc-message').textContent = '調査中…';
    try {
      const data = await request('/api/artifact-gc/run', 'POST', {dry_run});
      $('gc-message').textContent = '完了しました。';
      await refresh();
      report(data);
    } finally { await refresh(); }
  }
  $('gc-dry-run').addEventListener('click', () => action(() => run(true)));
  $('gc-run').addEventListener('click', () => action(() => run(false)));
  $('gc-refresh').addEventListener('click', () => action(() => refresh()));
  action(() => refresh(true));
})();
