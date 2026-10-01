// Codex GUI front end: plain JS, polling. Dispatches on <body data-page>.
"use strict";

const $ = (sel) => document.querySelector(sel);
const ACTIVE = ["queued", "starting", "running"];

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function hms(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour12: false });
}
function dt(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return isNaN(d) ? "-" : d.toLocaleString([], { hour12: false });
}
function errText(data) {
  const d = data && data.detail;
  if (!d) return "request failed";
  return typeof d === "string" ? d : d.message || JSON.stringify(d);
}
async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const err = new Error(errText(data));
    err.code = data && data.detail && data.detail.code;
    throw err;
  }
  return data;
}
const statusBadge = (s) => `<span class="status ${esc(s)}">${esc(s)}</span>`;
const repoName = (p) => p.split("/").filter(Boolean).pop() || p;

// ---------------- dashboard ----------------

function initDashboard() {
  const dialog = $("#new-task-dialog");
  const form = $("#new-task-form");
  const f = form.elements; // f.name etc. would collide with built-in properties
  const formError = $("#form-error");

  async function refresh() {
    try {
      const { tasks, counts } = await api("GET", "/api/tasks");
      $("#tasks-body").innerHTML = tasks.length
        ? tasks.map((t) => `
          <tr data-id="${esc(t.id)}">
            <td class="wrap"><a href="/tasks/${esc(t.id)}">${esc(t.name)}</a></td>
            <td title="${esc(t.repository)}">${esc(repoName(t.repository))}</td>
            <td>${esc(t.model || "default")}${t.reasoning_effort !== "default" ? " / " + esc(t.reasoning_effort) : ""}</td>
            <td>${statusBadge(t.status)}</td>
            <td>${esc(t.git_summary)}</td>
            <td>${esc(t.branch)}${t.branch_deleted ? " (deleted)" : ""}</td>
            <td>${esc(dt(t.created_at))}</td>
          </tr>`).join("")
        : `<tr><td colspan="7" class="muted">No tasks yet. Click "+ New Task".</td></tr>`;
      const n = (k) => counts[k] || 0;
      const active = n("queued") + n("starting") + n("running");
      $("#summary").innerHTML =
        `Running: <b>${active}</b> &nbsp; Completed: <b>${n("completed")}</b> &nbsp; Failed: <b>${n("failed")}</b>` +
        ` &nbsp; Stopped: <b>${n("stopped") + n("interrupted")}</b> &nbsp; Total: <b>${tasks.length}</b>`;
    } catch (e) {
      $("#summary").textContent = "cannot reach server: " + e.message;
    }
  }

  $("#tasks-body").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr[data-id]");
    if (tr && !ev.target.closest("a")) location.href = "/tasks/" + tr.dataset.id;
  });

  $("#new-task-btn").addEventListener("click", async () => {
    formError.hidden = true;
    try {
      const { repos } = await api("GET", "/api/repos");
      $("#repo-list").innerHTML = repos.map((r) => `<option value="${esc(r)}">`).join("");
      if (!f.repository.value && repos.length) f.repository.value = repos[0];
    } catch (_) {}
    dialog.showModal();
    (f.repository.value ? f.prompt : f.repository).focus();
  });
  $("#cancel-btn").addEventListener("click", () => dialog.close());

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const btn = $("#run-btn");
    btn.disabled = true;
    btn.textContent = "Creating worktree…";
    formError.hidden = true;
    try {
      await api("POST", "/api/tasks", {
        repository: f.repository.value,
        base_ref: f.base_ref.value,
        name: f.name.value,
        prompt: f.prompt.value,
        model: f.model.value,
        reasoning_effort: f.reasoning_effort.value,
        auto_approval: f.auto_approval.checked,
      });
      f.prompt.value = "";
      f.name.value = "";
      dialog.close();
      refresh();
    } catch (e) {
      formError.textContent = e.message;
      formError.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = "Run";
    }
  });

  refresh();
  setInterval(refresh, 2000);
}

// ---------------- task detail ----------------

