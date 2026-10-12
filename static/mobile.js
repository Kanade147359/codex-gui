/* Responsive presentation only. Execution and dependency decisions stay in app.js/the server. */
"use strict";
window.MobileUI = (() => {
  const q = s => document.querySelector(s);
  const mobile = matchMedia("(max-width: 768px)");
  const body = document.body, dashboard = body.dataset.page === "dashboard";
  let tasks = [], queueBusy = false, queueAgain = false, queueFetchedAt = 0;
  const scheduled = new Map();
  const moves = [];
  const taskLink = (id, hash = "") => `/tasks/${encodeURIComponent(id)}${hash}`;
  const dependencyText = t => t.deps_total ? `${t.deps_done}/${t.deps_total} successful` : "None";
  const lastUpdate = t => [t.updated_at, t.last_turn_at, t.finished_at, t.started_at, t.created_at]
    .filter(Boolean).sort().at(-1);
  const progress = t => ["running", "starting"].includes(t.status)
    ? `<div class="task-progress"><progress aria-label="Execution in progress"></progress><span>${esc(t.status_detail || "実行中 · Logsで進捗を確認")}</span></div>` : "";

  function card(t, hash = "", queue = false) {
    return `<article class="task-card" data-task-card="${esc(t.id)}">
      <a class="task-card-link" href="${taskLink(t.id, hash)}"><strong>${esc(t.name)}</strong>
        <div>${statusCell(t)} <span class="muted">${esc(t.execution_status || t.status)}</span></div>
        <dl><dt>Model</dt><dd>${esc(t.effective_model || t.model || "default")}</dd>
          <dt>Service tier</dt><dd>${esc(["default", "standard", ""].includes(t.service_tier || "") ? "Standard" : "Fast")}</dd>
          <dt>Dependencies</dt><dd>${esc(dependencyText(t))}</dd>
          <dt>Updated</dt><dd>${esc(dt(lastUpdate(t)))}</dd></dl>${progress(t)}
        ${queue ? `<p>Dependencies: ${(t.dependencies || []).map(d => `${esc(d.name)} (${esc(taskStatusText(d))})`).join(", ") || "None"}</p>
          <p>Scheduled time: ${esc(dt(t.next_retry_at))}${!t.next_retry_at ? " (条件成立・thread idle時)" : ""}</p>
          <p>Blocked reason: ${esc(["blocked", "failed", "waiting_dependencies", "retry_wait"].includes(t.status) ? t.status_detail || t.outcome_reason || "—" : "—")}</p>` : ""}
        ${t.scheduled_pending ? `<p>Scheduled: ${Number(t.scheduled_pending)}</p>` : ""}</a>
      <div class="card-actions"><a class="button" href="${taskLink(t.id, "#task-controls")}">${t.status === "interrupted" ? "Resume…" : "Controls…"}</a>
        <details><summary aria-label="More task actions">More…</summary><div class="card-menu">
          <a href="${taskLink(t.id, "#logs")}">Logs</a><a href="${taskLink(t.id, "#deps-section")}">Dependencies</a>
          <a href="${taskLink(t.id, "#schedule-box")}">Schedule</a><a href="/dependencies">依存グラフを開く</a>
        </div></details></div></article>`;
  }

  // Avoid replacing focused controls or an open overflow menu on every polling tick.
  function update(host, html) {
    if (!host || host.innerHTML === html || host.querySelector("details[open]")) return;
    if (host.contains(document.activeElement) && document.activeElement.matches("input, textarea, select, button")) return;
    host.innerHTML = html;
  }
  function renderLists() {
    if (!dashboard || !mobile.matches) return;
    update(q("#task-cards"), tasks.map(t => card(t)).join("") || '<p class="muted">No tasks yet. Tap + New Task.</p>');
    update(q("#mobile-log-tasks"), tasks.map(t => card(t, "#logs")).join("") || '<p class="muted">No tasks yet.</p>');
    const queued = tasks.filter(t => ["queued", "starting", "running", "waiting_dependencies", "retry_wait", "blocked"].includes(t.status));
    let html = queued.map(t => card(t, "#deps-section", true)).join("");
    for (const t of tasks) for (const s of scheduled.get(t.id) || []) {
      if (["completed", "cancelled"].includes(s.status)) continue;
      html += `<article class="task-card"><a class="task-card-link" href="${taskLink(t.id, '#scheduled-' + s.id)}">
        <strong>${esc(t.name)} · Instruction #${Number(s.id)}</strong>${statusBadge(s.status)}
        <p class="sched-prompt">${esc(s.prompt)}</p><p>Dependencies: ${(s.dependencies || []).map(d => `${esc(d.name)} (${esc(taskStatusText(d))})`).join(", ") || "None"}</p>
        <p>Scheduled time: ${esc(dt(s.scheduled_at))}${!s.scheduled_at ? " (条件成立・thread idle時)" : ""}</p>
        <p>Reserved: ${esc(dt(s.created_at))} · ${esc(s.speed || "Standard")}</p>
        <p>Blocked reason: ${esc(s.blocked_reason || s.wait_note || "—")}</p></a>
        <a class="button" href="${taskLink(t.id, '#scheduled-' + s.id)}">予約を確認・操作</a></article>`;
    }
    update(q("#mobile-queue"), html || '<p class="muted">No queued tasks or scheduled instructions.</p>');
  }
  async function refreshQueue(force = false) {
    if (!dashboard || !mobile.matches || body.dataset.mobileView !== "queue") return;
    if (queueBusy) { queueAgain = true; return; }
    if (!force && Date.now() - queueFetchedAt < 5000) return;
    queueBusy = true;
    try {
      // Counts exclude blocked reservations. Read existing schedules while Queue is open,
      // including tasks with zero pending count, so blocked/failed instructions remain visible.
      const results = await Promise.allSettled(tasks
        .map(async t => [t.id, (await api("GET", `/api/tasks/${encodeURIComponent(t.id)}/scheduled`)).scheduled]));
      scheduled.clear();
      for (const r of results) if (r.status === "fulfilled") scheduled.set(...r.value);
      queueFetchedAt = Date.now();
      q("#mobile-queue-error").textContent = results.some(r => r.status === "rejected") ? "予約情報を取得できません。次の更新で再試行します。" : "";
      renderLists();
    } finally {
      queueBusy = false;
      if (queueAgain) { queueAgain = false; refreshQueue(); }
    }
  }
  function route() {
    const hash = location.hash.slice(1), tab = ["tasks", "queue", "logs", "settings"].includes(hash) ? hash : "tasks";
    body.dataset.mobileView = dashboard ? tab : hash === "logs" ? "logs" : "tasks";
    document.querySelectorAll("[data-mobile-tab]").forEach(a => {
      if (a.dataset.mobileTab === body.dataset.mobileView) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    if (dashboard) {
      document.querySelectorAll("[data-mobile-panel]").forEach(el => {
        el.classList.toggle("mobile-panel-off", !el.dataset.mobilePanel.split(" ").includes(tab));
      });
      renderLists(); refreshQueue(true);
    }
  }

  // Move existing controls, preserving their listeners and desktop order across breakpoint changes.
  function arrangeControls() {
    if (body.dataset.page !== "task") return;
    if (!moves.length) {
      for (const [host, ids] of [["#mobile-task-tools .inline-actions", ["agents-btn", "commit-btn", "push-btn", "del-wt-btn", "del-branch-btn"]],
        ["#mobile-execution-tools .inline-actions", ["retry-medium-btn", "retry-high-btn", "new-session-btn", "compact-btn", "instruction-hint"]]]) {
        for (const id of ids) {
          const el = document.getElementById(id), marker = document.createComment(id);
          el.before(marker); moves.push({el, marker, host});
        }
      }
    }
    for (const m of moves) mobile.matches ? q(m.host).appendChild(m.el) : m.marker.after(m.el);
    measureComposer();
  }
  function measureComposer() {
    const composer = q("#instruction-composer");
    if (composer) document.documentElement.style.setProperty("--composer-height", `${composer.getBoundingClientRect().height}px`);
  }
  function viewportChanged() {
    const v = window.visualViewport;
    const inset = v ? Math.max(0, innerHeight - v.height - v.offsetTop) : 0;
    const editing = document.activeElement?.matches("textarea, input:not([type=checkbox]):not([type=radio])");
    // resizes-content browsers shrink innerHeight; iOS instead shrinks just visualViewport.
    // Keep the composer stable between pointerdown (input blur) and pointerup on Send.
    // Restore navigation when the viewport expands, rather than moving the button on blur.
    const keyboard = mobile.matches && (editing || body.classList.contains("keyboard-open")) &&
      (inset > 100 || innerHeight < baselineHeight - 100);
    body.classList.toggle("keyboard-open", Boolean(keyboard));
    document.documentElement.style.setProperty("--keyboard-inset", `${keyboard ? inset : 0}px`);
    document.documentElement.style.setProperty("--visual-height", `${v?.height || innerHeight}px`);
    measureComposer();
  }
  let baselineHeight = innerHeight;
  window.addEventListener("orientationchange", () => { baselineHeight = innerHeight; });
  window.addEventListener("resize", viewportChanged);
  window.visualViewport?.addEventListener("resize", viewportChanged);
  window.visualViewport?.addEventListener("scroll", viewportChanged);
  document.addEventListener("focusin", viewportChanged);
  document.addEventListener("focusout", () => setTimeout(viewportChanged, 0));
  window.addEventListener("hashchange", route);
  mobile.addEventListener("change", () => { arrangeControls(); route(); viewportChanged(); });
  q("#mobile-execution-tools")?.addEventListener("toggle", measureComposer);
  if (q("#instruction-composer")) new ResizeObserver(measureComposer).observe(q("#instruction-composer"));
  arrangeControls(); route(); viewportChanged();
  return {
    confirmAction(message) { return !mobile.matches || confirm(message); },
    renderTasks(list) {
      if (JSON.stringify(list.map(t => [t.id, t.status, t.scheduled_pending])) !== JSON.stringify(tasks.map(t => [t.id, t.status, t.scheduled_pending]))) queueFetchedAt = 0;
      tasks = list; renderLists(); refreshQueue();
    },
    renderTask(t) {
      if (!mobile.matches) return;
      q("#mobile-execution-status").innerHTML = statusCell(t) + progress(t) + `<p>Updated: ${esc(dt(lastUpdate(t)))}</p>`;
      if (!t.dependencies?.length && !t.dependents?.length) {
        q("#deps-section").hidden = false; q("#deps-items").innerHTML = '<li class="muted">No dependencies.</li>';
      }
    },
  };
})();
