// Loads context.js + app.js into a stub DOM exactly like the browser does (two classic scripts, in that order) and lets the
// page initialise against canned API responses. Any exception thrown during init or the first render fails the run.
// usage: node ui_page_smoke.js <page> <taskId> <responses.json> <static dir>
const fs = require("fs");
const vm = require("vm");
const [page, taskId, respFile, staticDir] = process.argv.slice(2);
const responses = JSON.parse(fs.readFileSync(respFile, "utf8"));
const errors = [];
process.on("unhandledRejection", (e) => errors.push("unhandledRejection: " + (e && e.stack || e)));

const registry = {};
function makeEl(name) {
  let html = "";
  const el = {
    __name: name, options: [], children: [], value: "", checked: false, hidden: false, disabled: false, textContent: "", href: "",
    dataset: {}, style: {}, className: "", scrollTop: 0, scrollHeight: 0, clientHeight: 0, childElementCount: 0,
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v); this.options = [...html.matchAll(/<option value="([^"]*)"/g)].map((m) => ({ value: m[1] })); },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(type, fn) { (this.__h = this.__h || {})[type] = (this.__h[type] || []).concat(fn); }, removeEventListener() {}, appendChild(c) { this.children.push(c); this.childElementCount = this.children.length; return c; },
    remove() {}, focus() {}, click() {}, showModal() {}, close() {}, setRangeText() {}, reportValidity: () => true,
    querySelector: (sel) => getEl(sel), querySelectorAll: () => [], closest: () => null,
    get firstElementChild() { return this._fec || (this._fec = makeEl(name + " > child")); }, lastElementChild: null,
    setAttribute() {}, getAttribute: () => null,
  };
  // form.elements.<name> and any other property read returns a stub, so the page code can touch every field
  el.elements = new Proxy({}, { get: (t, k) => (k in t ? t[k] : (t[k] = makeEl("form." + String(k)))) });
  return el;
}
function getEl(sel) { return registry[sel] || (registry[sel] = makeEl(sel)); }
const body = makeEl("body");
body.dataset = { page, taskId, scope: "task", repository: "/repo" };
const document = {
  body, title: "", querySelector: getEl, querySelectorAll: () => [], createElement: (t) => makeEl(t), createDocumentFragment: () => makeEl("fragment"),
  addEventListener() {},
};
const timers = [];
const calls = [];
const fetchStub = async (url, opts) => {
  calls.push({ method: (opts && opts.method) || "GET", url: String(url), body: opts && opts.body ? JSON.parse(opts.body) : null });
  const path = String(url).split("?")[0];
  const key = (opts && opts.method && opts.method !== "GET" ? opts.method + " " : "") + String(url);
  const hit = responses[key] !== undefined ? responses[key] : responses[path];
  const ok = hit !== undefined;
  return { ok, status: ok ? 200 : 404, json: async () => (ok ? hit : { detail: { message: "no canned response for " + key, code: "" } }) };
};
const sandbox = {
  document, console, fetch: fetchStub, setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout() {},
  setInterval: (fn) => { timers.push(fn); return timers.length; }, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {} }, location: { search: "", href: "", pathname: "/" },
  confirm: () => true, prompt: () => null, alert: (m) => errors.push("alert: " + m), URLSearchParams, Date, Math, JSON, Promise, Error,
  window: { addEventListener() {} }, FormData: class { forEach() {} },
};
sandbox.window.document = document;
const ctx = vm.createContext(sandbox);
const run = (file) => { try { vm.runInContext(fs.readFileSync(`${staticDir}/${file}`, "utf8"), ctx, { filename: file }); } catch (e) { errors.push(`${file}: ${e.stack || e}`); } };
run("context.js");
run("app.js");
(async () => {
  for (let i = 0; i < 4; i++) {                       // let the async init chains finish; run the queued timers once more
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setTimeout(r, 20));
    for (const fn of timers.splice(0)) { try { const r = fn(); if (r && r.catch) r.catch((e) => errors.push("timer: " + (e.stack || e))); } catch (e) { errors.push("timer: " + (e.stack || e)); } }
  }
  // UI_ACTIONS (JSON): [{sel, set: {prop: value}, fire: "click"|"change", target: {...}}], run after the page has initialised
  for (const a of JSON.parse(process.env.UI_ACTIONS || "[]")) {
    const el = getEl(a.sel);
    Object.assign(el, a.set || {});
    const target = a.target && a.target.cancelSid ? { closest: () => ({ dataset: { sid: a.target.cancelSid } }) } : a.target || el;  // a click on a Cancel button
    for (const fn of (el.__h || {})[a.fire] || []) { try { await fn({ target, preventDefault() {} }); } catch (e) { errors.push("handler: " + (e.stack || e)); } }
    for (let i = 0; i < 3; i++) await new Promise((r) => setImmediate(r));
  }
  const pick = (names) => Object.fromEntries(names.map((n) => [n, (registry[n] || {}).innerHTML || (registry[n] || {}).textContent || ""]));
  console.log(JSON.stringify({ errors, calls, instructionValue: getEl("#instruction").value, instructionDisabled: getEl("#instruction").disabled, published: !!(sandbox.window && sandbox.window.CtxUI),
    html: pick(["#ctx-banners", "#ctx-summary", "#ctx-settings", "#ctx-cache", "#ctx-tooloutputs", "#ctx-agents", "#tasks-body", "#send-btn", "#task-name", "#action-msg",
      "#task-status", "#deps-items", "#deps-summary", "#blocks-line", "#recovery-dl", "#recovery-hint", "#attempts-body", "#deps-list", "#instruction-hint",
      "#scheduled-items", "#scheduled-done-items", "#scheduled-summary", "#scheduled-done-summary", "#sched-deps", "#schedule-hint",
      "#completion-summary", "#completion-checks", "#completion-dependents", "#completion-validation-capability", "#completion-validation-note", "#completion-approval-note", "#completion-approvals"]),
    completionAutoApproveChecked: getEl("#completion-auto-approve").checked,
    sendFastHidden: (registry["#send-fast-btn"] || {}).hidden,
    hidden: Object.fromEntries(["#deps-section", "#run-anyway-btn", "#retry-deps-btn", "#retry-now-btn", "#retry-btn", "#attempts", "#stop-btn", "#scheduled-section", "#scheduled-done",
      "#schedule-box", "#sched-deps-box"]
      .map((n) => [n, (registry[n] || {}).hidden])) }));
})();
