/* Task files and immutable artifact transfers; no file contents enter prompts. */
"use strict";
window.TaskFileUI = (() => {
  const escape = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const size = n => n < 1024 ? `${n} B` : n < 1024*1024 ? `${(n/1024).toFixed(1)} KiB` : `${(n/1024/1024).toFixed(1)} MiB`;
  async function request(url, body) {
    const response = await fetch(url, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    if (!response.ok) {
      let data; try { data = await response.json(); } catch (_) {}
      throw new Error(data?.detail?.message || (typeof data?.detail === "string" ? data.detail : `Request failed (${response.status})`));
    }
    return response;
  }
  const json = async (url, body) => (await request(url, body)).json();
  function error(host, message) {
    let target = host.querySelector(".file-action-error");
    if (!target) { target = document.createElement("p"); target.className = "attachment-error file-action-error"; target.setAttribute("role", "alert"); host.appendChild(target); }
    target.textContent = message;
  }
  async function download(url, name, host, body) {
    try {
      const response = await request(url, body);
      const blob = await response.blob();
      const object = URL.createObjectURL(blob), a = document.createElement("a");
      a.href = object; a.download = name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(object), 30000);
      error(host, "");
    } catch (e) { error(host, "Download failed: " + e.message); }
  }
  function modal(title, html) {
    const dialog = document.createElement("dialog"); dialog.className = "file-dialog";
    dialog.innerHTML = `<div class="title-row"><h2>${escape(title)}</h2><button type="button" data-close>Cancel</button></div>${html}<p role="alert" class="attachment-error dialog-error"></p>`;
    document.body.appendChild(dialog);
    dialog.querySelector("[data-close]").onclick = () => dialog.close();
    dialog.addEventListener("close", () => dialog.remove());
    dialog.showModal(); return dialog;
  }
  function fileRows(files, checkbox = true) {
    return files.map(f => `<article class="task-file-row" data-path="${escape(f.path)}">
      ${checkbox ? `<input type="checkbox" class="file-check" value="${escape(f.path)}" aria-label="Select ${escape(f.path)}">` : ""}
      <div class="file-info"><strong>${escape(f.filename)}</strong><span>${escape(f.path)}</span><span class="small muted">${size(f.size)} · ${escape(f.modified_at)} · ${f.declared ? "Declared artifact" : f.changed ? "Changed since base" : "Source"} · ${escape(f.category)}</span></div>
      <div class="file-row-actions"><button type="button" data-download>Download</button>${f.preview ? '<button type="button" data-preview>Preview</button>' : ""}<button type="button" data-send>Send to Task</button></div></article>`).join("");
  }
  async function pickArtifacts(targetId) {
    const tasks = (await json("/api/tasks")).tasks;
    const dialog = modal("Attach from Task", `<label>Source Task<select class="artifact-source"><option value="">Select a task</option>${tasks.filter(t=>t.id!==targetId).map(t=>`<option value="${escape(t.id)}">${escape(t.name)} · ${escape(t.status)} · ${escape(t.task_outcome || "")}</option>`).join("")}</select></label>
      <form class="artifact-search file-toolbar"><label>Relative path<input class="artifact-path" placeholder="reports/ or verification.json"></label><button>Find</button></form>
      <label class="check"><input type="checkbox" class="artifact-show-source"> Show source files too</label>
      ${!targetId ? '<label class="check"><input type="checkbox" class="artifact-defer"> Wait for source success (adds a dependency; snapshot at execution)</label><p class="small muted">For a future file, enter its relative path above and choose Wait for source success.</p>' : ""}
      <div class="artifact-options task-file-list"></div><button type="button" class="artifact-more" hidden>Load more</button><button type="button" class="primary artifact-attach">Attach selected files</button>`);
    const source = dialog.querySelector(".artifact-source"), list = dialog.querySelector(".artifact-options"), message = dialog.querySelector(".dialog-error");
    let candidates = [], snapshots = [], nextOffset = null;
    function render() {
      const showSource = dialog.querySelector(".artifact-show-source").checked;
      list.innerHTML = candidates.filter(f => showSource || f.category === "artifact").map(f => `<label class="artifact-option"><input type="checkbox" data-current value="${escape(f.path)}"><span>${escape(f.path)} · ${size(f.size)} · ${escape(f.category)}</span></label>`).join("") +
        snapshots.map(a => `<label class="artifact-option"><input type="checkbox" data-snapshot value="${escape(a.id)}"><span>Snapshot: ${escape(a.relative_path)} · ${size(a.size)} · ${escape(a.sha256.slice(0,12))}</span></label>`).join("");
      if (!list.children.length) list.textContent = "No artifact candidates. Enter a path, show source files, or use an existing snapshot.";
    }
    async function load(more = false) {
      if (!source.value) return;
      try {
        const path = dialog.querySelector(".artifact-path").value.trim();
        const data = await json(`/api/tasks/${encodeURIComponent(source.value)}/artifacts?path=${encodeURIComponent(path)}&offset=${more ? nextOffset || 0 : 0}`);
        candidates = more ? [...candidates, ...data.files] : data.files; snapshots = data.snapshots; nextOffset = data.next_offset;
        dialog.querySelector(".artifact-more").hidden = nextOffset == null;
        message.textContent = data.truncated ? "Scan limit reached; narrow the relative path to find more files." : "";
        render();
      } catch (e) { message.textContent = e.message; candidates = []; render(); }
    }
    source.onchange = () => load();
    dialog.querySelector(".artifact-search").onsubmit = ev => { ev.preventDefault(); load(); };
    dialog.querySelector(".artifact-more").onclick = () => load(true);
    dialog.querySelector(".artifact-show-source").onchange = render;
    return new Promise(resolve => {
      let completed = false;
      dialog.addEventListener("close", () => { if (!completed) resolve([]); });
      dialog.querySelector(".artifact-attach").onclick = async ev => {
        const button = ev.currentTarget; button.disabled = true;
        try {
          if (!source.value) throw new Error("Select a source task.");
          const paths = [...list.querySelectorAll("[data-current]:checked")].map(i=>i.value);
          const existing = [...list.querySelectorAll("[data-snapshot]:checked")].map(i=>i.value);
          let inputs;
          if (dialog.querySelector(".artifact-defer")?.checked) {
            if (existing.length) throw new Error("Use current paths for deferred transfer; uncheck Wait to attach snapshots now.");
            if (!paths.length && dialog.querySelector(".artifact-path").value.trim()) paths.push(dialog.querySelector(".artifact-path").value.trim());
            inputs = paths.map(path => ({kind:"reference", filename:path, ref:{source_task_id:source.value, path}, size:0}));
          } else {
            const registered = paths.length ? (await json(`/api/tasks/${source.value}/artifacts`, {paths})).artifacts : [];
            const ids = [...existing, ...registered.map(a=>a.id)];
            if (!ids.length) throw new Error("Select files explicitly before attaching.");
            inputs = (await json("/api/artifacts/inputs", {artifact_ids:[...new Set(ids)], task_id:targetId})).inputs;
          }
          if (!inputs.length) throw new Error("Select files or enter a future relative file path.");
          completed = true; resolve(inputs); dialog.close();
        } catch (e) { message.textContent = e.message; } finally { button.disabled = false; }
      };
    });
  }

  const taskId = document.body.dataset.taskId;
  const host = document.querySelector("#task-files");
  let nextOffset = null, rows = [], refreshing = false;
  async function refresh(more = false) {
    if (!host || refreshing) return;
    refreshing = true;
    const message = host.querySelector("#file-error"); message.textContent = "Loading…";
    try {
      const [inputData, artifactData] = await Promise.all([json(`/api/tasks/${taskId}/inputs`), json(`/api/tasks/${taskId}/artifacts`)]);
      host.querySelector("#file-inputs").innerHTML = window.ImageAttachments.gallery(inputData.inputs) || '<span class="muted">No saved inputs.</span>';
      host.querySelector("#file-snapshots").innerHTML = artifactData.snapshots.map(a => `<article class="task-file-row"><div class="file-info"><strong>${escape(a.relative_path)}</strong><span class="small muted">Snapshot · ${size(a.size)} · ${escape(a.created_at)} · SHA-256 ${escape(a.sha256)}</span></div><button type="button" data-file-download="/api/tasks/${taskId}/artifacts/${escape(a.id)}/download" data-file-name="${escape(a.relative_path.split('/').pop())}">Download snapshot</button><button type="button" data-send-snapshot="${escape(a.id)}">Send to Task</button></article>`).join("") || '<span class="muted">No snapshots yet. Sending creates an immutable snapshot.</span>';
      const path = host.querySelector("#file-path").value.trim(), query = host.querySelector("#file-query").value.trim();
      const data = await json(`/api/tasks/${taskId}/files?path=${encodeURIComponent(path)}&q=${encodeURIComponent(query)}&offset=${more ? nextOffset || 0 : 0}`);
      rows = more ? [...rows, ...data.files] : data.files; nextOffset = data.next_offset;
      host.querySelector("#file-list").innerHTML = fileRows(rows);
      host.querySelector("#file-summary").textContent = `${rows.length} / ${data.total} files · Worktree · new, changed and declared files first${data.truncated ? ' · Scan limit reached; narrow the path' : ''}`;
      host.querySelector("#files-more").hidden = nextOffset == null;
      message.textContent = "";
    } catch (e) { message.textContent = e.message; host.querySelector("#file-list").innerHTML = ""; }
    finally { refreshing = false; }
  }
  const selected = () => [...host.querySelectorAll(".file-check:checked")].map(i=>i.value);
  async function send(paths = [], artifactIds = []) {
    const tasks = (await json("/api/tasks")).tasks;
    const dialog = modal("Send to Task", `<p>Selected: ${escape(paths.join(", ") || "saved snapshot")} · A snapshot is retained independently of the source worktree.</p>
      <label>Destination Task<select class="transfer-target"><option value="">Create a new task</option>${tasks.filter(t=>t.id!==taskId).map(t=>`<option value="${escape(t.id)}">${escape(t.name)} · ${escape(t.status)}</option>`).join("")}</select></label>
      <label>Additional instruction<textarea class="transfer-prompt" rows="3">Read the attached Artifact files and continue the task.</textarea></label>
      <button type="button" class="primary transfer-confirm">Confirm transfer</button>`);
    dialog.querySelector(".transfer-confirm").onclick = async ev => {
      ev.currentTarget.disabled = true;
      try {
        if (!paths.length && !artifactIds.length) throw new Error("Select at least one file.");
        const registered = paths.length ? (await json(`/api/tasks/${taskId}/artifacts`, {paths})).artifacts : [];
        const destination = dialog.querySelector(".transfer-target").value;
        const inputs = (await json("/api/artifacts/inputs", {artifact_ids:[...artifactIds, ...registered.map(a=>a.id)], task_id:destination || null})).inputs;
        if (!destination) {
          const source = tasks.find(t=>t.id===taskId);
          sessionStorage.setItem("codex-gui-artifact-draft", JSON.stringify({inputs, repository:source?.repository}));
          location.href = "/?attach_artifacts=1"; return;
        }
        const target = tasks.find(t=>t.id===destination);
        if (["waiting_dependencies", "queued", "retry_wait"].includes(target.status)) {
          await json(`/api/tasks/${destination}/inputs`, {attachment_ids:inputs.map(a=>a.id), pending_only:true});
        } else {
          await json(`/api/tasks/${destination}/messages`, {prompt:dialog.querySelector(".transfer-prompt").value, attachment_ids:inputs.map(a=>a.id), service_tier:"standard"});
        }
        dialog.close(); location.href = `/tasks/${destination}`;
      } catch (e) { dialog.querySelector(".dialog-error").textContent = e.message; ev.target.disabled = false; }
    };
  }
  if (host) {
    host.querySelector("#file-search").onsubmit = ev => { ev.preventDefault(); refresh(); };
    host.querySelector("#files-more").onclick = () => refresh(true);
    host.querySelector("#file-select-all").onclick = () => host.querySelectorAll(".file-check").forEach(i=>i.checked=true);
    host.querySelector("#files-zip").onclick = () => download(`/api/tasks/${taskId}/files/zip`, `${taskId}-files.zip`, host, {paths:selected()});
    host.querySelector("#files-send").onclick = () => send(selected()).catch(e=>error(host,e.message));
    host.addEventListener("click", async ev => {
      const row = ev.target.closest("[data-path]");
      const snapshot = ev.target.closest("[data-send-snapshot]");
      try {
        if (snapshot) await send([], [snapshot.dataset.sendSnapshot]);
        if (!row) return;
        const path = row.dataset.path;
        if (ev.target.closest("[data-download]")) await download(`/api/tasks/${taskId}/files/download?path=${encodeURIComponent(path)}`, path.split('/').pop(), host);
        if (ev.target.closest("[data-send]")) await send([path]);
        if (ev.target.closest("[data-preview]")) {
          const data = await json(`/api/tasks/${taskId}/files/preview?path=${encodeURIComponent(path)}`);
          const dialog = modal(path, '<p class="preview-limit muted small"></p><pre class="block file-preview"></pre>');
          dialog.querySelector("pre").textContent = data.text;
          dialog.querySelector(".preview-limit").textContent = data.truncated ? `Preview truncated at ${size(data.limit)}. Download to read the full file.` : size(data.size);
        }
      } catch (e) { error(host, e.message); }
    });
  }
  return {download, pickArtifacts, refresh};
})();
