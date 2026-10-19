"""Bounded independent repair advice. Mentor output never grants acceptance or permissions."""

from . import decisions
from .ledger import unpack
from .policy import canonical, digest, owns


MENTOR_POLICY = """
This is a MENTOR CONSULTATION to diagnose a blocked review or remaining defect and plan a bounded repair.
The original independent review's required findings remain binding. Explain the smallest concrete
repair and verification steps in each finding's reason; keep original finding IDs. Use supplied
fixed requirements, pinned code and validator evidence. Do not invent missing specifications,
increase edit ownership, change permissions, remove validation, or waive an owner's decision.
For an objective implementation defect return CHANGES_REQUESTED with actionable evidence and steps.
If a specification, authorization, credential, quota or owner decision is missing, return NEEDS_HUMAN.
An APPROVE response from this consultation cannot approve the candidate or discard prior findings;
the implementation must still be corrected and pass new managed validation and independent review.
"""

REVIEW_STOPS = {"blocked_decision": "NEEDS_HUMAN", "blocked_infrastructure": "INCONCLUSIVE"}


def pending(ap, node):
    records = ap.ledger.rows("reviews", "candidate_id=?", (node["candidate_id"],))
    source = next((r for r in reversed(records) if r["state"] == "completed"
                   and r["payload"].get("mentor_requested")
                   and r["payload"].get("purpose", "review") == "review"), None)
    if source and not any(r["state"] == "mentored" and r["payload"].get("source_review_id") == source["id"]
                          for r in records):
        return source
    return None


def queue(ap, node, source):
    return _queue(ap, node, source, recovering=False)


def recover(ap, node):
    """Inspect a saved review stop once; later mentor/failure records remain authoritative."""
    verdict = REVIEW_STOPS.get(node["state"])
    if not verdict or not node["candidate_id"]:
        return False
    records = ap.ledger.rows("reviews", "candidate_id=?", (node["candidate_id"],))
    if not records or records[-1]["payload"].get("verdict") != verdict:
        return False
    return _queue(ap, node, records[-1], recovering=True)


def _queue(ap, node, source, *, recovering):
    if not recovering and node["state"] != "reviewing":
        return False
    with ap.ledger.transaction() as connection:
        live = unpack(connection.execute("SELECT * FROM ap_workflows WHERE id=?", (node["workflow_id"],)).fetchone())
        current = unpack(connection.execute("SELECT * FROM ap_nodes WHERE id=?", (node["id"],)).fetchone())
        candidate = unpack(connection.execute("SELECT * FROM ap_candidates WHERE id=?", (node["candidate_id"],)).fetchone())
        latest = unpack(connection.execute(
            "SELECT * FROM ap_reviews WHERE candidate_id=? ORDER BY rowid DESC LIMIT 1", (node["candidate_id"],)
        ).fetchone())
        if (not live or not current or not candidate or not latest
            or live["state"] not in ("running", "paused")
            or current["state"] != node["state"] or current["generation"] != node["generation"]
            or current["candidate_id"] != node["candidate_id"]
            or candidate["node_id"] != current["id"] or candidate["generation"] != current["generation"]
            or live["generation"] != candidate["run_generation"]
            or live["generation"] != latest["payload"].get("run_generation")
            or latest["id"] != source["id"] or latest["state"] != "completed"
            or latest["payload"].get("purpose", "review") != "review"
            or latest["payload"].get("error_kind") or latest["payload"].get("mentor_requested")):
            return False
        policy, spec, payload = live["plan"]["policy"], current["spec"], latest["payload"]
        semantic = policy.get("semantic_recovery", True)
        verdict = payload.get("verdict")
        semantic_verdict = verdict in ("NEEDS_HUMAN", "INCONCLUSIVE")
        if (not policy.get("mentor_enabled", True)
            or (spec["manual_gates"] and not decisions.gate_approved(ap.ledger, current, live, connection))
            or (recovering and (not semantic or verdict != REVIEW_STOPS.get(current["state"])))
            or (semantic_verdict and not semantic)
            or (not semantic_verdict and (verdict != "CHANGES_REQUESTED" or current["fix_rounds"] < 1))):
            return False
        prior = [unpack(row) for row in connection.execute(
            "SELECT * FROM ap_reviews WHERE candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?)",
            (current["id"],),
        )]
        rounds = {r["candidate_id"] for r in prior
                  if r["payload"].get("mentor_requested") or r["payload"].get("purpose") == "mentor"}
        if (current["fix_rounds"] >= policy["max_fix_rounds"]
            or len(rounds) >= policy.get("mentor_max_rounds", 2) or current["candidate_id"] in rounds):
            return False
        # One first diagnosis may explain a repeated failure at the no-progress boundary.
        # The ensuing repair still uses the primary fingerprint and unchanged shared limits.
        at_no_progress_limit = (current["last_failure"] == digest(payload["blocking"])
                                and current["no_progress"] + 1 >= policy["no_progress_limit"])
        if at_no_progress_limit and (not semantic or rounds):
            return False
        connection.execute("UPDATE ap_reviews SET payload=? WHERE id=? AND state='completed'",
                           (canonical(dict(payload, mentor_requested=True)), latest["id"]))
        connection.execute("UPDATE ap_nodes SET state='review_queued',reason=? WHERE id=? AND state=?",
                           ("Independent mentor queued to diagnose the blocker; original findings stay required",
                            current["id"], current["state"]))
    return True


def context(source):
    return dict(source_review_id=source["id"], original_findings=source["payload"]["blocking"],
                prior_verdict=source["payload"]["verdict"],
                prior_unverified=source["payload"].get("unverified", []),
                prior_goal_review=source["payload"].get("goal_review", {}),
                purpose="concrete repair advice only")


def merge_findings(original, advice):
    """Keep the primary location/reason and append each distinct piece of mentor advice."""
    merged = {f["id"]: dict(f) for f in original}
    for finding in advice:
        if finding["id"] in merged:
            current = merged[finding["id"]]
            addition = "\nMentor guidance: " + finding["reason"]
            if finding["reason"] != current["reason"] and addition not in current["reason"]:
                current["reason"] += addition
        else:
            merged[finding["id"]] = dict(finding)
    return list(merged.values())


def preserve_findings(source, payload):
    """Record immutable primary findings separately from the provider's raw blocking output."""
    original = [dict(f) for f in source["payload"]["blocking"]]
    advice = payload.get("blocking", []) + payload.get("findings", [])
    return dict(payload, original_findings=original, repair_findings=merge_findings(original, advice))


def repair_findings(node, source, advice):
    """Retain every primary finding verbatim; append actionable advice within frozen ownership."""
    original = {f["id"]: dict(f) for f in source["payload"]["blocking"]}
    for finding in advice.get("blocking", []):
        # A primary finding may cite read-only evidence outside the edit ownership.
        # Retaining that same ID/path does not authorize edits to the evidence file.
        inherited_path = finding["id"] in original and finding["path"] == original[finding["id"]]["path"]
        if finding["path"] and not inherited_path and not owns(finding["path"], node["spec"]["paths"]):
            raise ValueError("mentor repair exceeds the approved edit ownership")
    return merge_findings(original.values(), advice.get("blocking", []))