function initTask() {
  const id = document.body.dataset.taskId;
  let task = null;
  let offset = 0;
  let gitTab = "status";
  let entryCount = 0;
  let stopping = false;
  const logEl = $("#log");
  const MAX_ROWS = 5000;

  function setMsg(text, isError) {
    const el = $("#action-msg");
    el.textContent = text || "";
    el.className = isError ? "error" : "muted";
    el.hidden = !text;
  }

  function renderTask() {
    const t = task;
    const active = ACTIVE.includes(t.status);
    document.title = `${t.name} - Codex GUI`;
    $("#task-name").textContent = t.name;
    $("#task-status").className = "status " + t.status;
    $("#task-status").textContent = t.status;
    $("#prompt").textContent = t.prompt;
    $("#git-branch").textContent = t.branch;
    const rows = [
      ["Status", t.status], ["Repository", t.repository],
      ["Branch", t.branch + (t.branch_deleted ? " (deleted)" : "")], ["Worktree", t.worktree + (t.worktree_removed ? " (removed)" : "")],
      ["Base ref", `${t.base_ref} (${t.base_sha.slice(0, 10)})`], ["Model", t.model || "default"],
      ["Reasoning effort", t.reasoning_effort], ["Auto approval", t.auto_approval ? "on (--approve-for-me)" : "off"],
      ["Started at", dt(t.started_at)], ["Finished at", dt(t.finished_at)],
      ["PID", t.pid ?? "-"], ["Exit code", t.exit_code ?? "-"],
    ];
    $("#meta").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");

    if (!active) stopping = false;
    $("#stop-btn").hidden = !active;
    $("#stop-btn").disabled = stopping;
    $("#stop-btn").textContent = stopping ? "Stopping…" : "Stop";
    const canGit = !active && !t.worktree_removed;
    $("#commit-btn").hidden = !canGit;
    $("#push-btn").hidden = !canGit;
    $("#del-wt-btn").hidden = active || t.worktree_removed;
    $("#del-branch-btn").hidden = !(t.worktree_removed && !t.branch_deleted);
  }

  function addEntries(entries) {
    const stick = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 30;
    const frag = document.createDocumentFragment();
    for (const e of entries) {
      const div = document.createElement("div");
      const kind = e.stream === "stdout" ? (e.type === "raw" ? "raw" : "") : e.stream;
      div.className = "entry " + kind + (e.type.endsWith("/agent_message") ? " agent_message" : "") + (e.event ? " has-event" : "");
      div.innerHTML = `<span class="ts">${esc(hms(e.ts))}</span><span class="type" title="${esc(e.type)}">${esc(e.type)}</span><span class="msg">${esc(e.message)}</span>`;
      if (e.event) {
        div.querySelector(".type").addEventListener("click", () => {
          const next = div.querySelector("pre.raw-json");
          if (next) { next.remove(); return; }
          const pre = document.createElement("pre");
          pre.className = "raw-json";
          pre.textContent = JSON.stringify(e.event, null, 2);
          div.querySelector(".msg").appendChild(pre);
        });
      }
      frag.appendChild(div);
    }
    logEl.appendChild(frag);
    entryCount += entries.length;
    while (logEl.childElementCount > MAX_ROWS) logEl.firstElementChild.remove();
    $("#log-count").textContent = `(${entryCount} lines)`;
    if (stick) logEl.scrollTop = logEl.scrollHeight;
  }

  async function pullLog() {
    for (let i = 0; i < 20; i++) { // a burst may need several chunks
      const data = await api("GET", `/api/tasks/${id}/log?offset=${offset}`);
      if (data.offset === offset) return;
      offset = data.offset;
      addEntries(data.entries);
    }
  }

  function colorDiff(text) {
    return text.split("\n").map((l) => {
      const e = esc(l);
      if (l.startsWith("diff --git")) return `<span class="file">${e}</span>`;
      if (l.startsWith("@@")) return `<span class="hunk">${e}</span>`;
      if (l.startsWith("+") && !l.startsWith("+++")) return `<span class="add">${e}</span>`;
      if (l.startsWith("-") && !l.startsWith("---")) return `<span class="del">${e}</span>`;
      return e;
    }).join("\n");
  }

  async function refreshGit() {
    try {
      const g = await api("GET", `/api/tasks/${id}/git`);
      if (!g.available) {
        const msg = g.error ? "git error: " + g.error : "worktree no longer exists";
        $("#git-status").textContent = $("#git-stat").textContent = $("#git-diff-body").textContent = $("#git-log").textContent = msg;
        return;
      }
      $("#git-status").textContent = g.status || "(clean)";
      $("#git-stat").textContent = g.diff_stat || "(no changes vs base)";
      $("#git-diff-body").innerHTML = g.diff ? colorDiff(g.diff) : "";
      $("#git-log").textContent = g.log;
    } catch (e) {
      $("#git-status").textContent = "error: " + e.message;
    }
  }

  document.querySelectorAll("#git-tabs button").forEach((b) => b.addEventListener("click", () => {
    gitTab = b.dataset.tab;
    document.querySelectorAll("#git-tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $("#git-status").hidden = gitTab !== "status";
    $("#git-diff").hidden = gitTab !== "diff";
    $("#git-log").hidden = gitTab !== "log";
  }));

  async function action(label, fn) {
    setMsg(label + "…");
    try {
      const out = await fn();
      setMsg(out || label + ": done");
    } catch (e) {
      setMsg(label + " failed: " + e.message, true);
    }
    await tick(true);
  }

  $("#stop-btn").addEventListener("click", () => {
    stopping = true;
    action("Stop", async () => { await api("POST", `/api/tasks/${id}/stop`); });
  });
  $("#commit-btn").addEventListener("click", () => {
    const message = prompt("Commit message (all changes are staged with git add -A):", `codex-gui: ${task.name}`);
    if (message === null) return;
    action("Commit", async () => (await api("POST", `/api/tasks/${id}/commit`, { message })).output);
  });
  $("#push-btn").addEventListener("click", () => {
    if (!confirm(`git push -u origin ${task.branch}?`)) return;
    action("Push", async () => (await api("POST", `/api/tasks/${id}/push`)).output || "pushed");
  });
  $("#del-wt-btn").addEventListener("click", () => action("Delete worktree", async () => {
    try {
      await api("DELETE", `/api/tasks/${id}/worktree`);
    } catch (e) {
      if (e.code !== "dirty") throw e;
      if (!confirm("The worktree has uncommitted changes (incl. untracked files) that will be LOST.\nThe branch and its commits are kept.\n\nDelete anyway?")) {
        throw new Error("cancelled");
      }
      await api("DELETE", `/api/tasks/${id}/worktree?force=true`);
    }
    return "worktree deleted (branch kept)";
  }));
  $("#del-branch-btn").addEventListener("click", () => action("Delete branch", async () => {
    if (!confirm(`Delete branch ${task.branch}? (git branch -d; refuses if unmerged)`)) throw new Error("cancelled");
    try {
      await api("DELETE", `/api/tasks/${id}/branch`);
    } catch (e) {
      if (e.code !== "unmerged") throw e;
      if (!confirm("The branch has commits that are not merged anywhere and will be lost.\n\nForce delete (git branch -D)?")) {
        throw new Error("cancelled");
      }
      await api("DELETE", `/api/tasks/${id}/branch?force=true`);
    }
    return "branch deleted";
  }));

  let lastGit = 0;
  let wasActive = true;
  async function tick(forceGit) {
    try {
      task = await api("GET", `/api/tasks/${id}`);
      renderTask();
      await pullLog();
      const active = ACTIVE.includes(task.status);
      const now = Date.now();
      if (forceGit || (active && now - lastGit > 3000) || (wasActive && !active) || lastGit === 0) {
        lastGit = now;
        await refreshGit();
      }
      wasActive = active;
    } catch (e) {
      setMsg("cannot refresh: " + e.message, true);
    }
  }

  // Poll fast while active; slow down once the task is finished.
  (async function loop() {
    await tick();
    setTimeout(loop, task && ACTIVE.includes(task.status) ? 1000 : 4000);
  })();
}

const page = document.body.dataset.page;
if (page === "dashboard") initDashboard();
else if (page === "task") initTask();
