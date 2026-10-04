// Codex GUI front end: plain JS, polling. Dispatches on <body data-page>.
"use strict";

const $ = (sel) => document.querySelector(sel);
const ACTIVE = ["queued", "starting", "running"];
const WAITING = ["waiting_dependencies", "retry_wait"];   // the scheduler holds them; no process is running
const BUSY = [...ACTIVE, ...WAITING];

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
const statusBadge = (s) => `<span class="status ${esc(s)}">${esc(statusText(s))}</span>`;
const num = (n) => (n == null ? "-" : Number(n).toLocaleString("en-US"));
const pct = (r) => (r == null ? "-" : r.toFixed(r >= 99.95 || r === 0 ? 0 : 1) + "%");
// ----- Efficiency (credit/API-equivalent estimates, never real subscription savings) -----
const eqCredits = (v) => (v == null ? "-" : Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 }) + " credits eq.");
const eqUsd = (v) => (v == null ? "-" : "$" + Number(v).toFixed(v !== 0 && Math.abs(v) < 0.01 ? 4 : 2) + " eq.");
const eqMultiplier = (v) => (v == null ? "-" : Number(v).toFixed(1) + "x");
const EFFICIENCY_PERIODS = [["today", "Today", "TODAY"], ["7d", "7 Days", "7 DAY"], ["lifetime", "Lifetime", "LIFETIME"]];
function efficiencyRows(a, mode) {
  // `a` is an aggregate (exact / estimated / total buckets). With estimated turns the exact-only part is shown too.
  const t = a.total, hasEst = a.estimated.turns > 0;
  const f = (fmt, path) => { const v = path(t); return v == null ? "-" : fmt(v) + (hasEst ? ` (exact only: ${fmt(path(a.exact))}; includes estimated)` : ""); };
  const rows = [];
  if (mode === "period") {
    rows.push(["Total input", num(a.input_tokens)], ["Cached input", num(a.cached_input_tokens)],
      ["Uncached input", `${num(a.uncached_input_tokens)} (${pct(a.uncached_input_rate)})`],
      ["Cache write", `${num(a.cache_write_input_tokens)} tokens (part of the uncached input; priced separately where a rate is known)`],
      ["Overall cache hit", pct(a.cache_hit_rate)]);
    if (!a.turns) { rows.push(["Savings", "no turns in this period"]); return rows; }
  } else rows.push(["Cache read (cached input)", num(a.cached_input_tokens)], ["Cache write", num(a.cache_write_input_tokens)],
    ["Uncached input", num(a.uncached_input_tokens)], ["Cache hit", pct(a.cache_hit_rate)]);
  if (!t.turns) { rows.push(["Savings", "pricing unavailable"]); return rows; }
  if (mode === "period") {
    rows.push(["Cache write cost (API-equivalent)", f(eqUsd, (b) => b.usd.cache_write_cost)],
      ["Effective input multiplier", f(eqMultiplier, (b) => b.effective_input_multiplier)],
      ["Credit-equivalent saved by cache", f(eqCredits, (b) => b.credits.saved_by_cache)],
      ["API-equivalent saved by cache", f(eqUsd, (b) => b.usd.saved_by_cache)],
      ["Credit-equivalent saved vs Astra (same-token estimate)", f(eqCredits, (b) => b.credits.saved_vs_astra)],
      ["API-equivalent saved vs Astra (same-token estimate)", f(eqUsd, (b) => b.usd.saved_vs_astra)]);
  } else {
    rows.push(["Actual credit-equivalent", f(eqCredits, (b) => b.credits.actual)],
      ["Without-cache equivalent", f(eqCredits, (b) => b.credits.no_cache)],
      ["Saved by cache", f(eqCredits, (b) => b.credits.saved_by_cache)],
      ["Cache write cost (API-equivalent)", f(eqUsd, (b) => b.usd.cache_write_cost)],
      ["Saved %", pct(t.saved_percent)],
      ["Astra Standard same-token equivalent (estimate)", f(eqCredits, (b) => b.credits.astra_same_token)],
      ["Saved vs Astra (same-token estimate)", f(eqCredits, (b) => b.credits.saved_vs_astra)]);
  }
  return rows;
}
function efficiencyHtml(a, mode) {
  const dl = efficiencyRows(a, mode).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
  const fast = mode === "period" && a.fast_tasks
    ? `<p class="small">Fast tasks: ${a.fast_tasks}<br>Included allowance multiplier: ${a.allowance_multiplier_fast}x vs Standard <span class="muted">(allowance only; Fast pricing above is Standard x 2)</span></p>` : "";
  return `<dl class="usage">${dl}</dl>${fast}<p class="small muted">${a.notes.map(esc).join(" ")}</p>`;
}
const repoName = (p) => p.split("/").filter(Boolean).pop() || p;
const kfmt = (n) => (n == null ? "-" : n >= 1000 ? Math.round(n / 1000) + "k" : String(n));
const EFFORT_LABELS = { default: "Auto", low: "Low", medium: "Medium", high: "High", xhigh: "XHigh", max: "Max", ultra: "Ultra" };
const effortLabel = (e) => EFFORT_LABELS[e] || (e ? e[0].toUpperCase() + e.slice(1) : "-");
const STATUS_TEXT = { "waiting-for-quota": "Waiting for Codex quota", waiting_dependencies: "Waiting for dependencies", retry_wait: "Retrying", blocked: "Blocked" };
const statusText = (s) => STATUS_TEXT[s] || s;
// "Retry 1/3 in 18s": counts down in the browser from next_retry_at (the server only sends the time).
function retryIn(t) {
  const s = Math.max(0, Math.ceil((new Date(t.next_retry_at) - Date.now()) / 1000));
  return s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`;
}
function taskStatusText(t) {
  if (t.status === "waiting_dependencies") return t.deps_total ? `Waiting (${t.deps_done}/${t.deps_total} complete)` : "Waiting for dependencies";
  if (t.status === "retry_wait") return `Retry ${t.retry_count}/${t.max_retries} in ${retryIn(t)}`;
  return statusText(t.status);
}
function statusCell(t) {
  const live = t.status === "retry_wait" ? ` data-retry-at="${esc(t.next_retry_at)}" data-retry="${t.retry_count}/${t.max_retries}"` : "";
  let html = `<span class="status ${esc(t.status)}"${live}>${esc(taskStatusText(t))}</span>`;
  if (t.status === "blocked" && t.status_detail) html += `<div class="sub">${esc(t.status_detail)}</div>`;
  if (t.status === "retry_wait" && t.last_failure_message) html += `<div class="sub" title="${esc(t.last_failure_message)}">${esc(t.last_failure_message.slice(0, 80))}</div>`;
  return html;
}
function tickRetryCountdowns() {
  document.querySelectorAll("[data-retry-at]").forEach((el) => {
    el.textContent = `Retry ${el.dataset.retry} in ${retryIn({ next_retry_at: el.dataset.retryAt })}`;
  });
}
setInterval(tickRetryCountdowns, 1000);
const DEP_MARK = { completed: ["✓", "ok"], failed: ["✗", "bad"], stopped: ["✗", "bad"], blocked: ["✗", "bad"] };
function resetText(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
    : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}
function bar(percent, cls) {
  const p = percent == null ? 0 : Math.max(0, Math.min(100, percent));
  const level = cls || (p >= 95 ? "hot" : p >= 80 ? "warm" : "ok");
  return `<div class="bar ${level}"><div class="fill" style="width:${p}%"></div></div>`;
}

// Folded sections (<details data-fold>) start closed and remember what the user opened.
function rememberFolds() {
  document.querySelectorAll("details[data-fold]").forEach((d) => {
    const key = "fold:" + d.dataset.fold;
    try { d.open = localStorage.getItem(key) === "1"; } catch (_) {}
    d.addEventListener("toggle", () => { try { localStorage.setItem(key, d.open ? "1" : "0"); } catch (_) {} });
  });
}

// ---------------- dashboard ----------------

function initDashboard() {
  const dialog = $("#new-task-dialog");
  const form = $("#new-task-form");
  const f = form.elements; // f.name etc. would collide with form's built-in properties
  const formError = $("#form-error");
  const formOk = $("#form-ok");
  const filterEl = $("#repo-filter");
  let allTasks = [];
  let options = { models: [], default_model: "", default_effort: "", recommended_model: "", repos: [] };

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
          <td class="wrap"><a href="/tasks/${esc(t.id)}">${esc(t.name)}</a>${scheduledBadge(t)}</td>
          <td title="${esc(t.repository)}">${esc(repoName(t.repository))}</td>
          <td title="${esc(t.effective_model || "")}">${esc(modelName(t.effective_model || t.model))}</td>
          <td>${esc(effortLabel(t.reasoning_effort))}${t.service_tier !== "default" ? ` <span class="warn" title="Fast mode consumes included usage more quickly.">fast</span>` : ""}</td>
          <td title="cache hit rate of the latest turn (cached / input)">${esc(pct(t.cache_hit_rate))}</td>
          <td title="current context / model window">${ctxCell(t.context)}${ctxBadges(t)}</td>
          <td>${statusCell(t)}</td>
          <td>${esc(t.git_summary)}</td>
          <td>${esc(t.branch)}${t.branch_deleted ? " (deleted)" : ""}</td>
          <td>${esc(dt(t.created_at))}</td>
        </tr>`).join("")
      : `<tr><td colspan="10" class="muted">No tasks yet. Click "+ New Task".</td></tr>`;
    const n = (k) => tasks.filter((t) => t.status === k).length;
    const active = n("queued") + n("starting") + n("running");
    $("#running-count").textContent = active;
    const nInt = n("interrupted");
    $("#resume-all-btn").hidden = !nInt;
    $("#resume-all-btn").textContent = `Resume interrupted (${nInt})`;
    $("#summary").innerHTML =
      `Running: <b>${active}</b> &nbsp; Completed: <b>${n("completed")}</b> &nbsp; Failed: <b>${n("failed")}</b>` +
      ` &nbsp; Waiting for quota: <b>${n("waiting-for-quota")}</b>` +
      ` &nbsp; Waiting: <b>${n("waiting_dependencies") + n("retry_wait")}</b> &nbsp; Blocked: <b>${n("blocked")}</b>` +
      ` &nbsp; Stopped: <b>${n("stopped") + n("interrupted")}</b> &nbsp; Total: <b>${tasks.length}</b>`;
  }

  // "Scheduled: 3 · Ready: 1" under the name: only a hint, the details are in Task Detail.
  function scheduledBadge(t) {
    if (!t.scheduled_pending) return "";
    return `<div class="sched-badge" title="Scheduled instructions for this task's thread (see Task Detail)">Scheduled: ${t.scheduled_pending}` +
      `${t.scheduled_ready ? ` · Ready: ${t.scheduled_ready}` : ""}</div>`;
  }

  // "GPT-6.1-Sol" for gpt-6.1-sol when the catalog knows it; otherwise the id; "default" when nothing is pinned.
  function modelName(slug) {
    if (!slug) return "default";
    const m = options.models.find((x) => x.slug === slug);
    return m ? m.name.replace(/^GPT-/i, "").replace(/-/g, " ") : slug;
  }
  // Long-context zone (from the latest request's context size) and the number of compactions of the thread.
  function ctxBadges(t) {
    const z = { long: ["LONG", "error", "GPT-6.1 Sol long-context pricing zone"], strong: ["!!", "error", "close to the long-context pricing threshold"],
                warning: ["!", "warn", "approaching the long-context pricing threshold"] }[t.ctx_zone];
    return (z ? ` <span class="${z[1]}" title="${z[2]}">${z[0]}</span>` : "") +
           (t.compactions ? ` <span class="muted" title="compactions of this thread">C${t.compactions}</span>` : "");
  }
  function ctxCell(c) {
    if (!c || c.tokens == null) return "-";
    const text = kfmt(c.tokens);
    return c.warn ? `<span class="warn" title="Context usage ${Math.round(c.percent)}%: this thread is large">${text} ⚠</span>` : text;
  }

  // ----- Codex sign-in (the same as `codex login`; the browser talks to OpenAI, the GUI never sees a password) -----

  const webUrl = (u) => (/^https?:\/\//.test(u || "") ? u : "");
  let accountTimer = null;
  async function refreshAccount() {
    clearTimeout(accountTimer);
    let signedIn = false;
    try {
      const a = await api("GET", "/api/codex/account");
      signedIn = a.signed_in && a.login.status !== "pending";
      renderAccount(a);
    } catch (e) {
      $("#codex-login").hidden = false;
      $("#codex-login-body").innerHTML = `<p class="muted">cannot read the Codex account: ${esc(e.message)}</p>`;
    }
    accountTimer = setTimeout(refreshAccount, signedIn ? 30000 : 3000);
  }
  function renderAccount(a) {
    const l = a.login || {}, box = $("#codex-login"), body = $("#codex-login-body");
    $("#codex-account").textContent = a.signed_in
      ? `Codex: signed in${a.email ? " as " + a.email : ""}${a.plan ? " (" + a.plan + ")" : ""}` : "";
    box.hidden = a.signed_in && l.status !== "pending";
    if (box.hidden) return;
    const url = webUrl(l.url), open = url ? `<a class="button" href="${esc(url)}" target="_blank" rel="noopener">Open sign-in page</a>` : "";
    if (l.status === "pending" && l.method === "device") {
      body.innerHTML = `<p>Open the page below, sign in, and enter this code:</p>
        <p class="login-code">${esc(l.user_code || "")}</p>
        <p>${open} <button data-login="cancel">Cancel</button></p>`;
    } else if (l.status === "pending") {
      body.innerHTML = `<p>Finish signing in in the browser tab that opened. When it is done this page updates by itself.</p>
        <p>${open} <button data-login="cancel">Cancel</button></p>
        <p class="muted small">The browser must be able to reach this machine's localhost (it returns there). From another computer, use a device code.</p>`;
    } else {
      const why = a.account_type === "apiKey" ? "Codex is using an API key, not a ChatGPT account." : "Codex is not signed in.";
      body.innerHTML = `<p>${why} ${l.status === "failed" ? `<span class="warn">Sign-in failed: ${esc(l.error || "")}</span>` : ""}</p>
        <p><button class="primary" data-login="browser">Sign in with ChatGPT (open browser)</button>
        <button data-login="device">Use a device code</button></p>`;
    }
  }
  $("#codex-login-body").addEventListener("click", async (ev) => {
    const method = ev.target.dataset && ev.target.dataset.login;
    if (!method) return;
    try {
      if (method === "cancel") {
        await api("POST", "/api/codex/login/cancel");
      } else {
        const l = await api("POST", "/api/codex/login", { method });
        const url = webUrl(l.url);
        if (url) window.open(url, "_blank", "noopener");  // may be blocked: the "Open sign-in page" link stays
      }
    } catch (e) {
      alert(e.message);
    }
    refreshAccount();
  });

  // ----- Codex usage (display only) -----

  async function refreshLimits() {
    try {
      const l = await api("GET", "/api/limits");
      renderLimits(l);
    } catch (e) {
      $("#limits").textContent = "cannot read usage: " + e.message;
    }
  }
  function renderLimits(l) {
    const box = $("#limits");
    if (!l.available) {
      box.innerHTML = `<span class="muted">not available${l.error ? " (" + esc(l.error) + ")" : ""}</span>`;
      $("#limits-note").textContent = "";
      return;
    }
    $("#usage-plan").textContent = l.plan_type ? `(${l.plan_type})` : "";
    box.className = "";
    box.innerHTML = l.windows.map((w) => `
      <div class="limit-row"><span class="limit-label">${esc(w.label)}</span>${bar(w.used_percent)}
        <span class="limit-pct">${Math.round(w.used_percent)}%</span>
        <span class="muted limit-reset">${w.resets_at ? "Reset: " + esc(resetText(w.resets_at)) : ""}</span></div>`).join("") ||
      `<span class="muted">Codex reported no usage windows</span>`;
    const notes = [];
    if (l.ordinary_usage_allowed === false) notes.push(`<span class="error">Ordinary usage is not available${l.reached_type ? " (" + esc(l.reached_type) + ")" : ""}.</span>`);
    if (l.available_resets != null) notes.push(`Available resets: ${l.available_resets}`);
    $("#limits-note").innerHTML = notes.join(" &nbsp; ");
  }

  // ----- repository bar: git state and AGENTS.md -----

  const repoInfoCache = {};
  async function repoInfo(repo) {
    try {
      repoInfoCache[repo] = await api("GET", `/api/repo-info?repository=${encodeURIComponent(repo)}`);
    } catch (_) {
      repoInfoCache[repo] = null;
    }
    return repoInfoCache[repo];
  }
  async function renderRepoBar() {
    const repo = filterEl.dataset.want;
    $("#repo-bar").hidden = !repo;
    if (!repo) return;
    $("#repo-path").textContent = repo;
    $("#repo-agents-btn").href = `/agents?repository=${encodeURIComponent(repo)}`;
    const info = await repoInfo(repo);
    if (filterEl.dataset.want !== repo) return;
    $("#repo-git").textContent = info ? info.git.label : "unknown";
    const a = info && info.agents_md;
    $("#repo-agents").textContent = !a ? "unknown" : (a.found ? "found" : "not found") + (a.nested.length ? ` (+${a.nested.length} nested)` : "");
  }
  $("#resume-all-btn").addEventListener("click", async () => {
    const ids = allTasks.filter((t) => t.status === "interrupted" && (!filterEl.dataset.want || t.repository === filterEl.dataset.want)).map((t) => t.id);
    if (!ids.length || !confirm(`Resume ${ids.length} interrupted task(s) in their existing Codex threads?\n\nTasks without a recorded session need Start New Session and are skipped.`)) return;
    try {
      const r = await api("POST", "/api/tasks/resume-interrupted", { task_ids: ids });
      if (r.skipped.length) alert(`Resumed ${r.resumed.length}. Skipped ${r.skipped.length}:\n` + r.skipped.map((x) => `${x.id}: ${x.reason}`).join("\n"));
    } catch (e) { alert(e.message); }
    refresh();
  });
  $("#repo-new-btn").addEventListener("click", () => $("#new-task-btn").click());

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
    renderRepoBar();
  });
  $("#tasks-body").addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr[data-id]");
    if (tr && !ev.target.closest("a")) location.href = "/tasks/" + tr.dataset.id;
  });

  // ----- model / effort / speed selects -----

  const modelSel = $("#model-select");
  const effortSel = $("#effort-select");
  const tierSel = $("#tier-select");
  const CUSTOM = "__custom__";
  const FALLBACK_EFFORTS = ["low", "medium", "high"];

  function buildModelSelect() {
    // "Use Codex default" / the recommended model (GPT-6.1 Sol when this codex lists it) / the rest / Other…
    const rec = options.recommended_model;
    const def = options.default_model ? ` (${options.default_model})` : "";
    const recInfo = options.models.find((m) => m.slug === rec);
    const rest = options.models.filter((m) => m.slug !== rec);
    modelSel.innerHTML =
      (recInfo ? `<option value="${esc(rec)}">${esc(recInfo.name)} — recommended</option>` : "") +
      `<option value="">Use Codex default${esc(def)}</option>` +
      rest.map((m) => `<option value="${esc(m.slug)}">${esc(m.name)} — ${esc(m.slug)}</option>`).join("") +
      `<option value="${CUSTOM}">Other…</option>`;
    modelSel.value = recInfo ? rec : "";
    buildEffortSelect(true);
  }

  function currentModelInfo() {
    const slug = modelSel.value || options.default_model;
    return options.models.find((m) => m.slug === slug);
  }

  function buildEffortSelect(reset) {
    // Only the efforts this model supports are offered. Default Low (the model's own default when it has no "low").
    const info = currentModelInfo();
    const efforts = info && info.efforts.length ? info.efforts : FALLBACK_EFFORTS;
    const prev = reset ? "" : effortSel.value;
    const dflt = !modelSel.value && options.default_effort ? options.default_effort : info ? info.default_effort : "";
    effortSel.innerHTML = `<option value="default">Auto${dflt ? " (Codex default: " + esc(effortLabel(dflt)) + ")" : ""}</option>` +
      efforts.map((e) => `<option value="${esc(e)}">${esc(effortLabel(e))}</option>`).join("");
    const wanted = [...effortSel.options].some((o) => o.value === prev) ? prev : efforts.includes("low") ? "low" : "default";
    effortSel.value = wanted;
    $("#model-custom").hidden = modelSel.value !== CUSTOM;
    buildTierSelect();
    showNotes();
  }

  function buildTierSelect() {
    // Standard is the default and always possible; Fast (or whatever else the model offers) only if listed.
    const info = currentModelInfo();
    const tiers = info ? info.service_tiers : [];
    const prev = tierSel.value;
    tierSel.innerHTML = `<option value="default">Standard</option>` +
      tiers.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
    tierSel.value = [...tierSel.options].some((o) => o.value === prev) ? prev : "default";
  }

  function showNotes() {
    $("#ultra-note").hidden = effortSel.value !== "ultra";
    $("#fast-note").hidden = tierSel.value === "default";
  }
  modelSel.addEventListener("change", () => buildEffortSelect(false));
  effortSel.addEventListener("change", showNotes);
  tierSel.addEventListener("change", showNotes);

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
    showRepoAgents();
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

  async function showRepoAgents() {
    const repo = f.repository.value.trim();
    const line = $("#repo-agents-line");
    if (!repo) { line.hidden = true; return; }
    const info = await repoInfo(repo);
    if (repo !== f.repository.value.trim()) return;
    line.hidden = !info;
    if (info) {
      const a = info.agents_md;
      line.innerHTML = `Git: ${esc(info.git.label)} &nbsp; AGENTS.md: <b>${a.found ? "Found" : "Not found"}</b>` +
        (a.nested.length ? ` (+${a.nested.length} nested)` : "") +
        ` &nbsp; <a href="/agents?repository=${encodeURIComponent(repo)}" target="_blank">${a.found ? "edit" : "create"}</a>`;
    }
  }

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

  // ----- one-line summaries of the folded groups of the form -----

  function updateGroupSummaries() {
    const text = (sel) => { const el = $(sel); return el && el.selectedOptions && el.selectedOptions[0] ? el.selectedOptions[0].textContent : ""; };
    $("#recovery-summary").textContent = f.auto_retry.checked ? `· on, max ${f.max_retries.value} retries` : "· off";
    const sub = $("#ctx-subagents");
    $("#ctx-group-summary").textContent = ["Tool profile " + text("#ctx-tool-profile"), "output " + text("#ctx-tool-output"),
      "subagents " + (sub && sub.checked ? "on" : "off")].join(" · ").replace(/^/, "· ");
  }
  form.addEventListener("input", updateGroupSummaries);
  form.addEventListener("change", updateGroupSummaries);

  // ----- new task dialog -----

  $("#new-task-btn").addEventListener("click", async () => {
    formError.hidden = formOk.hidden = true;
    showPicker(false);
    try {
      options = await api("GET", "/api/options");
    } catch (_) {}
    buildModelSelect();
    renderRecent();
    renderDepChoices();
    f.auto_retry.checked = options.default_auto_retry !== false;
    f.max_retries.value = options.default_max_retries ?? 3;
    if (window.CtxUI) CtxUI.initNewTask(options);
    updateGroupSummaries();
    if (options.error) { formError.textContent = options.error + " (use Custom… to type a model id)"; formError.hidden = false; }
    if (!f.repository.value) f.repository.value = filterEl.dataset.want || options.repos[0] || "";
    refsFor = "";
    loadRefs();
    dialog.showModal();
    (f.repository.value ? f.prompt : f.repository).focus();
  });
  // ----- run after other tasks -----

  function renderDepChoices() {
    $("#deps-list").innerHTML = allTasks.map((t) =>
      `<label class="check"><input type="checkbox" name="dep" value="${esc(t.id)}"> ${esc(t.name)} ` +
      `<span class="muted">${esc(repoName(t.repository))} · ${esc(taskStatusText(t))}</span></label>`).join("") ||
      `<span class="muted">No tasks yet.</span>`;
  }
  form.querySelectorAll('input[name="run_mode"]').forEach((r) => r.addEventListener("change", () => {
    $("#deps-box").hidden = f.run_mode.value !== "after";
  }));

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
      const after = f.run_mode.value === "after";
      const dependsOn = after ? [...form.querySelectorAll('input[name="dep"]:checked')].map((c) => c.value) : [];
      if (after && !dependsOn.length) throw new Error("Select at least one task to wait for, or choose Immediately.");
      const task = await api("POST", "/api/tasks", {
        depends_on: dependsOn,
        auto_retry: f.auto_retry.checked,
        max_retries: Number(f.max_retries.value),
        repository: f.repository.value,
        base_ref: baseValue(),
        name: f.name.value,
        prompt: f.prompt.value,
        model: selectedModel(),
        reasoning_effort: effortSel.value,
        auto_approval: f.auto_approval.checked,
        service_tier: tierSel.value,
        model_verbosity: f.model_verbosity.value,
        web_search: f.web_search.value,
        network_access: f.network_access.checked,
        sandbox: f.sandbox.value,
        adaptive_reasoning: f.adaptive_reasoning.checked,
        context_guard: f.context_guard.checked,
        writable_dirs: f.writable_dirs.value,
        feature_flags: f.feature_flags.value,
        ...(window.CtxUI ? CtxUI.formValues() : {}),
      });
      f.prompt.value = "";
      f.name.value = "";
      if (keepOpen) {
        formOk.textContent = task.status === "waiting_dependencies"
          ? `Queued “${task.name}”: it starts when ${dependsOn.length} task(s) have completed. Add the next one.`
          : `Started “${task.name}” on ${task.branch}. Add the next one.`;
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

  async function loadOptions() {
    try {
      options = await api("GET", "/api/options");
      $("#default-model").textContent = options.recommended_model ? modelName(options.recommended_model) : (options.default_model || "Codex default");
      $("#backend-note").textContent = options.backend === "app-server" ? "via codex app-server" : "legacy codex exec backend";
    } catch (_) {}
  }

  loadOptions().then(() => { renderTasks(); });
  refresh();
  setInterval(refresh, 2000);
  refreshLimits();
  setInterval(refreshLimits, 15000);
  refreshAccount();
  const effBox = $("#efficiency-box");
  effBox.open = store.get("efficiencyOpen") !== "0";  // remembered across reloads
  effBox.addEventListener("toggle", () => store.set("efficiencyOpen", effBox.open ? "1" : "0"));
  let effPeriod = "lifetime";
  const effTabs = $("#efficiency-tabs");
  effTabs.innerHTML = EFFICIENCY_PERIODS.map(([id, label]) => `<button type="button" data-period="${id}">${label}</button>`).join("");
  effTabs.addEventListener("click", (e) => {
    const id = e.target.dataset && e.target.dataset.period;
    if (!id || id === effPeriod) return;
    effPeriod = id;
    refreshEfficiency();
  });
  async function refreshEfficiency() {
    const period = effPeriod;
    effTabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.period === period));
    $("#efficiency-title").textContent = EFFICIENCY_PERIODS.find((p) => p[0] === period)[2] + " EFFICIENCY";
    try {
      const a = await api("GET", "/api/efficiency?period=" + period);
      if (period === effPeriod) $("#efficiency").innerHTML = efficiencyHtml(a, "period"); // drop a stale response
    } catch (_) {}
  }
  refreshEfficiency();
  setInterval(refreshEfficiency, 15000);
  setTimeout(renderRepoBar, 300);
  if (window.CtxUI) { CtxUI.bindNewTask(); CtxUI.initThresholds(); }
  setInterval(() => { delete repoInfoCache[filterEl.dataset.want]; renderRepoBar(); }, 20000);
}

