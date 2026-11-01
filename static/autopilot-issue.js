(() => {
  "use strict";
  const el = id => document.getElementById(id);
  if (!el("ap-issue-generate") || !window.AutopilotUI) return;

  el("ap-issue-generate").addEventListener("click", async () => {
    const button = el("ap-issue-generate");
    const status = el("ap-issue-status");
    const suggestions = el("ap-issue-suggestions");
    const details = el("ap-issue-result");
    button.disabled = true;
    suggestions.replaceChildren();
    details.textContent = "";
    status.textContent = "Issueと固定baseの参照情報を読み込み中…";
    try {
      const data = await window.AutopilotUI.api("/api/autopilot/issues/preview", {
        repository: el("ap-repository").value.trim(),
        base_ref: el("ap-base").value.trim() || "main",
        issue_url: el("ap-issue-url").value.trim(),
        paths: JSON.parse(el("ap-issue-paths").value),
        validation: JSON.parse(el("ap-issue-validation").value)
      });
      el("ap-manifest").value = JSON.stringify(data.manifest, null, 2);
      el("ap-manifest").dispatchEvent(new Event("input", {bubbles: true}));
      const missing = data.blockers || [];
      details.textContent = JSON.stringify({
        source: data.issue,
        fixed_base_sha: data.preview.plan.base_sha,
        acceptance_criteria: data.acceptance_criteria,
        referenced_context: data.context,
        blockers: missing,
        limitation: data.note
      }, null, 2);
      status.textContent = data.ready
        ? "Task Contractを生成しました。manifestと受け入れ条件を確認し、通常のDry-runから明示的にopt-inしてください。"
        : "未確定事項があります。下記の不足事項を解消してから再生成してください。実行はできません。";
      for (const missingItem of missing) {
        const p = document.createElement("p");
        p.textContent = "要確認: " + missingItem;
        suggestions.append(p);
      }
      const paths = data.context.filter(item => item.kind === "code").map(item => item.path);
      if (paths.length && !data.manifest.nodes[0].paths.length) {
        const fill = document.createElement("button");
        fill.type = "button";
        fill.textContent = "Issueで明示されたコードpath候補を入力（所有者の確認が必要）";
        fill.addEventListener("click", () => {
          el("ap-issue-paths").value = JSON.stringify([...new Set(paths)]);
          status.textContent = "候補を入力しました。編集許可範囲を確認してから再生成してください。";
        });
        suggestions.append(fill);
      }
    } catch (error) {
      status.textContent = "生成失敗: " + error.message;
      details.textContent = "";
    } finally {
      button.disabled = false;
    }
  });
})();
