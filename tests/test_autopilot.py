import asyncio
import copy
import json
from pathlib import Path
import time

import pytest

from app.autopilot.controller import Autopilot
from app.autopilot.git import git, scope, sha, snapshot
from app.autopilot.ledger import Ledger
from app.autopilot.policy import manifest, digest
from app.autopilot.providers import ClaudeProvider, ProviderFailure, claude_error, validate_result
from app.autopilot.runner import Validator, sandbox, minimal_env, run_bounded, InfrastructureUnavailable, redact
from autopilot_helpers import FixtureProvider, drive, install, plan, register


def test_T03_T23_manifest_validation(git_repo):
    p=plan(git_repo)
    for change in (lambda p:p["nodes"][0].update(validation={}),lambda p:p["nodes"][0].update(paths=["../escape"]),
                   lambda p:p["nodes"][0].update(depends_on=["C"]),lambda p:p.update(permissions=dict(deploy=True))):
        broken=copy.deepcopy(p);change(broken)
        with pytest.raises(ValueError):manifest(broken)
    assert manifest(json.dumps(p))["policy"]["max_fix_rounds"] == 3
    yaml="version: 1\nrepository: "+str(git_repo)+"\nnodes:\n- id: docs\n  kind: report\n  paths: [README.md]\n  validation:\n    artifacts: [README.md]\n"
    assert manifest(yaml)["nodes"][0]["kind"] == "report"


def test_T05_T06_quota_classification_is_envelope_only():
    fake=[dict(type="assistant",text="rate_limit included_quota exhausted 429"),dict(type="tool_result",rate_limit_info=dict(status="rejected"))]
    assert claude_error(fake,"2.1.185").kind == "invalid_output"
    quota=dict(type="rate_limit_event",rate_limit_info=dict(status="rejected",isUsingOverage=False,rateLimitType="five_hour",resetsAt=time.time()+50))
    assert claude_error([quota],"2.1.185").kind == "included_quota"
    assert claude_error([quota],"unknown-version").kind != "included_quota"
    for subtype,kind in (("error_max_turns","max_turns"),("error_max_structured_output_retries","invalid_output"),("error_during_execution","unknown")):
        assert claude_error([dict(type="result",subtype=subtype,is_error=True)],"2.1.185").kind == kind
    for code,kind in (("authentication_error","auth"),("overloaded_error","transient"),("rate_limit_error","transient"),("context_length_exceeded","context")):
        events=[dict(type="system",subtype="api_error",error=dict(type=code)),dict(type="result",subtype="error_during_execution",is_error=True)]
        assert claude_error(events,"2.1.185").kind == kind


def test_T22_migration_preserves_legacy(db,settings):
    from app.database import Database
    db.close()
    reopened=Database(settings.db_path)
    assert reopened._conn.execute("SELECT count(*) FROM ap_workflows").fetchone()[0] == 0
    reopened.close()
    # Fixture teardown closes the connection again; sqlite close is idempotent.


def test_T01_T02_T11_acceptance_and_successor_once(make_manager,git_repo):
    async def scenario():
        m=make_manager();ap=install(m)
        raw=plan(git_repo,("C","E"));raw["nodes"][1]["depends_on"]=["C"]
        identity=await register(ap,raw)
        await drive(m,lambda:ap.ledger.view(identity)["state"] == "completed")
        view=ap.ledger.view(identity)
        assert len(m.db.list_tasks())==2
        assert all(n["state"]=="accepted" for n in view["nodes"])
        assert len(ap.ledger.rows("acceptances"))==2
        for _ in range(10):m.tick()
        assert len(ap.ledger.rows("reviews"))==2 and len(m.db.list_tasks())==2
        assert all(t["approval_source"]=="ai_review" for t in m.db.list_tasks())
        await m.shutdown()
    asyncio.run(scenario())


def test_T04_T08_fix_same_thread_and_quota_same_candidate(make_manager,git_repo,fake_codex_state):
    async def scenario():
        m=make_manager();ap=install(m)
        raw=plan(git_repo,("A","B","C"))
        raw["nodes"][1]["depends_on"]=["A"]
        identity=await register(ap,raw)
        await drive(m,lambda:ap.ledger.view(identity)["state"]=="completed")
        view=ap.ledger.view(identity)
        a=next(n for n in view["nodes"] if n["name"]=="A")
        b=next(n for n in view["nodes"] if n["name"]=="B")
        assert a["fix_rounds"]==1 and len(a["candidates"])==2
        assert [r["provider"] for c in a["candidates"] for r in c["reviews"]]==["claude","claude"]
        assert [r["provider"] for r in b["candidates"][0]["reviews"]]==["claude","codex"]
        invocations=[json.loads(s) for s in (fake_codex_state/"autopilot-implementations.jsonl").read_text().splitlines()]
        turns=[i for i in invocations if i["node"]=="A"]
        assert len(turns)==2 and turns[0]["thread"]==turns[1]["thread"]==turns[1]["resume"]
        assert a["candidates"][1]["payload"]["unresolved"]==["A-value"]
        await m.shutdown()
    asyncio.run(scenario())


