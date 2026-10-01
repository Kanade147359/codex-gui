// Context Efficiency UI: New Task settings + preview, the Task Detail panel, and the thresholds dialog.
// Loaded before app.js; app.js calls CtxUI.* at a few well-defined points. Everything here only DISPLAYS or sends the
// user's explicit choices: nothing compacts, retries, edits an AGENTS.md or changes a setting by itself.
"use strict";

const CtxUI = (() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => (n == null ? "-" : Number(n).toLocaleString("en-US"));
  const kfmt = (n) => (n == null ? "-" : n >= 1000 ? Math.round(n / 1000) + "k" : String(n));
  const pct = (r) => (r == null ? "-" : r.toFixed(r >= 99.95 || r === 0 ? 0 : 1) + "%");
  async function api(method, url, body) {
    const res = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      const d = data && data.detail;
      const err = new Error(!d ? "request failed" : typeof d === "string" ? d : d.message || JSON.stringify(d));
      err.code = d && d.code;
      throw err;
    }
    return data;
  }
  const level = (l) => ({ ok: "ok", warning: "warm", critical: "hot" }[l] || "ok");
  const bar = (percent, lvl) => `<div class="bar ${lvl || "ok"}"><div class="fill" style="width:${Math.max(0, Math.min(100, percent || 0))}%"></div></div>`;
  const TIER_LABEL = (t) => (t === "priority" ? "Fast" : t === "default" || !t ? "Standard" : t);

  // ------------------------------------------------------------ AGENTS.md health (read-only)

  // `editHref(file)` returns where the Edit button goes (the GUI's own editor), or null when the GUI cannot edit that file.
  function agentsHealthHtml(a, editHref) {
    if (!a) return `<p class="muted">AGENTS.md chain not available.</p>`;
    const b = a.budget;
    const tag = b.level === "critical" ? `<span class="error"><b>CRITICAL</b></span>` : b.level === "warning" ? `<span class="warn"><b>WARNING</b></span>` : `<span class="ok">OK</span>`;
    const rows = a.files.map((f) => {
      const href = editHref && f.name === "AGENTS.md" ? editHref(f) : null;  // the GUI editor only opens files named AGENTS.md
      const status = f.scope === "global" ? "global (outside the budget)" : f.status === "included" ? "read in full" : f.status === "truncated" ? `<span class="warn">cut at ${num(f.included_bytes)} bytes</span>` : `<span class="error">left out</span>`;
      return `<tr><td class="wrap" title="${esc(f.path)}">${esc(f.scope === "global" ? "~/.codex/" + f.name : f.relative)}</td>
        <td>${num(f.bytes)}</td><td>~${num(f.tokens_est)}</td><td>${f.cumulative_bytes == null ? "-" : num(f.cumulative_bytes)}</td><td>${status}</td>
        <td>${href ? `<a class="button" href="${esc(href)}" target="_blank">Edit</a>` : `<span class="muted small" title="${esc(f.path)}">edit outside the GUI</span>`}</td></tr>`;
    }).join("");
    const warnings = a.warnings.map((w) => `<li class="warn">${esc(w.path.split("/").slice(-2).join("/"))}${w.line ? ":" + w.line : ""} — ${esc(w.message)}${w.excerpt ? ` <span class="muted">“${esc(w.excerpt)}”</span>` : ""}</li>`).join("");
    const dups = a.duplicates.map((d) => `<li class="warn">${d.kind === "global_project" ? "Global and project AGENTS.md overlap" : "Root and nested AGENTS.md overlap"}: ${d.shared_lines} shared lines (${d.share_percent}% of the shorter file) — ${esc(d.a.split("/").slice(-2).join("/"))} / ${esc(d.b.split("/").slice(-2).join("/"))}
        <span class="muted">e.g. “${esc(d.examples[0] || "")}”</span></li>`).join("");
    return `<div class="agents-health">
      <p>Project instructions: <b>${num(b.project_bytes)}</b> / ${num(b.max_bytes)} bytes (${b.used_percent}%, <span class="muted">project_doc_max_bytes from ${esc(b.source)}</span>) ${tag}</p>
      ${bar(b.used_percent, level(b.level))}
      ${a.truncation ? `<p class="error small">${esc(a.truncation)}</p>` : ""}
      <table class="usage"><thead><tr><th>FILE</th><th>BYTES</th><th>TOKENS</th><th>CUMULATIVE</th><th>STATUS</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="6" class="muted">no AGENTS.md is read for this directory</td></tr>`}</tbody></table>
      <p class="small muted">Chain for <code>${esc(a.cwd)}</code> (project root <code>${esc(a.project_root)}</code>). ~${num(a.total_tokens_est)} tokens in total (estimate). The GUI never edits these files: this check only warns.</p>
      ${warnings || dups ? `<ul class="small">${warnings}${dups}</ul>` : ""}</div>`;
  }

  // ------------------------------------------------------------ New Task: settings + preview

  const newTask = { timer: null, repo: "", opts: null };

  function initNewTask(options) {
    newTask.opts = options.context_efficiency || null;
    const o = newTask.opts;
    if (!o) return;
    const sel = (id, items, valueKey) => {
      const el = $(id);
      if (!el) return;
      const keep = el.value;
      el.innerHTML = items.map((i) => {
        const v = i[valueKey];
        const label = i.label + (v ? ` — ${num(v)}` : "");
        return `<option value="${esc(i.id)}">${esc(label)}</option>`;
      }).join("");
      el.value = [...el.options].some((x) => x.value === keep) ? keep : o["default_" + id.slice(1).replace("ctx-", "").replace("-", "_")] || el.options[0].value;
    };
    sel("#ctx-tool-output", o.tool_output, "limit");
    sel("#ctx-skills", o.skills, "budget");
    const tp = $("#ctx-tool-profile");
    if (tp) { const keep = tp.value; tp.innerHTML = o.tool_profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join(""); tp.value = keep || o.default_tool_profile; }
    showNotes();
  }

  function formValues() {
    const get = (id, dflt) => ($(id) ? $(id).value : dflt);
    return {
      tool_output: get("#ctx-tool-output", "default"), skills: get("#ctx-skills", "default"),
      tool_profile: get("#ctx-tool-profile", "full"), allow_subagents: $("#ctx-subagents") ? $("#ctx-subagents").checked : false,
      cwd_subdir: get("#ctx-cwd", ""),
    };
  }

  function showNotes() {
    const o = newTask.opts;
    const out = $("#ctx-tool-output-note");
    if (out && o) {
      const it = o.tool_output.find((x) => x.id === $("#ctx-tool-output").value);
      out.textContent = !it || !it.limit ? "Codex's own cap applies." :
        `GUI preset (${num(it.limit)} tokens). Measured with codex 0.159.2: this only LOWERS the cap; presets at or above the model's own cap (10,000 for GPT-6.1 Sol) change nothing.`;
    }
    const sk = $("#ctx-skills-note");
    if (sk) sk.textContent = "Budget of the skills CATALOG (names + descriptions in the prompt), not of the skill bodies.";
    const prof = $("#ctx-profile-note");
    if (prof) prof.textContent = { full: "Codex as configured: ChatGPT apps/connectors, plugins and all your MCP servers.",
      development: "No ChatGPT apps/connectors and no plugins; your own MCP servers stay.",
      minimal: "Built-in tools only: apps, plugins and every MCP server are turned off." }[$("#ctx-tool-profile").value] || "";
  }

  async function verifyProfile() {
    const repo = ($("#new-task-form").elements.repository.value || "").trim();
    const box = $("#ctx-verify");
    const profile = $("#ctx-tool-profile").value;
    if (!repo) { box.innerHTML = `<span class="muted">choose a repository first</span>`; return; }
    box.innerHTML = `<span class="muted">measuring with the real Codex (no model is called)…</span>`;
    try {
      const r = await api("POST", "/api/tool-profiles/verify", { repository: repo, profile });
      box.innerHTML = verifyHtml(r);
    } catch (e) { box.innerHTML = `<span class="error">${esc(e.message)}</span>`; }
  }

  function verifyHtml(r) {
    if (!r) return `<span class="muted">not verified</span>`;
    if (!r.ok) return `<span class="error">could not measure: ${esc(r.error || "unknown")}</span> — not marked optimized`;
    if (r.profile === "full") return `<span class="muted">Codex default: ${num(r.full_count)} tools reachable (nested), declarations ${num(r.full_bytes)} bytes</span>`;
    const saved = r.full_bytes - r.profile_bytes;
    return r.verified
      ? `<span class="ok"><b>Verified:</b> ${num(r.full_count)} → ${num(r.profile_count)} tools the model can reach (−${num(r.full_count - r.profile_count)}); declarations −${num(saved)} bytes (~${num(r.tokens_saved_est)} tokens, cached prefix)</span>`
      : `<span class="warn">${esc(r.note || "No reduction was measured")} (${num(r.full_count)} → ${num(r.profile_count)})</span>`;
  }

  async function refreshPreview() {
    const box = $("#ctx-preview");
    if (!box) return;
    const repo = ($("#new-task-form").elements.repository.value || "").trim();
    if (!repo) { box.innerHTML = ""; return; }
    const sub = ($("#ctx-cwd") || {}).value || "";
    const skills = ($("#ctx-skills") || {}).value || "default";
    const budget = (newTask.opts && newTask.opts.skills.find((x) => x.id === skills) || {}).budget;
    box.innerHTML = `<p class="muted small">checking AGENTS.md and skills…</p>`;
    try {
      const q = `repository=${encodeURIComponent(repo)}&cwd_subdir=${encodeURIComponent(sub)}&skills=${encodeURIComponent(skills)}` + (budget ? `&skills_budget=${budget}` : "");
      const p = await api("GET", `/api/context/preview?${q}`);
      if (($("#new-task-form").elements.repository.value || "").trim() !== repo) return;
      box.innerHTML = previewHtml(p, budget);
    } catch (e) { box.innerHTML = `<p class="error small">${esc(e.message)}</p>`; }
  }

  function previewHtml(p, budget) {
    const sk = p.skills, dflt = p.default_skills;
    let skills = `<p class="muted small">Skills: not reported.</p>`;
    if (sk) {
      const binding = budget && dflt && sk.tokens_est < dflt.tokens_est;
      skills = `<p class="small">Skills catalog: <b>${sk.skills}</b> skills, ~${num(sk.tokens_est)} tokens of catalog metadata
        <span class="muted">(names + descriptions only, not skill bodies)</span>${budget ? binding ? ` — <span class="ok">budget ${num(budget)} saves ~${num(dflt.tokens_est - sk.tokens_est)} tokens vs Codex default (${num(dflt.tokens_est)})</span>`
          : dflt ? ` — <span class="warn">budget ${num(budget)} does not reduce the catalog (Codex default: ~${num(dflt.tokens_est)} tokens)${sk.tokens_est > dflt.tokens_est ? "; it would even enlarge it" : ""}</span>` : "" : ""}</p>`;
    }
    const mcp = p.mcp_servers.length ? p.mcp_servers.map(esc).join(", ") : "none configured";
    return `<h3>What the model starts with</h3>
      ${agentsHealthHtml(p.agents, (f) => (f.scope === "project" && f.relative && f.path.startsWith(p.repository + "/") ? `/agents?repository=${encodeURIComponent(p.repository)}&path=${encodeURIComponent(f.relative)}` : null))}
      ${skills}
      <p class="small muted">Your MCP servers: ${mcp}. Model tool-output cap: ${p.tool_output_cap ? num(p.tool_output_cap) + " tokens" : "unknown"}. ${p.config_available ? "" : "Codex's effective config could not be read; defaults are assumed."}</p>`;
  }

  function bindNewTask() {
    const form = $("#new-task-form");
    if (!form || !$("#ctx-tool-output")) return;
    const debounced = () => { clearTimeout(newTask.timer); newTask.timer = setTimeout(refreshPreview, 500); };
    ["#ctx-tool-output", "#ctx-skills", "#ctx-tool-profile"].forEach((id) => $(id).addEventListener("change", () => { showNotes(); if (id === "#ctx-skills") debounced(); if (id === "#ctx-tool-profile") $("#ctx-verify").innerHTML = ""; }));
    $("#ctx-cwd").addEventListener("input", debounced);
    form.elements.repository.addEventListener("change", debounced);
    form.elements.repository.addEventListener("input", debounced);
    $("#ctx-verify-btn").addEventListener("click", verifyProfile);
    const adv = $("#ctx-advanced");
    if (adv) adv.addEventListener("toggle", () => { if (adv.open) refreshPreview(); });
  }

  // ------------------------------------------------------------ Task Detail panel

  function zoneBanner(ctx, task) {
    const z = ctx.context_zone;
    if (!z || !["warning", "strong", "long"].includes(z.zone) || z.acknowledged) return "";
    const cls = z.zone === "long" ? "error" : "warn";
    return `<div class="banner guard ${z.zone === "long" ? "quota" : ""}"><b class="${cls}">${z.zone === "long" ? "LONG CONTEXT" : z.zone === "strong" ? "Strong warning" : "Warning"}: ${esc(z.message)}</b>
      <p class="small">Context ${num(z.tokens)} tokens (the latest request, not the thread's accumulated usage). Thresholds ${num(z.warn_at)} / ${num(z.strong_at)} / ${num(z.threshold)}. Nothing is compacted automatically.</p>
      ${z.note ? `<p class="small muted">${esc(z.note)}</p>` : ""}
      <div class="inline-actions"><button data-ctx-act="continue">Continue</button><button data-ctx-act="compact">Compact</button><button data-ctx-act="new_session">Start New Session in Same Worktree</button></div></div>`;
  }

  function stopBanner(ctx) {
    const s = ctx.stop;
    if (!s) return "";
    return `<div class="banner quota"><b>Stopped by the retry guard.</b> ${esc(s.message)}
      <p class="small">Nothing is retried automatically${s.blocks_resend ? "; sending the same thread another instruction is blocked until the context is smaller" : ""}.</p>
      ${s.blocks_resend ? `<div class="inline-actions"><button data-ctx-act="compact">Compact</button><button data-ctx-act="new_session">Start New Session in Same Worktree</button></div>` : ""}</div>`;
  }

  function settingsHtml(ctx, task) {
    const s = ctx.settings, to = s.tool_output, tp = s.tool_profile;
    const chk = tp.check;
    const verified = tp.name === "full" ? `<span class="muted">Codex default</span>` : chk ? verifyHtml(chk) : `<span class="muted">not verified (measuring…)</span>`;
    return `<dl class="usage">
      <dt>Tool output limit</dt><dd>${esc(to.label)}${to.limit ? ` — ${num(to.limit)} tokens (GUI preset)` : ""} <span class="${to.effective ? "ok" : "muted"}">${esc(to.note)}</span></dd>
      <dt>Skills catalog budget</dt><dd>${esc(s.skills.label)}${s.skills.budget ? ` — ${num(s.skills.budget)} tokens (catalog metadata, not skill bodies)` : ""}</dd>
      <dt>Nested agents</dt><dd>${s.allow_subagents ? "allowed" : "OFF (Codex cannot spawn sub-agents in this task)"}</dd>
      <dt>Tool profile</dt><dd>${esc(tp.label)} <span class="muted">(frozen for the thread)</span> — ${verified}
        <button id="ctx-profile-btn" class="small-btn">Change…</button></dd>
      <dt>Working directory</dt><dd><code>${esc(s.cwd)}</code>${s.cwd_subdir ? ` <span class="muted">(custom: ${esc(s.cwd_subdir)})</span>` : ` <span class="muted">(repository root of the worktree)</span>`}</dd></dl>`;
  }

  function cacheTable(ctx) {
    const rows = ctx.turn_series.map((t) => `<tr><td>${t.turn}</td><td>${esc(TIER_LABEL(t.service_tier))}</td><td>${num(t.input)}</td><td>${num(t.cache_read)}</td><td>${num(t.cache_write)}</td>
      <td>${num(t.uncached)}</td><td>${num(t.output)}</td><td>${esc(pct(t.hit_rate))}</td><td>${t.requests == null ? "-" : t.requests}${t.max_request_input ? ` <span class="muted">max ${kfmt(t.max_request_input)}</span>` : ""}</td>
      <td>${t.tool_calls == null ? "-" : t.tool_calls}${t.large_tool_outputs ? ` <span class="warn">${t.large_tool_outputs} large</span>` : ""}</td></tr>`).join("");
    return `<table class="usage"><thead><tr><th>TURN</th><th>SPEED</th><th>INPUT</th><th>CACHE READ</th><th>CACHE WRITE</th><th>UNCACHED</th><th>OUTPUT</th><th>HIT</th><th>REQUESTS</th><th>TOOL CALLS</th></tr></thead><tbody>${rows || `<tr><td colspan="10" class="muted">no turn yet</td></tr>`}</tbody></table>
      <p class="small muted">Uncached = input − cache read (cache write tokens are part of it). SPEED is what was requested for that turn.</p>`;
  }

  function eventsHtml(ctx) {
    const miss = ctx.cache_misses.slice().reverse().map((e) => {
      const d = e.data || {};
      return `<li class="${e.severity === "info" ? "muted" : "warn"}">Turn ${e.turn}: ${esc((e.message || "").split(". Possible cause")[0])}<br>
        <span class="small muted">Possible cause${(d.possible_causes || []).length > 1 ? "s" : ""}: ${(d.possible_causes || []).map(esc).join("; ")}</span></li>`;
    }).join("");
    const others = ctx.events.filter((e) => ["context_jump", "frequent_compaction", "long_context", "retry_guard", "tool_profile_change"].includes(e.kind)).slice(-12).reverse()
      .map((e) => `<li class="${e.severity === "critical" ? "error" : "warn"}">${esc(e.kind.replace(/_/g, " "))}: ${esc(e.message)}</li>`).join("");
    return `${miss ? `<h3>Cache misses</h3><ul class="small">${miss}</ul>` : ""}${others ? `<h3>Warnings</h3><ul class="small">${others}</ul>` : ""}`;
  }

  function toolOutputsHtml(ctx) {
    const rows = ctx.large_tool_outputs.slice().reverse().map((e) => {
      const d = e.data || {};
      return `<tr><td>${e.turn ?? "-"}</td><td class="wrap">${esc(d.label)}</td><td>${num(d.raw_tokens_est)}</td><td>${num(d.model_tokens_est)}${d.truncated_for_model ? ` <span class="muted">(cut for the model)</span>` : ""}</td>
        <td>${d.size === "large" ? `<span class="warn">≥ large</span>` : "≥ warn"}</td></tr>`;
    }).join("");
    return rows ? `<h3>Large tool outputs</h3><table class="usage"><thead><tr><th>TURN</th><th>COMMAND / TOOL</th><th>RAW ~TOKENS</th><th>SEEN BY MODEL ~TOKENS</th><th></th></tr></thead><tbody>${rows}</tbody></table>
      <p class="small muted">Estimates (UTF-8 bytes / 4). Thresholds are configurable (Efficiency settings on the dashboard).</p>` : "";
  }

  let host = null, handlers = null, lastKey = "";

  function renderTask(task, h) {
    host = host || $("#ctx-panel");
    if (!host || !task.ctx) return;
    handlers = h;
    const ctx = task.ctx;
    const key = JSON.stringify([ctx, task.status]);
    if (key === lastKey) return;  // nothing changed: keep the user's scroll / open <details>
    lastKey = key;
    const age = ctx.cache_age, comp = ctx.compaction, z = ctx.context_zone;
    const pctUsed = task.context && task.context.percent != null ? Math.round(task.context.percent) + "%" : "-";
    $("#ctx-banners").innerHTML = stopBanner(ctx) + zoneBanner(ctx, task);
    $("#ctx-summary").innerHTML = `<dl class="usage">
      <dt>Context</dt><dd>${pctUsed}${z.tokens != null ? ` <span class="muted">(${num(z.tokens)} tokens in the latest request)</span>` : ""} ${z.zone && !["n/a", "normal", "unknown"].includes(z.zone) ? `<b class="${z.zone === "long" ? "error" : "warn"}">${esc(z.zone.toUpperCase())}</b>` : ""}</dd>
      <dt>Compactions</dt><dd>${comp.count}${comp.frequent ? ` <span class="warn">${esc(comp.warning)}</span>` : ""}</dd>
      <dt>Cache age</dt><dd><b class="cache-${esc(age.state)}">${esc(age.label)}</b> <span class="muted small">reference only; the prompt cache is kept for at least 30 minutes. The GUI never sends a prompt just to keep it warm.</span></dd></dl>`;
    $("#ctx-settings").innerHTML = settingsHtml(ctx, task);
    $("#ctx-cache").innerHTML = cacheTable(ctx) + eventsHtml(ctx);
    $("#ctx-tooloutputs").innerHTML = toolOutputsHtml(ctx);
    host.querySelectorAll("[data-ctx-act]").forEach((b) => b.addEventListener("click", () => h.zoneAction(b.dataset.ctxAct)));
    const pb = $("#ctx-profile-btn");
    if (pb) pb.addEventListener("click", () => h.changeProfile(ctx.settings.tool_profile.name));
  }

  async function loadAudit(taskId) {
    const box = $("#ctx-agents");
    if (!box) return;
    try {
      const a = await api("GET", `/api/tasks/${taskId}/agents-audit`);
      box.innerHTML = agentsHealthHtml(a, (f) => (f.scope === "project" ? `/tasks/${taskId}/agents?path=${encodeURIComponent(f.relative)}` : null));
    } catch (e) { box.innerHTML = `<p class="muted small">${esc(e.message)}</p>`; }
  }

  // ------------------------------------------------------------ thresholds dialog (dashboard)

  async function initThresholds() {
    const btn = $("#ctx-settings-btn"), dlg = $("#ctx-settings-dialog");
    if (!btn || !dlg) return;
    const LABELS = {
      cache_miss_uncached_tokens: "Cache miss: uncached input tokens above", cache_miss_drop_points: "Cache miss: hit-rate drop vs recent average (points)",
      cache_miss_min_input: "Cache miss: ignore turns with input below", cache_recent_turns: "Recent average: number of turns",
      tool_output_warn_tokens: "Tool output: list when above (tokens)", tool_output_large_tokens: "Tool output: mark large above (tokens)",
      context_jump_tokens: "Context jump after a large output (tokens)", frequent_compaction_count: "Frequent compaction: compactions",
      frequent_compaction_minutes: "Frequent compaction: within (minutes)", cache_hot_minutes: "Cache age: HOT up to (minutes)",
      cache_warm_minutes: "Cache age: WARM up to (minutes), then COLD", idle_cause_minutes: "Idle gap listed as a possible cause (minutes)",
      repeat_failure_limit: "Retry guard: identical failures in a row before the turn is stopped",
    };
    const load = async () => {
      const s = await api("GET", "/api/context/settings");
      $("#ctx-settings-form").innerHTML = Object.keys(s.values).map((k) => `<label>${esc(LABELS[k] || k)}
        <input type="number" name="${esc(k)}" value="${s.values[k]}" min="${s.bounds[k][0]}" max="${s.bounds[k][1]}"> <span class="muted small">default ${num(s.defaults[k])}</span></label>`).join("");
    };
    btn.addEventListener("click", async () => { $("#ctx-settings-msg").textContent = ""; try { await load(); dlg.showModal(); } catch (e) { alert(e.message); } });
    $("#ctx-settings-close").addEventListener("click", () => dlg.close());
    $("#ctx-settings-reset").addEventListener("click", async () => { await api("DELETE", "/api/context/settings"); await load(); $("#ctx-settings-msg").textContent = "Reset to defaults."; });
    $("#ctx-settings-save").addEventListener("click", async () => {
      const values = {};
      new FormData($("#ctx-settings-form")).forEach((v, k) => { values[k] = parseInt(v, 10); });
      try { await api("PUT", "/api/context/settings", { values }); $("#ctx-settings-msg").textContent = "Saved."; }
      catch (e) { $("#ctx-settings-msg").textContent = e.message; }
    });
  }

  return { agentsHealthHtml, initNewTask, formValues, bindNewTask, refreshPreview, renderTask, loadAudit, initThresholds, verifyHtml };
})();
window.CtxUI = CtxUI;  // a top-level const is not a window property: app.js looks for window.CtxUI
