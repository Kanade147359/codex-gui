/* File and image input shared by the new-task form and the task's follow-up composer. */
"use strict";
window.ImageAttachments = (() => {
  const escape = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const imageGallery = (images) => (images || []).map((a) => `<button type="button" class="image-preview" data-image-url="${escape(a.url)}" data-image-name="${escape(a.filename)}" title="${escape(a.filename)}: enlarge"><img src="${escape(a.url)}" alt="${escape(a.filename)}" loading="lazy"><span>${escape(a.filename)}</span></button>`).join("");
  const gallery = (items) => (items || []).map(a => a.kind === "file" || a.kind === "reference"
    ? `<div class="file-attachment"><strong>${escape(a.filename)}</strong><span class="small muted">${a.kind === "reference" ? "After source success" : (a.size / 1024).toFixed(1) + " KiB"}</span>${a.url ? `<button type="button" data-file-download="${escape(a.url)}" data-file-name="${escape(a.filename)}">Download input</button>` : ""}</div>`
    : imageGallery([a])).join("");

  function preview(url, name) {
    const dialog = document.createElement("dialog");
    dialog.className = "image-lightbox";
    dialog.innerHTML = `<div class="title-row"><strong>${escape(name)}</strong><button type="button">Close</button></div><img src="${escape(url)}" alt="${escape(name)}">`;
    document.body.appendChild(dialog);
    dialog.querySelector("button").addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => dialog.remove());
    dialog.addEventListener("click", (ev) => { if (ev.target === dialog) dialog.close(); });
    dialog.showModal();
  }
  document.addEventListener("click", (ev) => {
    const button = ev.target.closest("[data-image-url]");
    if (button) preview(button.dataset.imageUrl, button.dataset.imageName);
    const file = ev.target.closest("[data-file-download]");
    if (file) window.TaskFileUI?.download(file.dataset.fileDownload, file.dataset.fileName, file.parentNode);
  });

  function create(textarea, host, key) {
    const storageKey = "codex-gui-image-draft:" + key;
    let items = [], queue = Promise.resolve(), busy = false;
    let fileLimits = {max_files:16, max_file_bytes:100*1024*1024, max_message_bytes:400*1024*1024};
    fetch("/api/uploads/limits").then(r => r.ok ? r.json() : null).then(v => { if (v) { fileLimits = v; updateHint(); } }).catch(() => {});
    let limits = { max_images: 8, max_image_bytes: 10 * 1024 * 1024, max_message_bytes: 40 * 1024 * 1024 };
    fetch("/api/attachments/limits").then((r) => r.ok ? r.json() : null).then((v) => { if (v) limits = v; }).catch(() => {});
    host.innerHTML = `<button type="button" class="attach-button">Attach files</button><button type="button" class="attach-from-task">Attach from Task</button><input class="image-files" type="file" multiple hidden><span class="muted small file-limit-hint">Files · choose or drop · uploading limits…</span><div class="attachment-error" role="alert" hidden></div><div class="attachment-list"></div>`;
    function updateHint() {
      const hint = host.querySelector(".file-limit-hint");
      if (hint) hint.textContent = `Files: ${fileLimits.max_files}, ${(fileLimits.max_file_bytes/1024/1024).toFixed(0)} MiB each, ${(fileLimits.max_message_bytes/1024/1024).toFixed(0)} MiB total · Images: 8, 10 MiB each`;
    }
    updateHint();
    const picker = host.querySelector(".image-files");
    const list = host.querySelector(".attachment-list");
    const error = host.querySelector(".attachment-error");
    const choose = host.querySelector(".attach-button");
    function showError(message) { error.textContent = message; error.hidden = !message; }
    function persist() {
      try {
        if (!textarea.value && !items.length) localStorage.removeItem(storageKey);
        else localStorage.setItem(storageKey, JSON.stringify({ prompt: textarea.value, images: items.filter((a) => a.id || a.kind === "reference").map(({ file, objectUrl, error, ...a }) => a) }));
      } catch (_) { /* storage may be unavailable; the current input is still kept in memory */ }
    }
    function render() {
      list.innerHTML = items.map((a, i) => `<div class="attachment-card">${gallery([a])}<button type="button" data-remove-image="${i}" aria-label="Remove ${escape(a.filename)}" ${busy ? "disabled" : ""}>Remove</button>${(a.id || a.kind === "reference") ? "" : `<span class="small ${a.error ? "attachment-error" : "muted"}">${escape(a.error || `Uploading… ${a.progress || 0}%`)}</span>${a.error ? `<button type="button" data-retry-image="${i}" ${busy ? "disabled" : ""}>Retry upload</button>` : ""}`}</div>`).join("");
      if (textarea.name === "prompt") textarea.required = !items.length;
      persist();
    }
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (saved && Array.isArray(saved.images)) {
        items = saved.images.filter((a) => /^[a-f0-9]{32}$/.test(a.id) || a.kind === "reference").map((a) => ({ ...a, url: a.kind === "file" || a.kind === "reference" ? (a.url || "") : `/api/attachments/${a.id}` }));
        if (!textarea.value) textarea.value = saved.prompt || "";
      }
    } catch (_) {}
    textarea.addEventListener("input", persist);
    async function upload(item) {
      if (!items.includes(item) || item.id) return;
      item.error = "";
      render();
      try {
        const data = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", `${item.kind === "file" ? "/api/uploads" : "/api/attachments"}?filename=${encodeURIComponent(item.filename)}`);
          xhr.setRequestHeader("Content-Type", "application/octet-stream");
          xhr.upload.onprogress = ev => { if (ev.lengthComputable) { item.progress = Math.floor(ev.loaded / ev.total * 100); render(); } };
          xhr.onerror = () => reject(new Error("Upload failed. Check your connection and retry."));
          xhr.onabort = () => reject(new Error("Upload interrupted. Retry or remove this file."));
          xhr.onload = () => {
            let body; try { body = JSON.parse(xhr.responseText); } catch (_) { reject(new Error("Upload failed (" + xhr.status + ")")); return; }
            if (xhr.status < 200 || xhr.status >= 300) reject(new Error(typeof body.detail === "string" ? body.detail : body.detail?.message || "Upload failed"));
            else resolve(body);
          };
          xhr.send(item.file);
        });
        if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
        Object.assign(item, data, { objectUrl: null, file: null });
      } catch (e) { item.error = e.message; }
      render();
    }
    function add(files) {
      if (busy || textarea.disabled) return;
      showError("");
      for (const file of files) {
        const image = /^image\/(png|jpeg|webp)$/.test(file.type) || (!file.type && /\.(png|jpe?g|webp)$/i.test(file.name));
        const same = items.filter(a => (a.kind !== "file" && a.kind !== "reference") === image);
        const maxCount = image ? limits.max_images : fileLimits.max_files;
        const maxBytes = image ? limits.max_image_bytes : fileLimits.max_file_bytes;
        if (same.length >= maxCount) { showError(`添付は${maxCount}件までです。`); continue; }
        if ((image && !file.size) || file.size > maxBytes) { showError(`サイズ上限は${(maxBytes/1024/1024).toFixed(0)} MiBです。`); continue; }
        if (same.reduce((n, a) => n + (a.size || 0), 0) + file.size > (image ? limits.max_message_bytes : fileLimits.max_message_bytes)) { showError("添付の合計容量上限を超えています。"); continue; }
        const objectUrl = image ? URL.createObjectURL(file) : null;
        const item = { file, objectUrl, url: objectUrl || "", filename: file.name || "clipboard.png", size: file.size, ...(image ? {} : {kind:"file"}) };
        items.push(item);
        queue = queue.then(() => upload(item));
      }
      render();
    }
    host.querySelector(".attach-from-task").addEventListener("click", async () => {
      if (busy || textarea.disabled) return;
      try {
        const attached = await window.TaskFileUI.pickArtifacts(key.startsWith("task:") ? key.slice(5) : null);
        if (items.filter(a => a.kind === "file" || a.kind === "reference").length + attached.length > fileLimits.max_files) throw new Error("入力ファイルは16件までです。");
        items.push(...attached); showError(""); render();
      } catch (e) { showError(e.message); }
    });
    choose.addEventListener("click", () => { if (!textarea.disabled) picker.click(); });
    picker.addEventListener("change", () => { add([...picker.files]); picker.value = ""; });
    textarea.addEventListener("paste", (ev) => {
      const files = [...(ev.clipboardData?.items || [])].filter((i) => i.kind === "file" && i.type.startsWith("image/")).map((i) => i.getAsFile()).filter(Boolean);
      if (!files.length || busy || textarea.disabled) return;
      if (!ev.clipboardData.getData("text/plain")) ev.preventDefault();
      add(files);
    });
    // Only file drags are captured. Text paste/drop and IME keyboard events keep their native behaviour.
    for (const target of [textarea, host]) {
      target.addEventListener("dragover", (ev) => {
        if ([...(ev.dataTransfer?.types || [])].includes("Files")) { ev.preventDefault(); ev.dataTransfer.dropEffect = busy || textarea.disabled ? "none" : "copy"; }
      });
      target.addEventListener("drop", (ev) => {
        if (!ev.dataTransfer?.files.length) return;
        ev.preventDefault(); add([...ev.dataTransfer.files]);
      });
    }
    list.addEventListener("click", (ev) => {
      if (busy) return;
      const remove = ev.target.closest("[data-remove-image]");
      if (remove) {
        const [item] = items.splice(Number(remove.dataset.removeImage), 1);
        if (item?.objectUrl) URL.revokeObjectURL(item.objectUrl);
        showError(""); render();
      }
      const retry = ev.target.closest("[data-retry-image]");
      if (retry) { const item = items[Number(retry.dataset.retryImage)]; queue = queue.then(() => upload(item)); }
    });
    render();
    return {
      hasImages: () => items.length > 0,
      async ids() {
        await queue;
        if (items.some((a) => !a.id && a.kind !== "reference")) throw new Error("画像のアップロードが完了していません。理由を確認し、再試行または削除してください。");
        return items.filter(a => a.id).map((a) => a.id);
      },
      refs: () => items.filter(a => a.kind === "reference").map(a => a.ref),
      add(itemsToAdd) { items.push(...itemsToAdd); render(); },
      clear() { items.forEach((a) => { if (a.objectUrl) URL.revokeObjectURL(a.objectUrl); }); items = []; showError(""); render(); },
      setBusy(value) { busy = value; textarea.readOnly = value; choose.disabled = value; host.querySelector(".attach-from-task").disabled = value; picker.disabled = value; render(); },
    };
  }
  function history(host, messages) {
    const key = JSON.stringify(messages || []);
    if (host.dataset.imagesKey === key) return;
    host.dataset.imagesKey = key;
    host.innerHTML = (messages || []).map((m) => `<li><span class="muted small">${escape(m.created_at)} · ${escape(m.kind)}</span><pre class="block">${escape(m.prompt)}</pre><div class="attachment-list">${gallery(m.attachments)}</div></li>`).join("");
  }
  return { create, gallery, history };
})();
