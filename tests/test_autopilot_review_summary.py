from app.autopilot.review_summary import summarize


def candidate_node(*, state="blocked_budget", findings=(), reviews=()):
    return dict(id="w:A", name="A", state=state, reason="fix limit", task_id="task", candidate_id="c",
                spec=dict(manual_gates=[]), candidates=[dict(id="c", reviews=list(reviews),
                    payload=dict(findings=list(findings), unresolved=[f["id"] for f in findings]))])


def test_summary_retains_inherited_findings_and_every_location_without_a_new_review():
    findings = [dict(id="one", path="a.py", line=3, reason="境界チェックが不足"),
                dict(id="two", path="a.py", line=35, reason="境界チェックが不足")]
    result = summarize(dict(state="running", nodes=[candidate_node(findings=findings)]))
    assert result["count"] == 1
    assert "a.py:3" in result["text"] and "a.py:35" in result["text"]


def test_stale_revision_is_reported_even_when_no_node_has_a_blocked_state():
    node = candidate_node(state="accepted")
    stale = dict(id="revision", state="stale", payload=dict(reason="plan revision is stale"))
    result = summarize(dict(state="running", nodes=[node], plan_revisions=[stale]))
    assert result["count"] == 1
    assert "適用できません" in result["text"] and "作り直してください" in result["text"]
    assert result["items"][0]["state"] == "stale"


def test_mentor_human_request_is_included_but_cannot_resolve_primary_findings():
    original = dict(id="original", path="a.py", line=3, reason="修正が未完了")
    question = dict(id="owner", path="a.py", line=5, reason="仕様の選択が必要")
    mentor = dict(id="mentor", state="mentored", payload=dict(purpose="mentor", verdict="NEEDS_HUMAN",
                   blocking=[question], resolved=["original"], unverified=["出力仕様"]))
    result = summarize(dict(state="running", nodes=[candidate_node(state="blocked_decision", findings=[original], reviews=[mentor])]))
    assert "修正が未完了" in result["text"] and "仕様の選択が必要" in result["text"]
    assert result["items"][0]["review_id"] == "mentor"


def test_mentor_reused_id_preserves_original_reason_and_unverified_requirements():
    original = dict(id="boundary", path="a.py", line=3, reason="境界チェックの欠落")
    review = dict(id="review", state="completed", payload=dict(
        purpose="review", verdict="CHANGES_REQUESTED", blocking=[original], unverified=["並行実行要件未確認"]))
    mentor = dict(id="mentor", state="mentored", payload=dict(
        purpose="mentor", verdict="NEEDS_HUMAN", blocking=[dict(original, reason="人が方針選択")], unverified=[]))
    result = summarize(dict(state="running", nodes=[candidate_node(state="blocked_decision", reviews=[review, mentor])]))
    assert all(text in result["text"] for text in ("境界チェックの欠落", "並行実行要件未確認", "人が方針選択"))
