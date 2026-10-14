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
    const link = document.createElement('a'); link.href = '/autopilot'; link.textContent = `${data.node.state} / ${data.node.reason}`;
    const explanation = document.createElement('p'); explanation.textContent = data.approval_kind;
    const history = document.createElement('pre'); history.textContent = JSON.stringify(data.node,null,2);
    target.append(title,link,explanation,history);
  }
  refresh(); setInterval(refresh,3000);
})();