def test_T07_T13_quota_releases_slots_and_stop_is_final(make_manager,git_repo):
    async def scenario():
        m=make_manager(max_concurrent=1);ap=install(m,FixtureProvider("claude",dict(quota=True)),FixtureProvider("codex",dict(error="included_quota")))
        identity=await register(ap,plan(git_repo))
        await drive(m,lambda:ap.ledger.view(identity)["nodes"][0]["state"]=="waiting_provider")
        assert not m._jobs and m._slots._value==1
        assert len(ap.ledger.rows("reviews"))==2
        await ap.control(identity,"stop")
        with ap.ledger.transaction() as c:c.execute("UPDATE ap_provider_availability SET recheck_at=0")
        for _ in range(10):m.tick()
        assert len(ap.ledger.rows("reviews"))==2 and not ap.ledger.rows("acceptances")
        with pytest.raises(ValueError):await ap.control(identity,"resume")
        await m.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize("error,state",[("auth","blocked_auth"),("configuration","blocked_configuration"),("timeout","blocked_infrastructure"),("transient","blocked_infrastructure"),("invalid_output","blocked_infrastructure"),("max_turns","blocked_infrastructure"),("context","blocked_infrastructure")])
def test_T06_bounded_errors_never_fallback(make_manager,git_repo,error,state):
    async def scenario():
        m=make_manager();claude=FixtureProvider("claude",dict(error=error));codex=FixtureProvider("codex");ap=install(m,claude,codex)
        raw=plan(git_repo);raw["policy"]["infra_retries"]=0
        identity=await register(ap,raw)
        await drive(m,lambda:ap.ledger.view(identity)["nodes"][0]["state"]==state)
        assert len(claude.calls)==1 and not codex.calls and not ap.ledger.rows("acceptances")
        await m.shutdown()
    asyncio.run(scenario())


def test_T10_T26_stale_head_review_cannot_accept(make_manager,git_repo):
    async def scenario():
        m=make_manager();ap=install(m,FixtureProvider("claude",dict(delay=.4)))
        identity=await register(ap,plan(git_repo))
        await drive(m,lambda:bool(ap.ledger.rows("reviews","state='running'")))
        node=ap.ledger.view(identity)["nodes"][0];task=m.db.get_task(node["task_id"])
        root=Path(task["worktree"]);(root/"C.txt").write_text("updated\n")
        await git(root,"add","C.txt");await git(root,"commit","-m","concurrent update")
        await drive(m,lambda:ap.ledger.view(identity)["nodes"][0]["state"]=="blocked_scope")
        assert not ap.ledger.rows("acceptances") and m.db.get_task(task["id"])["task_outcome"]=="needs_review"
        await m.shutdown()
    asyncio.run(scenario())


def test_T12_restart_uncertain_attempt_no_relaunch(make_manager,git_repo):
    async def scenario():
        m=make_manager();ap=install(m,FixtureProvider("claude",dict(delay=3)))
        identity=await register(ap,plan(git_repo))
        await drive(m,lambda:bool(ap.ledger.rows("reviews","state='running'")))
        await ap.shutdown()
        replacement=Autopilot(m,providers=dict(claude=FixtureProvider("claude"),codex=FixtureProvider("codex")))
        m.autopilot=replacement;m.tick()
        assert replacement.ledger.view(identity)["nodes"][0]["state"]=="reconciling"
        for _ in range(5):m.tick()
        assert len(replacement.ledger.rows("reviews"))==1
        await m.shutdown()
    asyncio.run(scenario())


def test_T15_missing_artifact_no_self_approve(make_manager,git_repo):
    async def scenario():
        m=make_manager();ap=install(m)
        raw=plan(git_repo);raw["nodes"][0]["contract"]={"required_paths":["missing.txt"]};raw["policy"]["max_fix_rounds"]=0
        identity=await register(ap,raw)
        await drive(m,lambda:ap.ledger.view(identity)["nodes"][0]["state"]=="blocked_budget")
        assert not ap.ledger.rows("reviews") and not ap.ledger.rows("acceptances")
        await m.shutdown()
    asyncio.run(scenario())


