"""Synthetic Claude/Codex events, not real quota observations or model quality evidence."""
import json
from pathlib import Path
import sys
import time
import uuid

request = json.load(sys.stdin)
candidate,provider,scenario = request["candidate"],request["provider"],request["scenario"]
payload = candidate["payload"]
name = payload["acceptance"].splitlines()[0].split()[-1]
root = Path(request["root"])
verdict,blocking = "APPROVE",[]
if scenario.get("delay"):
    time.sleep(scenario["delay"])
if name == "A" and (root/"A.txt").read_text() == "bad\n":
    verdict = "CHANGES_REQUESTED"
    blocking = [dict(id="A-value",path="A.txt",line=1,reason="Acceptance requires good, fixture currently writes bad")]
result = dict(candidate_id=candidate["id"],run_generation=candidate["run_generation"],scope=payload["scope"],
              verdict=verdict,blocking=blocking,nonblocking=[],coverage=payload["changed_paths"] or ["README.md"],
              unverified=[],resolved=payload.get("unresolved",[]) if not blocking else [])
if scenario.get("error"):
    print(json.dumps(dict(fixture_error=scenario["error"])))
elif provider == "claude" and (scenario.get("quota") or name == "B"):
    print(json.dumps(dict(type="rate_limit_event",rate_limit_info=dict(status="rejected",isUsingOverage=False,rateLimitType="five_hour",resetsAt=time.time()+3600))))
    print(json.dumps(dict(type="result",subtype="error_during_execution",is_error=True)))
else:
    print(json.dumps(dict(type="result",subtype="success",is_error=False,session_id="review-"+uuid.uuid4().hex,structured_output=result)))
