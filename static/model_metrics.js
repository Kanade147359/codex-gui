"use strict";
(() => {
  const tbody = document.getElementById("model-perf-rows");
  if (!tbody) return;
  function cell(tr, text) {
    const td = document.createElement("td");
    td.textContent = String(text);
    tr.appendChild(td);
  }
  async function refresh() {
    const response = await fetch("/api/model-performance");
    if (!response.ok) throw new Error("Could not load model metrics");
    const data = await response.json();
    tbody.replaceChildren();
    if (!data.models.length) {
      const tr = document.createElement("tr"); cell(tr, "No attributed attempts yet."); tbody.appendChild(tr); return;
    }
    for (const m of data.models) {
      const tr = document.createElement("tr");
      cell(tr, m.model);
      cell(tr, m.attempts);
      cell(tr, m.execution_success_rate == null ? "—" : (100 * m.execution_success_rate).toFixed(1) + "%");
      cell(tr, m.accepted_tasks);
      cell(tr, m.duration_avg_seconds == null ? "—" : m.duration_avg_seconds.toFixed(0) + "s");
      cell(tr, m.input_tokens.toLocaleString() + " / " + m.cached_input_tokens.toLocaleString());
      cell(tr, m.output_tokens.toLocaleString());
      cell(tr, m.accepted_prs);
      tbody.appendChild(tr);
    }
  }
  const form = document.getElementById("record-pr-form");
  const result = document.getElementById("pr-record-result");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form).entries());
    try {
      const response = await fetch("/api/tasks/" + encodeURIComponent(values.task_id) + "/accepted-pr", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({pr_url: values.pr_url, evidence: values.evidence})
      });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail?.message || error.detail || "Could not record PR");
      }
      result.textContent = "Recorded (manual evidence).";
      await refresh();
    } catch (error) { result.textContent = String(error.message); }
  });
  refresh().catch((error) => { tbody.textContent = String(error.message); });
})();
