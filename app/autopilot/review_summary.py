"""Human-readable, evidence-derived workflow status without another model call."""

import re

from .runner import redact


ACTIVE = {
    "launching", "implementing", "submitting", "validating", "reviewing", "mentoring",
    "fixing", "integrating", "integration_implementing", "publishing", "replanning",
    "planning", "probing_implementation",
}
WAITING = {"pending", "draft", "blocked_dependencies", "awaiting_replacement"}
ACTIONS = {
    "blocked_decision": "表示された要件と指摘を確認し、根拠を添えて承認または修正方針を回答してください",
    "blocked_replan": "提案された追加タスクと依存関係を確認し、計画の承認または見直しを判断してください",
    "blocked_auth": "対象CLIの認証を復旧してから再開してください",
    "blocked_configuration": "モデル・プロバイダー・検証設定を修正してから再開してください",
    "blocked_infrastructure": "検証またはレビューを完了できなかった原因を確認してください",
    "blocked_no_progress": "同じ問題が残っているため、修正方針または作業範囲を見直してください",
    "blocked_budget": "修正回数の上限に達したため、残る指摘と次の修正方針を確認してください",
    "blocked_scope": "担当範囲と実際の変更を確認し、必要なら計画を更新してください",
    "blocked_result": "不足している成果物または完了条件を確認してください",
    "reconciling": "前回の処理結果を確認し、重複実行を避けて復旧してください",
    "reconciling_git": "保存されたGit操作の結果を確認してください",
}


def _text(value, limit=1400):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\s+", " ", redact(value)).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def progress(nodes):
    current = [n for n in nodes if n["state"] != "superseded"]
    accepted = sum(n["state"] == "accepted" for n in current)
    active = sum(n["state"] in ACTIVE for n in current)
    blocked = sum(
        (n["state"].startswith("blocked_") and n["state"] not in WAITING)
        or n["state"].startswith("reconciling") or n["state"] == "stopped"
        for n in current
    )
    return dict(total=len(current), accepted=accepted, active=active, blocked=blocked,
                waiting=len(current) - accepted - active - blocked,
                superseded=len(nodes) - len(current),
                percent=round(100 * accepted / len(current)) if current else 0)


def _review_evidence(node):
    candidate = next((c for c in node.get("candidates", []) if c["id"] == node.get("candidate_id")), None)
    if not candidate:
        return [], [], None
    carried = candidate.get("payload", {}).get("findings", [])
    unresolved = candidate.get("payload", {}).get("unresolved", [])
    findings = {finding["id"]: finding for finding in carried
                if isinstance(finding, dict) and finding.get("id") in unresolved and finding.get("reason")}
    unverified, latest = [], None
    for review in candidate.get("reviews", []):
        payload = review.get("payload", {})
        is_mentor = payload.get("purpose") == "mentor"
        if review["state"] not in ("completed", "incomplete", "mentored"):
            continue
        if is_mentor and payload.get("verdict") != "NEEDS_HUMAN":
            continue
        latest = review
        if not is_mentor:
            for identity in payload.get("resolved", []):
                findings.pop(identity, None)
        for finding in payload.get("blocking", payload.get("findings", [])):
            if isinstance(finding, dict) and finding.get("reason"):
                identity = finding.get("id") or _text(finding["reason"])
                if is_mentor and identity in findings:
                    original = findings[identity]
                    if original["reason"] != finding["reason"]:
                        findings[identity] = dict(original, reason=original["reason"] + "。メンターの補足: " + finding["reason"])
                else:
                    findings[identity] = finding
        if review["state"] in ("completed", "mentored"):
            retained = unverified if is_mentor else []
            unverified = list(dict.fromkeys(retained + [_text(x) for x in payload.get("unverified", []) if _text(x)]))
    return list(findings.values()), unverified, latest


def summarize(view):
    """Summarize only actionable current evidence; historical failures stay in details."""
    items, prose = [], []
    pending_revisions = [r for r in view.get("plan_revisions", []) + view.get("graph_revisions", [])
                         if r["state"] in ("awaiting_owner", "stale")]
    revision_sources = {r.get("source_node") for r in pending_revisions}
    for node in view.get("nodes", []):
        state = node["state"]
        if (not state.startswith("blocked_") and not state.startswith("reconciling")) or state in WAITING:
            continue
        if state == "blocked_replan" and node["id"] in revision_sources:
            continue
        findings, unverified, review = _review_evidence(node)
        reasons = []
        seen = set()
        for finding in findings:
            reason = _text(finding["reason"])
            path = _text(finding.get("path", ""), 240)
            key = (path, finding.get("line", 0), reason)
            if key in seen:
                continue
            seen.add(key)
            location = path + (f":{finding['line']}" if path and finding.get("line", 0) > 0 else "")
            reasons.append((location + " — " if location else "") + reason)
        reasons.extend("未確認: " + text for text in unverified)
        if not reasons:
            reasons.append(_text(node.get("reason")) or "完了条件の確認が必要です")
        gates = node["spec"].get("manual_gates", [])
        if state == "blocked_decision" and gates:
            reasons.append("必要な人の判断: " + "、".join(_text(gate) for gate in gates))
        action = ACTIONS.get(state, "詳細と根拠を確認し、次の操作を判断してください")
        name = _text(node["name"], 100)
        detail = "；".join(reasons)
        prose.append(f"{name}では、{detail}。{action}。")
        items.append(dict(kind="node", node_id=node["id"], node_name=node["name"], state=state,
                          reason=detail, action=action, findings=findings, unverified=unverified,
                          candidate_id=node.get("candidate_id"),
                          review_id=review["id"] if review else None,
                          task_id=node.get("task_id"), target=node.get("decision_target")))
    for revision in pending_revisions:
        payload = revision.get("payload", {})
        changes = payload.get("owner_changes") or payload.get("expansions") or []
        if isinstance(changes, list):
            details = "、".join(_text(x) for x in changes if isinstance(x, str))
        else:
            details = _text(changes)
        if revision["state"] == "stale":
            reason = "現在の計画と一致しないため、変更案を適用できません"
            details = _text(payload.get("reason")) or details
            action = "古い変更案を承認せず、現状の担当範囲と依存関係を確認して計画を作り直してください"
        else:
            reason = "計画の変更に人の判断が必要です"
            action = "変更前後の担当範囲・文書・検証・依存関係を確認して判断してください"
        reason += "（" + details + "）" if details else ""
        prose.append(reason + "。" + action + "。")
        items.append(dict(kind="revision", revision_id=revision["id"], state=revision["state"], reason=reason, action=action,
                          target=revision.get("decision_target")))
    if not items:
        text = ("現在、人の判断を待っている項目はありません。すべての子タスクが完了しています。"
                if view.get("state") == "completed" else "現在、人の判断を待っている項目はありません。")
    else:
        text = f"確認が必要な項目は{len(items)}件です。" + "\n\n".join(prose)
    return dict(text=text, count=len(items), items=items)
