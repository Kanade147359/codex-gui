/* Image input shared by the new-task form and the task's follow-up composer. */
"use strict";
window.ImageAttachments = (() => {
  const escape = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const gallery = (images) => (images || []).map((a) => `<button type="button" class="image-preview" data-image-url="${escape(a.url)}" data-image-name="${escape(a.filename)}" title="${escape(a.filename)}: enlarge"><img src="${escape(a.url)}" alt="${escape(a.filename)}" loading="lazy"><span>${escape(a.filename)}</span></button>`).join("");

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
  });

  function create(textarea, host, key) {
    const storageKey = "codex-gui-image-draft:" + key;
    let items = [], queue = Promise.resolve(), busy = false;
    let limits = { max_images: 8, max_image_bytes: 10 * 1024 * 1024, max_message_bytes: 40 * 1024 * 1024 };
    fetch("/api/attachments/limits").then((r) => r.ok ? r.json() : null).then((v) => { if (v) limits = v; }).catch(() => {});
    host.innerHTML = `<button type="button" class="attach-button">Attach images</button><input class="image-files" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden><span class="muted small">PNG / JPEG / WebP · paste or drop · 8 images, 10 MiB each, 40 MiB total</span><div class="attachment-error" role="alert" hidden></div><div class="attachment-list"></div>`;
    const picker = host.querySelector(".image-files");
    const list = host.querySelector(".attachment-list");
    const error = host.querySelector(".attachment-error");
    const choose = host.querySelector(".attach-button");
    function showError(message) { error.textContent = message; error.hidden = !message; }
    function persist() {
      try {
        if (!textarea.value && !items.length) localStorage.removeItem(storageKey);
        else localStorage.setItem(storageKey, JSON.stringify({ prompt: textarea.value, images: items.filter((a) => a.id).map(({ file, objectUrl, error, ...a }) => a) }));
      } catch (_) { /* storage may be unavailable; the current input is still kept in memory */ }
    }
    function render() {
      list.innerHTML = items.map((a, i) => `<div class="attachment-card">${gallery([a])}<button type="button" data-remove-image="${i}" aria-label="Remove ${escape(a.filename)}" ${busy ? "disabled" : ""}>Remove</button>${a.id ? "" : `<span class="small ${a.error ? "attachment-error" : "muted"}">${escape(a.error || "Uploading…")}</span>${a.error ? `<button type="button" data-retry-image="${i}" ${busy ? "disabled" : ""}>Retry upload</button>` : ""}`}</div>`).join("");
      if (textarea.name === "prompt") textarea.required = !items.length;
      persist();
    }
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (saved && Array.isArray(saved.images)) {
        items = saved.images.filter((a) => /^[a-f0-9]{32}$/.test(a.id)).map((a) => ({ ...a, url: `/api/attachments/${a.id}` }));
        if (!textarea.value) textarea.value = saved.prompt || "";
      }
    } catch (_) {}
    textarea.addEventListener("input", persist);
    async function upload(item) {
      if (!items.includes(item) || item.id) return;
      item.error = "";
      render();
      try {
        const response = await fetch(`/api/attachments?filename=${encodeURIComponent(item.filename)}`, {
          method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: item.file,
        });
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.detail?.message || "Image upload failed");
        if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
        Object.assign(item, data, { objectUrl: null, file: null });
      } catch (e) { item.error = e.message; }
      render();
    }
    function add(files) {
      if (busy || textarea.disabled) return;
      showError("");
      for (const file of files) {
        if (!/^image\/(png|jpeg|webp)$/.test(file.type) && !(file.type === "" && /\.(png|jpe?g|webp)$/i.test(file.name))) {
          showError("対応形式はPNG、JPEG、WebPです。"); continue;
        }
        if (items.length >= limits.max_images) { showError(`画像は${limits.max_images}枚までです。`); continue; }
        if (!file.size || file.size > limits.max_image_bytes) { showError("画像は空にできません。1枚の上限は10 MiBです。"); continue; }
        if (items.reduce((n, a) => n + a.size, 0) + file.size > limits.max_message_bytes) { showError("画像の合計上限は40 MiBです。"); continue; }
        const objectUrl = URL.createObjectURL(file);
        const item = { file, objectUrl, url: objectUrl, filename: file.name || "clipboard.png", size: file.size };
        items.push(item);
        queue = queue.then(() => upload(item));
      }
      render();
    }
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
        if (items.some((a) => !a.id)) throw new Error("画像のアップロードが完了していません。理由を確認し、再試行または削除してください。");
        return items.map((a) => a.id);
      },
      clear() { items.forEach((a) => { if (a.objectUrl) URL.revokeObjectURL(a.objectUrl); }); items = []; showError(""); render(); },
      setBusy(value) { busy = value; textarea.readOnly = value; choose.disabled = value; picker.disabled = value; render(); },
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