// ---------------- task detail ----------------

function initTask() {
  rememberFolds();
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
    const waiting = WAITING.includes(t.status);
    const busy = active || waiting;
    document.title = `${t.name} - Codex GUI`;
    $("#task-name").textContent = t.name;
    $("#task-status").className = "status " + t.status;
    $("#task-status").textContent = taskStatusText(t);
    if (t.status === "retry_wait") { $("#task-status").dataset.retryAt = t.next_retry_at; $("#task-status").dataset.retry = `${t.retry_count}/${t.max_retries}`; }
    else { delete $("#task-status").dataset.retryAt; }
    $("#prompt").textContent = t.prompt;
    $("#git-branch").textContent = t.branch;
    const rows = [
      ["Status", taskStatusText(t)], ["Repository", t.repository],
      ["Branch", t.branch + (t.branch_deleted ? " (deleted)" : "")], ["Worktree", t.worktree + (t.worktree_removed ? " (removed)" : t.worktree_pending ? " (created when the task starts)" : "")],
      ["Base ref", `${t.base_ref} (${t.base_sha.slice(0, 10)})`], ["Model", t.effective_model || t.model || "default"],
      ["Reasoning", effortLabel(t.reasoning_effort)], ["Speed", t.service_tier === "default" ? "Standard" : t.service_tier],
      ["Output verbosity", t.model_verbosity], ["Web search", { cached: "cached", live: "live", disabled: "off" }[t.web_search_mode] || (t.web_search_enabled ? "live" : "off")], ["Network access", t.network_access ? "on" : "off"],
      ["Auto approval", t.auto_approval ? "on (--approve-for-me)" : "off"], ["Sandbox", t.sandbox],
      ["Adaptive reasoning", t.adaptive_reasoning ? "on (suggestions only)" : "off"], ["Context guard", t.context_guard ? "on" : "off"],
      ["Started at", dt(t.started_at)], ["Finished at", dt(t.finished_at)],
      ["Backend", t.backend], ["Exit code", t.exit_code ?? "-"],
      ["Codex thread", t.codex_thread_id || "-"], ["Last turn at", dt(t.last_turn_at)],
    ];
    if (t.status_detail) rows.push(["Detail", t.status_detail]);
    $("#meta").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("#meta-summary").textContent = "· " + [t.effective_model || t.model || "default", effortLabel(t.reasoning_effort),
      t.service_tier === "default" ? "Standard" : t.service_tier, t.sandbox, t.branch].join(" · ") + (t.status_detail ? ` · ${t.status_detail}` : "");

    if (!busy) stopping = false;
    $("#stop-btn").hidden = !busy;
    $("#stop-btn").disabled = stopping;
    $("#stop-btn").textContent = stopping ? "Stopping…" : "Stop";
    const hasWorktree = !t.worktree_removed && !t.worktree_pending;
    const canGit = !busy && hasWorktree;
    $("#commit-btn").hidden = !canGit;
    $("#push-btn").hidden = !canGit;
    $("#del-wt-btn").hidden = busy || !hasWorktree;
    $("#del-branch-btn").hidden = !(t.worktree_removed && !t.branch_deleted);
    $("#agents-btn").hidden = !hasWorktree;

    // Additional instruction. Idle: Send resumes the thread. Running (app-server): Send steers the running turn.
    const idle = !busy && t.status !== "blocked" && hasWorktree;
    const steerable = active && t.status === "running" && t.backend === "app-server" && !t.worktree_removed;
    $("#send-btn").textContent = steerable ? "Send to running turn" : "Send Standard";
    $("#send-btn").disabled = sending || !(steerable || (idle && t.codex_thread_id));
    $("#send-fast-btn").hidden = active;  // the speed is chosen per turn; an instruction added to a running turn has none
    $("#send-fast-btn").disabled = sending || !(idle && t.codex_thread_id);
    $("#new-session-btn").disabled = !idle || sending;
    $("#resume-btn").hidden = t.status !== "interrupted";
    $("#resume-btn").disabled = sending || !t.codex_thread_id;
    const canSchedule = !t.worktree_removed;  // a reservation can be made while the thread is busy: that is its point
    $("#instruction").disabled = !(idle || steerable || canSchedule);
    $("#session-id").textContent = t.codex_thread_id ? `thread ${t.codex_thread_id}` : "";
    $("#instruction-hint").textContent =
      waiting ? "The task is waiting for the scheduler; it takes instructions once it has run." :
      t.status === "blocked" ? "The task is blocked by a dependency: use Run Anyway or retry the failed dependency." :
      active && !steerable ? (t.backend === "app-server" ? "The turn is starting: you can add an instruction in a moment." :
                                                          "Task is running: send the next instruction when it has finished (the exec backend cannot add to a running turn).") :
      t.worktree_removed ? "The worktree was deleted." :
      !t.codex_thread_id ? "No Codex thread was recorded for this task: use Start New Session." : "";

    // Retry with more reasoning: only ever on click. The suggested step is highlighted after a failed turn.
    const ladder = ["low", "medium", "high"];
    // "default" (Auto) is below both steps; xhigh / max / ultra are above them: never offer a step down.
    const at = ladder.includes(t.reasoning_effort) ? ladder.indexOf(t.reasoning_effort) : t.reasoning_effort === "default" ? -1 : 99;
    const canRetry = idle && t.codex_thread_id && t.adaptive_reasoning !== 0 && !sending;
    for (const [id, effort, rank] of [["#retry-medium-btn", "medium", 1], ["#retry-high-btn", "high", 2]]) {
      const el = $(id);
      el.hidden = !(canRetry && at < rank);
      el.classList.toggle("primary", !!t.retry_suggestion && t.retry_suggestion.effort === effort);
    }
    $("#retry-hint").hidden = $("#retry-medium-btn").hidden && $("#retry-high-btn").hidden;
    $("#retry-hint").textContent = t.retry_suggestion
      ? `The last turn failed. Retry with ${effortLabel(t.retry_suggestion.effort)} sends the instruction below (or the last one) again in the same thread with that effort.`
      : "Retry with … sends the instruction below (or the last one) again in the same thread with more reasoning effort.";

    // Quota
    $("#quota-banner").hidden = t.status !== "waiting-for-quota";
    $("#quota-detail").textContent = t.status_detail ? `(${t.status_detail})` : "";
    $("#quota-retry-btn").disabled = sending;

    // Context guard (warning only) and manual compaction
    const ctx = t.context || {};
    $("#context-guard").hidden = !ctx.warn;
    if (ctx.warn) $("#guard-title").textContent = `Context usage: ${Math.round(ctx.percent)}%`;
    const canCompact = idle && t.codex_thread_id && t.backend === "app-server" && !sending;
    $("#compact-btn").hidden = !(t.backend === "app-server" && t.codex_thread_id);
    document.querySelectorAll(".compact-btn").forEach((b) => (b.disabled = !canCompact));
    renderContext(t);
    renderObserved(t);
    renderDeps(t);
    renderSchedule(t, canSchedule);
    renderRecovery(t);
    if (window.CtxUI) CtxUI.renderTask(t, ctxHandlers);
  }

  // ----- dependencies and recovery -----

  function renderDeps(t) {
    const deps = t.dependencies || [], blocks = t.dependents || [];
    $("#deps-section").hidden = !deps.length && !blocks.length;
    const left = t.deps_total - t.deps_done;
    $("#deps-summary").textContent = deps.length
      ? `(${t.deps_done}/${t.deps_total} complete)` + (t.status === "waiting_dependencies" ? ` · Waiting for ${left} task${left === 1 ? "" : "s"}` : "")
      : "";
    $("#deps-items").innerHTML = deps.map((d) => {
      const [mark, cls] = DEP_MARK[d.status] || (ACTIVE.includes(d.status) || WAITING.includes(d.status) ? ["…", "wait"] : ["⏸", "wait"]);
      return `<li><span class="mark ${cls}">${mark}</span> <a href="/tasks/${esc(d.id)}">${esc(d.name)}</a> <span class="muted">${esc(statusText(d.status))}</span></li>`;
    }).join("");
    $("#blocks-line").hidden = !blocks.length;
    $("#blocks-line").innerHTML = blocks.length ? "Waiting for this task: " + blocks.map((d) => `<a href="/tasks/${esc(d.id)}">${esc(d.name)}</a> (${esc(statusText(d.status))})`).join(", ") : "";
    $("#run-anyway-btn").hidden = !["waiting_dependencies", "blocked"].includes(t.status);
    $("#retry-deps-btn").hidden = t.status !== "blocked";
  }

  // ----- scheduled instructions: a follow-up turn for this task's existing thread, after other tasks complete -----

  const SCHED_LABEL = { waiting_dependencies: "WAITING", waiting_thread: "WAITING FOR THREAD", ready: "READY", running: "RUNNING",
                        completed: "COMPLETED", blocked: "BLOCKED", cancelled: "CANCELLED", failed: "FAILED" };
  const SCHED_FINISHED = ["completed", "cancelled", "failed"];
  const SCHED_CANCELLABLE = ["waiting_dependencies", "waiting_thread", "ready", "blocked"];
  const schedDeps = new Set();   // the tasks ticked in "Depends on" (kept across the re-renders of the list)
  let schedTasks = [];
  let schedTasksAt = 0;
  let schedDepsKey = "";

  function schedItem(r) {
    const deps = (r.dependencies || []).map((d) => {
      const [mark, cls] = DEP_MARK[d.status] || (ACTIVE.includes(d.status) || WAITING.includes(d.status) ? ["…", "wait"] : ["⏸", "wait"]);
      return `<span class="dep"><span class="mark ${cls}">${mark}</span><a href="/tasks/${esc(d.id)}">${esc(d.name)}</a>` +
        `${d.status === "completed" ? "" : ` <span class="muted">${esc(statusText(d.status))}</span>`}</span>`;
    }).join("");
    const note = r.status === "waiting_thread" ? `This thread is busy (${esc(r.wait_note || "")}): it is sent when the thread is idle.`
      : r.status === "running" ? "Sent. Use Stop to stop it; if Codex stops unexpectedly the task's automatic recovery continues the same thread."
      : ["blocked", "failed", "cancelled"].includes(r.status) && r.blocked_reason ? esc(r.blocked_reason) : "";
    const cancel = SCHED_CANCELLABLE.includes(r.status) ? `<button class="sched-cancel" data-sid="${r.id}">Cancel</button>` : "";
    const when = r.finished_at ? ` · ${esc(dt(r.finished_at))}` : r.started_at ? ` · sent ${esc(dt(r.started_at))}` : "";
    return `<li id="scheduled-${r.id}" class="${SCHED_FINISHED.includes(r.status) ? "done" : ""}"><div class="sched-head"><b>#${r.id}</b>` +
      `<span class="status ${esc(r.status)}">${esc(SCHED_LABEL[r.status] || r.status)}</span>` +
      `<span class="muted">Speed: ${esc(r.speed)}${when}</span><span class="spacer"></span>${cancel}</div>` +
      (r.dependencies && r.dependencies.length ? `<div class="sched-deps"><span class="muted">After:</span> ${deps}</div>` : "") +
      `<div class="sched-prompt">“${esc(r.prompt.length > 400 ? r.prompt.slice(0, 400) + "…" : r.prompt)}”</div>` +
      (note ? `<div class="small muted">${note}</div>` : "") + `</li>`;
  }

  function renderSchedule(t, canSchedule) {
    const list = t.scheduled_instructions || [];
    const open = list.filter((r) => !SCHED_FINISHED.includes(r.status));
    const done = list.filter((r) => SCHED_FINISHED.includes(r.status));
    $("#scheduled-section").hidden = !list.length;
    $("#scheduled-summary").textContent = open.length ? `(${open.length} pending${open.some((r) => r.status === "ready") ? ", ready to send" : ""})` : "";
    $("#scheduled-items").innerHTML = open.map(schedItem).join("");
    $("#scheduled-done").hidden = !done.length;
    $("#scheduled-done-summary").textContent = `Finished (${done.length})`;
    $("#scheduled-done-items").innerHTML = done.slice().reverse().map(schedItem).join("");
    $("#schedule-box").hidden = !canSchedule;
    $("#schedule-btn").disabled = sending || !canSchedule;
    $("#schedule-hint").textContent = t.status === "completed" ? "" :
      t.status === "failed" || t.status === "stopped" || t.status === "interrupted" || t.status === "waiting-for-quota" || t.status === "blocked"
        ? `This task is ${statusText(t.status)}: a scheduled instruction waits until it has completed.`
        : "This thread is busy: a scheduled instruction waits for the current turn to finish.";
    const target = (window.location?.hash || '').match(/^#scheduled-(\d+)$/);
    if (target && done.some(r=>String(r.id)===target[1])) $("#scheduled-done").open = true;
    if (target && !document.body.dataset.scheduleLocated) {
      const row = document.getElementById('scheduled-'+target[1]);
      if (row) { row.scrollIntoView({block:'center'}); document.body.dataset.scheduleLocated='1'; }
    }
    renderSchedChoices();
  }

  // The "Depends on" list: every other task. Rebuilt only when it changed, so a tick does not undo what the user is doing.
  function renderSchedChoices() {
    const after = $("#delivery-after").checked;
    $("#sched-deps-box").hidden = !after;
    if (!after) return;
    const tasks = schedTasks.filter((x) => x.id !== id);
    const key = tasks.map((x) => `${x.id}:${x.status}:${x.name}`).join("|");
    if (key === schedDepsKey) return;
    schedDepsKey = key;
    $("#sched-deps").innerHTML = tasks.map((x) =>
      `<label class="check"><input type="checkbox" class="sched-dep" value="${esc(x.id)}" ${schedDeps.has(x.id) ? "checked" : ""}> ${esc(x.name)} ` +
      `<span class="muted">${esc(repoName(x.repository))} · ${esc(taskStatusText(x))}</span></label>`).join("") ||
      `<span class="muted">No other tasks.</span>`;
  }
  async function loadSchedTasks() {
    if (!$("#delivery-after").checked || Date.now() - schedTasksAt < 5000) { renderSchedChoices(); return; }
    schedTasksAt = Date.now();
    try { schedTasks = (await api("GET", "/api/tasks")).tasks || []; } catch (_) { /* the list stays as it was */ }
    renderSchedChoices();
  }
  $("#delivery-idle").addEventListener("change", renderSchedChoices);
  $("#delivery-after").addEventListener("change", () => { schedTasksAt = 0; loadSchedTasks(); });
  $("#sched-deps").addEventListener("change", (ev) => {
    const el = ev.target;
    if (!el || el.type !== "checkbox" || !el.value) return;
    if (el.checked) schedDeps.add(el.value); else schedDeps.delete(el.value);
  });
  $("#scheduled-section").addEventListener("click", (ev) => {
    const b = ev.target && ev.target.closest ? ev.target.closest("button.sched-cancel") : null;
    if (!b) return;
    const sid = b.dataset.sid;
    if (!confirm(`Cancel scheduled instruction #${sid}? It will not be sent.`)) return;
    action("Cancel", async () => { await api("DELETE", `/api/tasks/${id}/scheduled/${sid}`); return `Scheduled instruction #${sid} cancelled`; });
  });
  $("#schedule-btn").addEventListener("click", async () => {
    const prompt = $("#instruction").value;
    if (!prompt.trim()) { setMsg("Write an instruction first.", true); return; }
    const after = $("#delivery-after").checked;
    const deps = after ? [...schedDeps] : [];
    if (after && !deps.length) { setMsg("Select at least one task to wait for, or choose “Send when thread is idle”.", true); return; }
    const fast = $("#sched-speed-fast").checked;
    if (fast && !confirm("Schedule this instruction at Fast speed?\n\nFast costs 2x at API-equivalent prices and consumes your included usage faster. Only this turn is Fast.")) return;
    sending = true;
    renderTask();
    try {
      await action("Schedule Instruction", async () => {
        await api("POST", `/api/tasks/${id}/scheduled`, { prompt, depends_on: deps, service_tier: fast ? "fast" : "standard" });
        $("#instruction").value = "";
        schedDeps.clear();
        schedDepsKey = "";
        return after ? `Scheduled: sent when ${deps.length} task(s) have completed and this thread is idle` : "Scheduled: sent when this thread is idle";
      });
    } finally {
      sending = false;
      renderTask();
    }
  });

  function renderRecovery(t) {
    const rows = [["Auto retry", t.auto_retry_enabled ? "ON" : "OFF"], ["Retries", `${t.retry_count} / ${t.max_retries}`]];
    if (t.last_failure_message) rows.push(["Last failure", `${t.last_failure_message}${t.last_failure_kind ? ` [${t.last_failure_kind}]` : ""}`]);
    if (t.status === "retry_wait") rows.push(["Next retry", `${dt(t.next_retry_at)} (in ${retryIn(t)})`]);
    $("#recovery-summary").textContent = `· auto retry ${t.auto_retry_enabled ? "ON" : "OFF"} · ${t.retry_count}/${t.max_retries}` +
      (t.last_failure_kind ? ` · last failure: ${t.last_failure_kind}` : "");
    $("#recovery-dl").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
    $("#retry-now-btn").hidden = t.status !== "retry_wait";
    $("#retry-btn").hidden = !["failed", "stopped"].includes(t.status);
    $("#auto-retry-btn").textContent = t.auto_retry_enabled ? "Disable Auto Retry" : "Enable Auto Retry";
    $("#recovery-hint").textContent =
      t.status === "retry_wait" ? "The task stopped unexpectedly. It is retried in the same worktree and the same Codex thread, with a short instruction to check the current state first." :
      t.status === "failed" || t.status === "stopped" ? "Retry runs the task again in the same worktree and Codex thread. Use Start New Session for a different thread." :
      "Only unexpected stops are retried. Stop, quota, authentication and configuration problems are not.";
  }

  function renderAttempts(list) {
    $("#attempts").hidden = !list.length;
    $("#attempts-body").innerHTML = list.map((a) => {
      const how = { initial: "first run", instruction: "instruction", new_session: "new session", compact: "compact", auto_retry: "auto retry",
                    manual_retry: "manual retry", restart_recovery: "GUI restart" }[a.trigger_kind] || a.trigger_kind;
      const result = a.result + (a.was_resume ? " (resumed same thread)" : "");
      return `<tr><td>${a.attempt_number}</td><td>${esc(hms(a.started_at))}</td><td>${esc(how)}</td><td>${esc(result)}</td>` +
        `<td title="${esc(a.codex_thread_id || "")}">${esc((a.codex_thread_id || "-").slice(0, 8))}</td>` +
        `<td title="${esc(a.failure_message || "")}">${esc(a.failure_kind ? `${a.failure_kind}: ${(a.failure_message || "").slice(0, 70)}` : "")}</td></tr>`;
    }).join("");
  }
  async function refreshAttempts() {
    try { renderAttempts((await api("GET", `/api/tasks/${id}/attempts`)).attempts); } catch (_) {}
  }

  function renderContext(t) {
    const c = t.context || {};
    const known = c.window != null || c.tokens != null;
    $("#context-box").hidden = !known && !t.codex_thread_id;
    if ($("#context-box").hidden) return;
    $("#context-dl").innerHTML =
      `<dt>Current</dt><dd>${c.tokens == null ? "unknown" : esc(kfmt(c.tokens))}</dd>` +
      `<dt>Model window</dt><dd>${c.window == null ? "unknown" : esc(kfmt(c.window))}</dd>`;
    const bar_ = $("#context-bar");
    bar_.className = "bar " + (c.warn ? "warm" : "ok");
    bar_.firstElementChild.style.width = (c.percent == null ? 0 : Math.min(100, c.percent)) + "%";
    $("#context-note").textContent =
      c.tokens == null ? "Measured from the next turn (Codex reports the context size with each model request)." :
      c.percent == null ? "" : `${c.percent.toFixed(0)}% of the model window`;
  }

  function renderObserved(t) {
    const o = t.observed_quota;
    $("#observed-box").hidden = !o;
    if (!o) return;
    const row = (label, pair) => pair ? `<dt>${label}</dt><dd>${Math.round(pair[0])}% → ${Math.round(pair[1])}%</dd>` : "";
    $("#observed-dl").innerHTML = row("5 hour", o.five_hour) + row("Weekly", o.weekly);
    $("#observed-note").textContent = o.note;
    $("#observed-summary").textContent = "· " + [["5 hour", o.five_hour], ["Weekly", o.weekly]].filter(([, p]) => p)
      .map(([l, p]) => `${l} ${Math.round(p[0])}% → ${Math.round(p[1])}%`).join(", ");
  }

  function renderUsage(u) {
    const l = u.latest;
    const rows = l ? [
      ["Input", num(l.input_tokens)], ["Cached", num(l.cached_input_tokens)],
      ["Uncached", num(l.uncached_input_tokens)], ["Cache hit", pct(l.cache_hit_rate)],
      ["Output", num(l.output_tokens)],
      ...(l.reasoning_output_tokens != null ? [["Reasoning", num(l.reasoning_output_tokens)]] : []),
      ...(l.cache_write_input_tokens ? [["Cache write", num(l.cache_write_input_tokens)]] : []),
    ] : [];
    $("#latest-usage").innerHTML = l
      ? `<div class="muted">Latest turn (${l.turn})</div><dl class="usage">${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`
      : "no completed turn yet";
    $("#usage-summary").textContent = l ? `· input ${num(l.input_tokens)} · cache hit ${pct(l.cache_hit_rate)} · output ${num(l.output_tokens)}` : "";
    $("#efficiency").innerHTML = u.turns.length ? efficiencyHtml(u.efficiency, "task") : "no turn yet";
    const eff = u.turns.length ? u.efficiency : null, saved = eff && eff.total && eff.total.turns ? eff.total.saved_percent : null;
    $("#efficiency-summary").textContent = eff ? `· cache hit ${pct(eff.cache_hit_rate)}` + (saved != null ? ` · saved ${pct(saved)}` : "") : "";
    $("#turns").hidden = !u.turns.length;
    $("#turns-body").innerHTML = u.turns.map((r) =>
      `<tr><td>${r.turn}${r.kind === "compact" ? " (compact)" : ""}${r.status !== "completed" ? ` <span class="muted">${esc(r.status)}</span>` : ""}</td><td>${r.session}</td>` +
      `<td>${num(r.input_tokens)}</td><td>${num(r.cached_input_tokens)}</td>` +
      `<td>${esc(pct(r.cache_hit_rate))}</td><td>${num(r.output_tokens)}</td><td>${num(r.reasoning_output_tokens)}</td></tr>`).join("");
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
      div.className = "entry " + kind + (/\/(agent_message|agentMessage)$/.test(e.type) ? " agent_message" : "") + (e.event ? " has-event" : "");
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

  $("#run-anyway-btn").addEventListener("click", () => {
    if (!confirm("Start this task now, without waiting for its dependencies?")) return;
    action("Run Anyway", async () => { await api("POST", `/api/tasks/${id}/run-anyway`); return "Run Anyway: started"; });
  });
  $("#retry-deps-btn").addEventListener("click", () => action("Retry Failed Dependency", async () => {
    let r;
    try {
      r = await api("POST", `/api/tasks/${id}/retry-dependencies`, {});
    } catch (e) {
      if (e.code !== "over_limit" || !confirm(`${e.message}\n\nRetry anyway?`)) throw e;
      r = await api("POST", `/api/tasks/${id}/retry-dependencies`, { confirm_over_limit: true });
    }
    return `Retried ${r.retried.length} task(s)` + (r.problems.length ? `; skipped: ${r.problems.join("; ")}` : "");
  }));
  $("#retry-now-btn").addEventListener("click", () => action("Retry Now", async () => { await api("POST", `/api/tasks/${id}/retry`, {}); return "Retry Now: started"; }));
  $("#retry-btn").addEventListener("click", () => action("Retry", async () => {
    try {
      await api("POST", `/api/tasks/${id}/retry`, {});
    } catch (e) {
      if (e.code !== "over_limit" || !confirm(`${e.message}\n\nRetry anyway?`)) throw e;
      await api("POST", `/api/tasks/${id}/retry`, { confirm_over_limit: true });
    }
    return "Retry: started (same worktree, same Codex thread)";
  }));
  $("#auto-retry-btn").addEventListener("click", () => {
    const enable = !task.auto_retry_enabled;
    if (!enable && task.status === "retry_wait" && !confirm("Turn off automatic retry? The pending retry is cancelled and the task is marked failed (Retry still works by hand).")) return;
    action(enable ? "Enable Auto Retry" : "Disable Auto Retry", async () => { await api("POST", `/api/tasks/${id}/auto-retry`, { enabled: enable }); });
  });
  $("#max-retries-btn").addEventListener("click", () => {
    const v = prompt("Maximum automatic retries (0-10):", String(task.max_retries));
    if (v === null) return;
    action("Max retries", async () => { await api("POST", `/api/tasks/${id}/auto-retry`, { max_retries: Number(v) }); });
  });
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

  async function sendInstruction({ path = "messages", label = "Send", confirmText = "", effort = null, fallbackToLast = false, tier = null }) {
    let prompt = $("#instruction").value;
    if (!prompt.trim() && fallbackToLast) prompt = task.last_prompt || "";
    if (!prompt.trim()) { setMsg("Write an instruction first.", true); return; }
    if (confirmText && !confirm(confirmText)) return;
    sending = true;
    renderTask();
    try {
      await action(label, async () => {
        const body = { prompt };
        if (effort) body.reasoning_effort = effort;
        if (tier) body.service_tier = tier;  // Send Standard / Send Fast: the speed of this turn only
        await api("POST", `/api/tasks/${id}/${path}`, body);
        $("#instruction").value = "";
        return label + ": started";
      });
    } finally {
      sending = false;
      renderTask();
    }
  }
  $("#send-btn").addEventListener("click", () => sendInstruction(ACTIVE.includes(task.status) ? { label: "Send to running turn" } : { label: "Send Standard", tier: "standard" }));
  $("#send-fast-btn").addEventListener("click", () => sendInstruction({ label: "Send Fast", tier: "fast",
    confirmText: "Send this instruction at Fast speed?\n\nFast costs 2x at API-equivalent prices and consumes your included usage faster. Only this turn is Fast; the next Send is Standard." }));
  // Context Efficiency panel actions (the choices are the user's; the GUI never does any of them on its own)
  const ctxHandlers = {
    zoneAction: async (what) => {
      if (what === "continue") { await action("Continue", async () => { await api("POST", `/api/tasks/${id}/context-ack`); return "Continuing in the same thread (nothing changed)"; }); return; }
      if (what === "compact") { $("#compact-btn").click(); return; }
      $("#instruction").focus();
      setMsg("Write the first instruction of the new session, then press Start New Session.", false);
    },
    changeProfile: async (current) => {
      const next = (prompt(`Tool profile (full / development / minimal). Current: ${current}\n\nChanging tool configuration may reduce prompt cache reuse.`, current) || "").trim().toLowerCase();
      if (!next || next === current) return;
      if (!confirm(`Change the tool profile to "${next}"?\n\nChanging tool configuration may reduce prompt cache reuse.`)) return;
      await action("Change tool profile", async () => { await api("POST", `/api/tasks/${id}/tool-profile`, { profile: next, confirm: true }); return "tool profile changed"; });
    },
  };
  if (window.CtxUI) {
    CtxUI.loadAudit(id);
    $("#ctx-agents-details").addEventListener("toggle", (ev) => { if (ev.target.open) CtxUI.loadAudit(id); });
  }
  $("#retry-medium-btn").addEventListener("click", () => sendInstruction({ label: "Retry with Medium", effort: "medium", fallbackToLast: true }));
  $("#retry-high-btn").addEventListener("click", () => sendInstruction({ label: "Retry with High", effort: "high", fallbackToLast: true }));
  $("#resume-btn").addEventListener("click", () => action("Resume", async () => {
    const r = await api("POST", "/api/tasks/resume-interrupted", { task_ids: [id] });
    if (r.skipped.length) throw new Error(r.skipped[0].reason);
    return "Resume: started";
  }));
  $("#new-session-btn").addEventListener("click", () => sendInstruction({ path: "new-session", label: "Start New Session",
    confirmText: "Start a NEW Codex thread in this worktree?\n\nThe conversation so far is not carried over and the previous thread's cached input is not reused." }));
  $("#quota-retry-btn").addEventListener("click", () => {
    // No thread yet (the first turn never ran): a plain first run. Otherwise the same thread continues.
    $("#instruction").value = task.last_prompt || "";
    sendInstruction({ path: task.codex_thread_id ? "messages" : "new-session", label: "Retry" });
  });
  document.querySelectorAll(".compact-btn").forEach((b) => b.addEventListener("click", () => {
    if (!confirm("Compact this thread?\n\nCodex summarizes the older context to shrink it. The task, worktree, branch and thread stay the same.\n" +
                 "Compaction itself uses some of your included usage.")) return;
    action("Compact", async () => { await api("POST", `/api/tasks/${id}/compact`); return "Compact: started"; });
  }));

  let lastGit = 0;
  let lastUsage = 0;
  let wasActive = true;
  async function tick(forceGit) {
    try {
      task = await api("GET", `/api/tasks/${id}`);
      renderTask();
      await loadSchedTasks();
      await pullLog();
      const active = ACTIVE.includes(task.status);
      const now = Date.now();
      if (forceGit || (active && now - lastUsage > 3000) || (wasActive && !active) || lastUsage === 0) {
        lastUsage = now;
        await refreshUsage();
        await refreshAttempts();
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
    setTimeout(loop, task && BUSY.includes(task.status) ? 1000 : 4000);
  })();
}

// ---------------- AGENTS.md editor ----------------

function initAgents() {
  const scope = document.body.dataset.scope;            // "repository" | "task"
  const repository = document.body.dataset.repository;
  const taskId = document.body.dataset.taskId;
  const text = $("#agents-text");
  let current = { path: "AGENTS.md", sha256: "", exists: false, content: "" };

  const base = () => (scope === "task" ? `/api/tasks/${taskId}/agents-md` : "/api/agents-md");
  const query = (path) => (scope === "task" ? `?path=${encodeURIComponent(path)}` : `?repository=${encodeURIComponent(repository)}&path=${encodeURIComponent(path)}`);
  const dirty = () => text.value !== current.content;

  function msg(textContent, kind) {
    const el = $("#agents-msg");
    el.textContent = textContent || "";
    el.className = kind || "muted";
    el.hidden = !textContent;
  }
  function updateGutter() {
    const n = text.value.split("\n").length;
    $("#gutter").textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
    $("#gutter").scrollTop = text.scrollTop;
    $("#dirty-flag").textContent = dirty() ? "● unsaved changes" : "";
  }

  function renderGit(g) {
    $("#agents-git").hidden = false;
    const label = { untracked: "Untracked", modified: "Modified", added: "Added", deleted: "Deleted", clean: "Clean (no changes)",
                    missing: "Does not exist", ignored: "Ignored by git", conflict: "Conflict" }[g.state] || g.state;
    $("#git-state").textContent = (g.short ? g.short + "\n" : "") + label;
    $("#agents-diff").hidden = true;
  }

  async function load(path) {
    msg("");
    try {
      const r = await api("GET", base() + query(path));
      current = { path: r.path, sha256: r.sha256, exists: r.exists, content: r.content };
      $("#agents-root").textContent = r.root;
      const files = r.files.includes("AGENTS.md") || !r.exists ? r.files : ["AGENTS.md", ...r.files];
      $("#file-pick-wrap").hidden = files.length < 2;
      $("#file-pick").innerHTML = files.map((f) => `<option value="${esc(f)}" ${f === r.path ? "selected" : ""}>${esc(f)}</option>`).join("");
      $("#agents-missing").hidden = r.exists;
      $("#agents-editor").hidden = !r.exists;
      if (r.exists) text.value = r.content;
      $("#agents-missing p b").textContent = `${r.path} does not exist`;
      $("#create-btn").textContent = `Create ${r.path}`;
      renderGit(r.git);
      updateGutter();
    } catch (e) {
      $("#agents-missing").hidden = $("#agents-editor").hidden = $("#agents-git").hidden = true;
      msg(e.message, "error");
    }
  }

  async function save() {
    try {
      const body = { content: text.value, path: current.path, expected_sha: current.sha256 };
      if (scope === "repository") body.repository = repository;
      const r = await api("PUT", base(), body);
      msg(r.changed ? `Saved ${current.path}` : `No changes: ${current.path} was not written`, "ok");
      current.sha256 = r.sha256;
      current.exists = true;
      current.content = text.value;
      $("#agents-missing").hidden = true;
      $("#agents-editor").hidden = false;
      const g = await api("GET", base() + query(current.path));
      renderGit(g.git);
      updateGutter();
    } catch (e) {
      msg((e.code === "changed" ? "Not saved: " : "Save failed: ") + e.message, "error");
    }
  }

  text.addEventListener("input", updateGutter);
  text.addEventListener("scroll", () => { $("#gutter").scrollTop = text.scrollTop; });
  text.addEventListener("keydown", (ev) => {
    if (ev.key === "Tab" && !ev.shiftKey && !ev.ctrlKey && !ev.altKey) {
      ev.preventDefault();
      const { selectionStart: a, selectionEnd: b } = text;
      text.setRangeText("\t", a, b, "end");
      updateGutter();
    }
  });
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
      ev.preventDefault();
      if (!$("#agents-editor").hidden) save();
    }
  });
  window.addEventListener("beforeunload", (ev) => { if (dirty()) { ev.preventDefault(); ev.returnValue = ""; } });

  $("#save-btn").addEventListener("click", save);
  $("#reload-btn").addEventListener("click", () => {
    if (dirty() && !confirm("Discard your unsaved changes and reload the file from disk?")) return;
    load(current.path);
  });
  $("#create-btn").addEventListener("click", () => {
    current.sha256 = "";  // "I expect it to be absent": a file that appeared meanwhile is not overwritten
    current.content = "";
    $("#agents-missing").hidden = true;
    $("#agents-editor").hidden = false;
    text.value = "# Project instructions\n\n";
    text.focus();
    updateGutter();
    msg("Not created yet: press Save to write the file.", "muted");
  });
  $("#file-pick").addEventListener("change", (ev) => {
    if (dirty() && !confirm("Discard your unsaved changes?")) { ev.target.value = current.path; return; }
    load(ev.target.value);
  });
  $("#diff-btn").addEventListener("click", async () => {
    const pre = $("#agents-diff");
    if (!pre.hidden) { pre.hidden = true; return; }
    try {
      const d = await api("GET", base() + "/diff" + query(current.path));
      pre.innerHTML = d.diff ? colorDiffText(d.diff) : esc(d.state === "missing" ? "(file does not exist)" : "(no changes)");
      pre.hidden = false;
    } catch (e) {
      msg(e.message, "error");
    }
  });

  load(new URLSearchParams(location.search).get("path") || "AGENTS.md");  // the health check's Edit button names the file
}

function colorDiffText(text) {
  return text.split("\n").map((l) => {
    const e = esc(l);
    if (l.startsWith("diff --git")) return `<span class="file">${e}</span>`;
    if (l.startsWith("@@")) return `<span class="hunk">${e}</span>`;
    if (l.startsWith("+") && !l.startsWith("+++")) return `<span class="add">${e}</span>`;
    if (l.startsWith("-") && !l.startsWith("---")) return `<span class="del">${e}</span>`;
    return e;
  }).join("\n");
}

const page = document.body.dataset.page;
if (page === "dashboard") initDashboard();
else if (page === "task") initTask();
else if (page === "agents") initAgents();
