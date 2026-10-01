// Renders the Context Efficiency UI with data from the real backend (tests/test_ui_context.py writes it) into a stub DOM.
const fs = require("fs");
const vm = require("vm");
const data = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const els = {};
function makeEl() {
  let html = "";
  return { get innerHTML() { return html; }, set innerHTML(v) { html = v; this.options = [...String(v).matchAll(/<option value="([^"]*)"/g)].map((m) => ({ value: m[1] })); },
    options: [], textContent: "", value: "", checked: false, hidden: false, dataset: {}, elements: { repository: { value: "/repo", addEventListener() {} } },
    querySelectorAll: () => [], addEventListener() {}, classList: { toggle() {} } };
}
const el = (sel) => (els[sel] = els[sel] || makeEl());
const document = { querySelector: (sel) => el(sel), querySelectorAll: () => [] };
const ctx = vm.createContext({ document, console, fetch: async () => { throw new Error("no network in the smoke test"); }, window: {}, setTimeout, clearTimeout });
vm.runInContext(fs.readFileSync(process.argv[3], "utf8"), ctx);  // exactly as the browser loads it: nothing is exported by the harness
const U = ctx.window.CtxUI;
if (!U) throw new Error("context.js did not publish window.CtxUI: app.js checks window.CtxUI");
const out = {};
if (data.options) {
  U.initNewTask(data.options);
  out.form_before = U.formValues();
  els["#ctx-tool-output"].value = "conservative"; els["#ctx-skills"].value = "economy"; els["#ctx-tool-profile"].value = "minimal";
  els["#ctx-subagents"].checked = true; els["#ctx-cwd"].value = "pkg/api";
  out.form_after = U.formValues();
  out.selects = { tool_output: els["#ctx-tool-output"].innerHTML, skills: els["#ctx-skills"].innerHTML, profile: els["#ctx-tool-profile"].innerHTML };
  out.notes = { output: els["#ctx-tool-output-note"].textContent, skills: els["#ctx-skills-note"].textContent, profile: els["#ctx-profile-note"].textContent };
  els["#ctx-tool-output"].value = "balanced"; U.initNewTask(data.options);
}
U.renderTask(data.task, { zoneAction() {}, changeProfile() {} });
for (const k of ["#ctx-banners", "#ctx-summary", "#ctx-settings", "#ctx-cache", "#ctx-tooloutputs"]) out[k] = el(k).innerHTML;
out.audit = U.agentsHealthHtml(data.audit, (f) => (f.scope === "project" ? "/edit/" + f.relative : null));
out.verify = U.verifyHtml(data.verify);
out.verifyNull = U.verifyHtml(null);
out.empty = U.agentsHealthHtml(null);
console.log(JSON.stringify(out));
