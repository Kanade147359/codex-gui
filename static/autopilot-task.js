(() => {
  const target = document.getElementById('ap-task-review');
  if (!target) return;
  async function refresh() {
    const response = await fetch(`/api/autopilot/tasks/${target.dataset.taskId}`);
    if (!response.ok) return;
    const data = await response.json();
    if (!data.autopilot) {target.hidden = true; return;}
    target.hidden = false;
    target.replaceChildren();
    const title = document.createElement('h2'); title.textContent = 'Autopilot review';
    const link = document.createElement('a'); link.href = data.workflow_url || `/autopilot/workflows/${encodeURIComponent(data.workflow_id)}`; link.textContent = '大タスクの進捗・確認事項を開く';
    const explanation = document.createElement('p'); explanation.textContent = data.approval_kind;
    const narrative = document.createElement('p'); narrative.textContent = data.review_summary?.text || data.node.reason;
    const history = document.createElement('pre'); history.textContent = JSON.stringify(data.node,null,2);
    const details = document.createElement('details'), summary = document.createElement('summary'); summary.textContent = 'レビューの詳細データ'; details.append(summary,history);
    target.append(title,link,explanation,narrative,details);
  }
  refresh(); setInterval(refresh,3000);
})();