def test_T16_T21_validator_isolation(git_repo,tmp_path):
    async def scenario():
        base=await sha(git_repo)
        root=await snapshot(str(git_repo),base,base,tmp_path/"snapshot")
        secret=tmp_path/"db-and-auth";secret.write_text("secret")
        script="from pathlib import Path; assert not Path("+repr(str(secret))+ ").exists(); assert not Path('/home/kanade/.codex/auth.json').exists(); Path('/scratch/build').write_text('ok');\ntry: Path('README.md').write_text('tampered')\nexcept OSError: pass\nelse: raise AssertionError('source writable')"
        profile=dict(commands=[["python3","-c",script]],artifacts=["README.md"],outputs=[],timeout=15)
        candidate=dict(payload=dict(scope=await scope(root,base,base)))
        result=await Validator().validate(candidate,root,profile,tmp_path/"logs")
        assert result["state"]=="PASS"
        assert (root/"README.md").read_text()=="# demo\n" and secret.read_text()=="secret"
        assert (root/".git").is_dir() and not (root/".git/objects/info/alternates").exists()
    asyncio.run(scenario())


def test_T23_bulk_import_atomic_duplicate_rejected(make_manager,git_repo):
    async def scenario():
        m=make_manager();ap=install(m);raw=plan(git_repo)
        preview=await ap.dry_run(raw)
        await ap.register(raw,preview["input_digest"])
        with pytest.raises(ValueError):await ap.register(raw,preview["input_digest"])
        assert len(ap.ledger.rows("workflows"))==1 and not m.db.list_tasks()
        with pytest.raises(ValueError):await ap.register(raw,"wrong-digest")
        await m.shutdown()
    asyncio.run(scenario())


def test_T25_manual_gate_leaves_independent_branch_running(make_manager,git_repo):
    async def scenario():
        m=make_manager();ap=install(m);raw=plan(git_repo,("A","C"));raw["nodes"][0]["manual_gates"]=["OWNER_ACCEPTED"]
        identity=await register(ap,raw)
        await drive(m,lambda:next(n for n in ap.ledger.view(identity)["nodes"] if n["name"]=="C")["state"]=="accepted")
        assert next(n for n in ap.ledger.view(identity)["nodes"] if n["name"]=="A")["state"]=="blocked_decision"
        assert len(m.db.list_tasks())==1
        await m.shutdown()
    asyncio.run(scenario())


def test_T17_vertical_e2e_restart_integration_and_D(make_manager,git_repo,fake_codex_state,tmp_path):
    async def scenario():
        m=make_manager(max_concurrent=3);ap=install(m)
        raw=plan(git_repo,("A","B","C"))
        raw["nodes"].append(dict(id="I1",kind="integration",prompt="fixture I1",paths=["."],depends_on=["A","B","C"],
                      validation=dict(commands=[["python3","-c","from pathlib import Path; assert all(Path(p+'.txt').read_text()=='good\\n' for p in ['A','B','C'])"]],artifacts=["A.txt","B.txt","C.txt"])))
        raw["nodes"].append(dict(id="D",prompt="fixture D",paths=["D.txt"],depends_on=["I1"],validation=dict(commands=[["python3","-c","from pathlib import Path; assert Path('D.txt').read_text()=='good\\n'"]],artifacts=["D.txt"])))
        original=await sha(git_repo)
        identity=await register(ap,raw)
        await drive(m,lambda:all(n["state"]=="accepted" for n in ap.ledger.view(identity)["nodes"] if n["name"] in ("A","B","C")))
        # Freeze new dispatch, wait for any already-claimed read-only jobs, then replace the GUI/controller.
        await ap.control(identity,"pause")
        await asyncio.gather(*list(ap.jobs.values()),return_exceptions=True)
        await ap.shutdown();m.detach()
        replacement=make_manager(max_concurrent=3);new=install(replacement)
        replacement.recover();await new.control(identity,"resume")
        await drive(replacement,lambda:new.ledger.view(identity)["state"]=="completed",timeout=30)
        view=new.ledger.view(identity)
        integration=next(n for n in view["nodes"] if n["name"]=="I1")
        d=next(n for n in view["nodes"] if n["name"]=="D")
        task=replacement.db.get_task(d["task_id"])
        assert task["base_sha"]==integration["candidates"][-1]["payload"]["scope"]["head_sha"]
        assert len(integration["inputs"])==3 and len(integration["operations"])==1
        assert await sha(git_repo)==original  # live/main stayed unchanged
        assert len(new.ledger.rows("acceptances"))==5
        (tmp_path/"e2e-result.json").write_text(json.dumps(view,indent=2))
        await replacement.shutdown()
    asyncio.run(scenario())
