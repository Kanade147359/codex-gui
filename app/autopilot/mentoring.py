"""Bounded independent repair advice. Mentor output never grants acceptance or permissions."""

from .ledger import unpack
from .policy import canonical, digest, owns


MENTOR_POLICY = """
This is a MENTOR CONSULTATION after a corrective turn, not an acceptance decision.
The original independent review's required findings remain binding. Explain the smallest concrete
repair and verification steps in each finding's reason; keep original finding IDs. Use supplied
fixed requirements, pinned code and validator evidence. Do not invent missing specifications,
increase edit ownership, change permissions, remove validation, or waive an owner's decision.
For an objective implementation defect return CHANGES_REQUESTED with actionable evidence and steps.
If a specification, authorization, credential, quota or owner decision is missing, return NEEDS_HUMAN.
An APPROVE response from this consultation cannot approve the candidate or discard prior findings;
the implementation must still be corrected and pass new managed validation and independent review.
"""


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
    workflow = ap.ledger.get("workflows", node["workflow_id"])
    policy = workflow["plan"]["policy"]
    if (not policy.get("mentor_enabled", True) or node["fix_rounds"] < 1
        or source["payload"].get("verdict") != "CHANGES_REQUESTED"
        or node["spec"]["manual_gates"] or node["spec"]["contract"].get("manual_approval")):
        return False
    # Advice shares the exact existing fix/no-progress limits. New wording cannot replenish either.
    if (node["fix_rounds"] >= policy["max_fix_rounds"]
        or (node["last_failure"] == digest(source["payload"]["blocking"])
            and node["no_progress"] + 1 >= policy["no_progress_limit"])):
        return False
    prior = ap.ledger.rows("reviews", "candidate_id IN (SELECT id FROM ap_candidates WHERE node_id=?)", (node["id"],))
    rounds = {r["candidate_id"] for r in prior if r["payload"].get("mentor_requested")}
    if len(rounds) >= policy.get("mentor_max_rounds", 2) or node["candidate_id"] in rounds:
        return False
    with ap.ledger.transaction() as connection:
        live = unpack(connection.execute("SELECT * FROM ap_workflows WHERE id=?", (workflow["id"],)).fetchone())
        current = unpack(connection.execute("SELECT * FROM ap_nodes WHERE id=?", (node["id"],)).fetchone())
        if (live["state"] not in ("running", "paused")
            or live["generation"] != source["payload"].get("run_generation")
            or current["state"] != "reviewing" or current["generation"] != node["generation"]
            or current["candidate_id"] != source["candidate_id"]):
            return False
        connection.execute("UPDATE ap_reviews SET payload=? WHERE id=? AND state='completed'",
                           (canonical(dict(source["payload"], mentor_requested=True)), source["id"]))
        connection.execute("UPDATE ap_nodes SET state='review_queued',reason=? WHERE id=? AND state='reviewing'",
                           ("Independent mentor queued to explain the remaining repair; original findings stay required", node["id"]))
    return True


def context(source):
    return dict(source_review_id=source["id"], original_findings=source["payload"]["blocking"],
                prior_verdict=source["payload"]["verdict"], purpose="concrete repair advice only")


def repair_findings(node, source, advice):
    """Retain every primary finding verbatim; append actionable advice within frozen ownership."""
    original = {f["id"]: dict(f) for f in source["payload"]["blocking"]}
    for finding in advice.get("blocking", []):
        if finding["path"] and not owns(finding["path"], node["spec"]["paths"]):
            raise ValueError("mentor repair exceeds the approved edit ownership")
        if finding["id"] in original:
            if finding["reason"] != original[finding["id"]]["reason"]:
                original[finding["id"]]["reason"] += "\nMentor guidance: " + finding["reason"]
        else:
            original[finding["id"]] = dict(finding)
    return list(original.values())
