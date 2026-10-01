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
const num = (n) => (n == null ? "-" : Number(n).toLocaleString("en-US"));
const pct = (r) => (r == null ? "-" : r.toFixed(r >= 99.95 || r === 0 ? 0 : 1) + "%");
const repoName = (p) => p.split("/").filter(Boolean).pop() || p;

// ---------------- dashboard ----------------

function initDashboard() {
  const dialog = $("#new-task-dialog");
  const form = $("#new-task-form");
  const f = form.elements; // f.name etc. would collide with form's built-in properties
  const formError = $("#form-error");
  const formOk = $("#form-ok");
  const filterEl = $("#repo-filter");
  let allTasks = [];
  let options = { models: [], default_model: "", default_effort: "", repos: [] };

  const store = {
    get(k) { try { return localStorage.getItem(k) || ""; } catch (_) { return ""; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (_) {} },
  };
  filterEl.dataset.want = store.get("repoFilter");

  // ----- task table -----

  function renderTasks() {
    const repos = [...new Set(allTasks.map((t) => t.repository))].sort();
    const want = filterEl.dataset.want;
    filterEl.innerHTML = `<option value="">All repositories</option>` +
      repos.map((r) => `<option value="${esc(r)}" ${r === want ? "selected" : ""}>${esc(repoName(r))} — ${esc(r)}</option>`).join("");
    if (!repos.includes(want)) filterEl.dataset.want = "";
    const tasks = filterEl.dataset.want ? allTasks.filter((t) => t.repository === filterEl.dataset.want) : allTasks;

    $("#tasks-body").innerHTML = tasks.length
      ? tasks.map((t) => `
        <tr data-id="${esc(t.id)}">
          <td class="wrap"><a href="/tasks/${esc(t.id)}">${esc(t.name)}</a></td>
          <td title="${esc(t.repository)}">${esc(repoName(t.repository))}</td>
          <td>${esc(t.model || "default")}${t.reasoning_effort !== "default" ? " / " + esc(t.reasoning_effort) : ""}</td>
          <td>${statusBadge(t.status)}</td>
          <td title="cache hit rate of the latest turn (cached / input)">${esc(pct(t.cache_hit_rate))}</td>
          <td>${esc(t.git_summary)}</td>
          <td>${esc(t.branch)}${t.branch_deleted ? " (deleted)" : ""}</td>
          <td>${esc(dt(t.created_at))}</td>
        </tr>`).join("")
      : `<tr><td colspan="7" class="muted">No tasks yet. Click "+ New Task".</td></tr>`;
    const n = (k) => tasks.filter((t) => t.status === k).length;
    const active = n("queued") + n("starting") + n("running");
    $("#summary").innerHTML =
      `Running: <b>${active}</b> &nbsp; Completed: <b>${n("completed")}</b> &nbsp; Failed: <b>${n("failed")}</b>` +
      ` &nbsp; Stopped: <b>${n("stopped") + n("interrupted")}</b> &nbsp; Total: <b>${tasks.length}</b>`;
  }

  async function refresh() {
    try {
      allTasks = (await api("GET", "/api/tasks")).tasks;
      renderTasks();
    } catch (e) {
      $("#summary").textContent = "cannot reach server: " + e.message;
    }
  }

  filterEl.addEventListener("change", () => {
    filterEl.dataset.want = filterEl.value;
    store.set("repoFilter", filterEl.value);
    renderTasks();
  });
  $("#tasks-body").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr[data-id]");
    if (tr && !ev.target.closest("a")) location.href = "/tasks/" + tr.dataset.id;
  });

  // ----- model / effort selects -----

  const modelSel = $("#model-select");
  const effortSel = $("#effort-select");
  const CUSTOM = "__custom__";

  function buildModelSelect() {
    const def = options.default_model ? ` (${options.default_model})` : "";
    modelSel.innerHTML =
      `<option value="">Codex default${esc(def)}</option>` +
      options.models.map((m) => `<option value="${esc(m.slug)}">${esc(m.name)} — ${esc(m.slug)}</option>`).join("") +
      `<option value="${CUSTOM}">Custom…</option>`;
    buildEffortSelect();
  }

  function buildEffortSelect() {
    const slug = modelSel.value || options.default_model;
    const info = options.models.find((m) => m.slug === slug);
    const efforts = info && info.efforts.length ? info.efforts : ["low", "medium", "high"];
    const dflt = !modelSel.value && options.default_effort ? options.default_effort : info ? info.default_effort : "";
    const prev = effortSel.value;
    effortSel.innerHTML = `<option value="default">default${dflt ? " (" + esc(dflt) + ")" : ""}</option>` +
      efforts.map((e) => `<option value="${esc(e)}">${esc(e)}</option>`).join("");
    if ([...effortSel.options].some((o) => o.value === prev)) effortSel.value = prev;
    $("#model-custom").hidden = modelSel.value !== CUSTOM;
  }
  modelSel.addEventListener("change", buildEffortSelect);

  function selectedModel() {
    return modelSel.value === CUSTOM ? f.model_custom.value.trim() : modelSel.value;
  }

  // ----- base ref (branches / worktrees / remotes / tags of the chosen repository) -----

  const baseSel = $("#base-select");
  let refsFor = "";

  function baseValue() {
    return baseSel.value === CUSTOM ? f.base_custom.value.trim() : baseSel.value;
  }

  async function loadRefs() {
    const repo = f.repository.value.trim();
    if (!repo || repo === refsFor) return;
    refsFor = repo;
    const prev = baseValue();
    let r = null;
    try { r = await api("GET", `/api/refs?repository=${encodeURIComponent(repo)}`); } catch (_) {}
    if (repo !== f.repository.value.trim()) return; // the field changed while we were waiting
    const opt = (v, label) => `<option value="${esc(v)}">${esc(label)}</option>`;
    const group = (title, items) => items.length ? `<optgroup label="${esc(title)}">${items.join("")}</optgroup>` : "";
    if (!r) {
      baseSel.innerHTML = opt("main", "main") + opt(CUSTOM, "Custom…");
      refsFor = ""; // not a usable repository (yet): try again next time
    } else {
      baseSel.innerHTML =
        group("Branches", r.branches.map((b) => opt(b.name, b.name + (b.name === r.current ? "  (checked out)" : "") + (b.subject ? "  — " + b.subject.slice(0, 50) : "")))) +
        group("Worktrees (committed state only)", r.worktrees.map((w) => opt(w.ref, `${w.ref}  — ${w.path.split("/").slice(-2).join("/")}`))) +
        group("Remote branches", r.remotes.map((b) => opt(b.name, b.name))) +
        group("Tags", r.tags.map((b) => opt(b.name, b.name))) +
        opt(CUSTOM, "Custom…");
    }
    const values = [...baseSel.options].map((o) => o.value);
    baseSel.value = values.includes(prev) && prev ? prev : r && values.includes(r.default) ? r.default : values[0];
    $("#base-custom").hidden = baseSel.value !== CUSTOM;
  }
  baseSel.addEventListener("change", () => { $("#base-custom").hidden = baseSel.value !== CUSTOM; });

  // ----- recent repositories -----

  function renderRecent() {
    $("#recent-repos").innerHTML = options.repos.slice(0, 6)
      .map((r) => `<button type="button" class="chip" data-path="${esc(r)}" title="${esc(r)}">${esc(repoName(r))}</button>`).join("");
  }
  $("#recent-repos").addEventListener("click", (ev) => {
    const b = ev.target.closest("button[data-path]");
    if (b) { f.repository.value = b.dataset.path; loadRefs(); f.prompt.focus(); }
  });

  // ----- folder picker -----

  const picker = $("#picker");
  let pickerPath = "";
  let pickerIsGit = false;

  async function browse(path) {
    const info = $("#picker-info");
    try {
      const d = await api("GET", `/api/fs?path=${encodeURIComponent(path)}&hidden=${$("#picker-hidden").checked}`);
      pickerPath = d.path;
      pickerIsGit = d.is_git;
      $("#picker-path").value = d.path;
      const rows = [];
      if (d.parent) rows.push(`<li data-path="${esc(d.parent)}"><span class="name">..</span></li>`);
      for (const e of d.entries) {
        rows.push(`<li data-path="${esc(e.path)}" class="${e.is_git ? "git" : ""}"><span class="name">${esc(e.name)}/</span>` +
          (e.is_git ? `<span class="badge">git</span><button type="button" class="pick" data-pick="${esc(e.path)}">Select</button>` : "") + `</li>`);
      }
      $("#picker-list").innerHTML = rows.join("") || `<li class="muted">(no sub-folders)</li>`;
      info.textContent = d.is_git ? "This folder is a Git repository." : "Open a folder marked “git”, or navigate into one.";
      info.className = d.is_git ? "ok" : "muted";
      $("#picker-select").disabled = !d.is_git;
    } catch (e) {
      info.textContent = e.message;
      info.className = "error";
    }
  }
  function showPicker(on) {
    picker.hidden = !on;
    $("#form-body").hidden = on;
  }
  $("#browse-btn").addEventListener("click", () => {
    showPicker(true);
    browse(f.repository.value.trim() || options.repos[0] || "");
  });
  $("#picker-list").addEventListener("click", (ev) => {
    const pick = ev.target.closest("button[data-pick]");
    if (pick) { f.repository.value = pick.dataset.pick; showPicker(false); loadRefs(); return; }
    const li = ev.target.closest("li[data-path]");
    if (li) browse(li.dataset.path);
  });
  $("#picker-go").addEventListener("click", () => browse($("#picker-path").value));
  $("#picker-path").addEventListener("keydown", (ev) => { if (ev.key === "Enter") { ev.preventDefault(); browse(ev.target.value); } });
  $("#picker-hidden").addEventListener("change", () => browse(pickerPath));
  $("#picker-back").addEventListener("click", () => showPicker(false));
  $("#picker-select").addEventListener("click", () => {
    if (pickerIsGit) { f.repository.value = pickerPath; showPicker(false); loadRefs(); }
  });

  // ----- new task dialog -----

  $("#new-task-btn").addEventListener("click", async () => {
    formError.hidden = formOk.hidden = true;
    showPicker(false);
    try {
      options = await api("GET", "/api/options");
    } catch (_) {}
    buildModelSelect();
    renderRecent();
    if (options.error) { formError.textContent = options.error + " (use Custom… to type a model id)"; formError.hidden = false; }
    if (!f.repository.value) f.repository.value = filterEl.dataset.want || options.repos[0] || "";
    refsFor = "";
    loadRefs();
    dialog.showModal();
    (f.repository.value ? f.prompt : f.repository).focus();
  });
  f.repository.addEventListener("change", loadRefs);
  f.repository.addEventListener("input", () => { clearTimeout(loadRefs.t); loadRefs.t = setTimeout(loadRefs, 400); });
  $("#cancel-btn").addEventListener("click", () => dialog.close());

  async function submitTask(keepOpen) {
    const buttons = [$("#run-btn"), $("#run-more-btn")];
    buttons.forEach((b) => (b.disabled = true));
    $("#run-btn").textContent = "Creating worktree…";
    formError.hidden = formOk.hidden = true;
    try {
      if (!form.reportValidity()) return;
      const task = await api("POST", "/api/tasks", {
        repository: f.repository.value,
        base_ref: baseValue(),
        name: f.name.value,
        prompt: f.prompt.value,
        model: selectedModel(),
        reasoning_effort: effortSel.value,
        auto_approval: f.auto_approval.checked,
      });
      f.prompt.value = "";
      f.name.value = "";
      if (keepOpen) {
        formOk.textContent = `Started “${task.name}” on ${task.branch}. Add the next one.`;
        formOk.hidden = false;
        f.prompt.focus();
      } else {
        dialog.close();
      }
      refresh();
    } catch (e) {
      formError.textContent = e.message;
      formError.hidden = false;
    } finally {
      buttons.forEach((b) => (b.disabled = false));
      $("#run-btn").textContent = "Run";
    }
  }
  form.addEventListener("submit", (ev) => { ev.preventDefault(); submitTask(false); });
  $("#run-more-btn").addEventListener("click", () => submitTask(true));

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
  let sending = false;
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
      ["Codex session", t.codex_thread_id || "-"], ["Last turn at", dt(t.last_turn_at)],
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

    // Additional instruction: Send resumes the session, Start New Session is the explicit alternative.
    const canSend = !active && !t.worktree_removed;
    $("#send-btn").disabled = !canSend || !t.codex_thread_id || sending;
    $("#new-session-btn").disabled = !canSend || sending;
    $("#instruction").disabled = !canSend;
    $("#session-id").textContent = t.codex_thread_id ? `session ${t.codex_thread_id}` : "";
    $("#instruction-hint").textContent =
      active ? "Task is running: send the next instruction when it has finished." :
      t.worktree_removed ? "The worktree was deleted." :
      !t.codex_thread_id ? "No Codex session id was recorded for this task: use Start New Session." : "";
  }

  function renderUsage(u) {
    const l = u.latest;
    const rows = l ? [
      ["Input", num(l.input_tokens)], ["Cached", num(l.cached_input_tokens)],
      ["Uncached", num(l.uncached_input_tokens)], ["Cache hit", pct(l.cache_hit_rate)],
      ...(l.cache_write_input_tokens != null ? [["Cache write", num(l.cache_write_input_tokens)]] : []),
      ["Output", num(l.output_tokens)],
      ...(l.reasoning_output_tokens != null ? [["Reasoning", num(l.reasoning_output_tokens)]] : []),
    ] : [];
    $("#latest-usage").innerHTML = l
      ? `<div class="muted">Latest usage (turn ${l.turn})</div><dl class="usage">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`
      : "no completed turn yet";
    $("#turns").hidden = !u.turns.length;
    $("#turns-body").innerHTML = u.turns.map((r) =>
      `<tr><td>${r.turn}</td><td>${r.session}</td><td>${num(r.input_tokens)}</td><td>${num(r.cached_input_tokens)}</td>` +
      `<td>${esc(pct(r.cache_hit_rate))}</td><td>${num(r.output_tokens)}</td></tr>`).join("");
  }

  async function refreshUsage() {
    try { renderUsage(await api("GET", `/api/tasks/${id}/usage`)); } catch (_) {}
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

  async function sendInstruction(path, label, confirmText) {
    const prompt = $("#instruction").value;
    if (!prompt.trim()) { setMsg("Write an instruction first.", true); return; }
    if (confirmText && !confirm(confirmText)) return;
    sending = true;
    renderTask();
    try {
      await action(label, async () => {
        await api("POST", `/api/tasks/${id}/${path}`, { prompt });
        $("#instruction").value = "";
        return label + ": started";
      });
    } finally {
      sending = false;
      renderTask();
    }
  }
  $("#send-btn").addEventListener("click", () => sendInstruction("messages", "Send"));
  $("#new-session-btn").addEventListener("click", () => sendInstruction("new-session", "Start New Session",
    "Start a NEW Codex session in this worktree?\n\nThe conversation so far is not carried over and the previous session's cached input is not reused."));

  let lastGit = 0;
  let lastUsage = 0;
  let wasActive = true;
  async function tick(forceGit) {
    try {
      task = await api("GET", `/api/tasks/${id}`);
      renderTask();
      await pullLog();
      const active = ACTIVE.includes(task.status);
      const now = Date.now();
      if (forceGit || (active && now - lastUsage > 3000) || (wasActive && !active) || lastUsage === 0) {
        lastUsage = now;
        await refreshUsage();
      }
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
