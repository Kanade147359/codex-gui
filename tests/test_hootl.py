"""Deterministic three-layer regressions: local planner + existing mock CLI processes."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from app.autopilot import dsh
from app.autopilot.controller import Autopilot
from app.autopilot.policy import manifest, canonical
from autopilot_helpers import FixtureProvider, drive, install, plan, register


@pytest.fixture
def ollama():
    calls = []
    responses = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.path == "/api/show":
                value = {"details": {"family": "fixture"}}
            else:
                context = json.loads(body["prompt"])["context"]
                choice = dict(node_id=context["ready"][-1]["id"], reason="fixture priority")
                value = responses.pop(0) if responses else dict(done=True, done_reason="stop", response=json.dumps(choice))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield dict(model="fixture:local", endpoint=f"http://127.0.0.1:{server.server_port}", timeout=5), calls, responses
    server.shutdown()
    server.server_close()
    thread.join()


def mission(repo, config, names=("C",)):
    raw = plan(repo, names)
    raw["policy"].update(dsh=config, review_provider_policy="claude_only", mentor_enabled=False)
    return raw


def test_three_layers_feedback_dependencies_and_metrics(make_manager, git_repo, fake_codex_state, ollama):
    async def scenario():
        config, calls, _ = ollama
        manager = make_manager()
        ap = install(manager)
        raw = mission(git_repo, config, ("A", "C"))
        raw["nodes"][1]["depends_on"] = ["A"]
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        view = ap.ledger.view(identity)
        requests = [body for path, body in calls if path == "/api/generate"]
        assert [[n["id"] for n in json.loads(r["prompt"])["context"]["ready"]] for r in requests] == [["A"], ["C"]]
        assert all(r["options"]["num_gpu"] == 0 and r["stream"] is False for r in requests)
        invocations = [json.loads(line) for line in (fake_codex_state / "autopilot-implementations.jsonl").read_text().splitlines()]
        fixes = [i for i in invocations if i["node"] == "A"]
        assert len(fixes) == 2 and fixes[0]["thread"] == fixes[1]["resume"]
        assert len(manager.db.list_tasks()) == 2
        assert {r["provider"] for r in ap.ledger.rows("reviews")} == {"claude"}
        assert view["metrics"]["verified_deliverables"] == 2
        assert view["metrics"]["dsh_assignment_success_rate"] == 1
        assert view["metrics"]["claude_findings"] == 1
        assert view["metrics"]["quota_stop_seconds"] is None
        for _ in range(20):
            manager.tick()
        assert len(calls) == 4
        await manager.shutdown()

    asyncio.run(scenario())


def test_required_claude_quota_waits_and_resumes_same_candidate(make_manager, git_repo, ollama):
    async def scenario():
        manager = make_manager()
        ap = install(manager, FixtureProvider("claude", dict(quota=True)))
        raw = mission(git_repo, ollama[0])
        raw["policy"]["provider_backoff"] = 60
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"] == "waiting_provider")
        candidate = ap.ledger.view(identity)["nodes"][0]["candidate_id"]
        assert len(ap.ledger.rows("reviews")) == 1
        assert not ap.ledger.rows("acceptances") and not ap.providers["codex"].calls
        ap.providers["claude"] = FixtureProvider("claude")
        with ap.ledger.transaction() as c:
            c.execute("UPDATE ap_provider_availability SET recheck_at=0")
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        assert ap.ledger.view(identity)["nodes"][0]["candidate_id"] == candidate
        await manager.shutdown()

    asyncio.run(scenario())


def test_acceptance_gate_rejects_substitute_audit(make_manager, git_repo, ollama):
    async def scenario():
        manager = make_manager()
        ap = install(manager)
        original = ap.ledger.accept
        checked = []

        def accept(candidate, review, scope):
            with ap.ledger.transaction() as c:
                c.execute("UPDATE ap_reviews SET provider='codex' WHERE id=?", (review["id"],))
            assert not original(candidate, review, scope)
            assert not ap.ledger.rows("acceptances")
            checked.append(True)
            with ap.ledger.transaction() as c:
                c.execute("UPDATE ap_reviews SET provider='claude' WHERE id=?", (review["id"],))
            return original(candidate, review, scope)

        ap.ledger.accept = accept
        identity = await register(ap, mission(git_repo, ollama[0]))
        await drive(manager, lambda: ap.ledger.view(identity)["state"] == "completed")
        assert checked
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    dict(done=True, done_reason="stop", response='{"node_id":"outside","reason":"escape"}'),
    dict(done=True, done_reason="stop", response='{"node_id":"C","node_id":"C","reason":"duplicate"}'),
    dict(done=True, done_reason="length", response='{"node_id":"C","reason":"truncated"}'),
    dict(done=True, done_reason="stop", response='{"node_id":"C","reason":"ok","command":"rm"}'),
])
def test_invalid_planner_never_launches_or_retries(make_manager, git_repo, ollama, response):
    async def scenario():
        config, calls, responses = ollama
        responses.append(response)
        manager = make_manager()
        ap = install(manager)
        identity = await register(ap, mission(git_repo, config))
        await drive(manager, lambda: any(e["state"] == "blocked" for e in ap.ledger.view(identity)["events"]))
        for _ in range(20):
            manager.tick()
        assert not manager.db.list_tasks()
        assert len(calls) == 2
        await manager.shutdown()

    asyncio.run(scenario())


def test_planning_claim_pause_restart_and_resume(make_manager, git_repo, ollama):
    async def scenario():
        manager = make_manager()
        ap = install(manager)
        identity = await register(ap, mission(git_repo, ollama[0]))
        entered = asyncio.Event()

        class Waiting:
            calls = 0

            async def choose(self, config, context):
                self.calls += 1
                entered.set()
                await asyncio.Event().wait()

        waiting = ap.dispatcher = Waiting()
        ap.tick()
        await entered.wait()
        other = Autopilot(manager, providers=ap.providers)
        other.reconcile(identity)
        assert other.ledger.view(identity)["events"][0]["state"] == "running"
        workflow = other.ledger.get("workflows", identity)
        assert dsh.selection(other, workflow, other.ledger.rows("nodes", "workflow_id=?", (identity,))) is None
        assert not other.jobs
        for _ in range(15):
            ap.tick()
        assert waiting.calls == 1 and not manager.db.list_tasks()
        await ap.control(identity, "pause")
        await ap.shutdown()
        replacement = Autopilot(manager, providers=ap.providers)
        manager.autopilot = replacement
        replacement.reconcile()
        assert replacement.ledger.view(identity)["events"][0]["state"] == "interrupted"
        replacement.tick()
        assert not manager.db.list_tasks()
        await replacement.control(identity, "resume")
        await drive(manager, lambda: replacement.ledger.view(identity)["state"] == "completed")
        assert len(manager.db.list_tasks()) == 1
        assert replacement.ledger.view(identity)["metrics"]["dsh_request_attempts"] == 2
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("status,expected", [(401, "blocked"), (404, "blocked"), (503, "retry")])
def test_local_http_failure_classification_and_retry_budget(make_manager, git_repo, ollama, status, expected):
    import urllib.error

    async def scenario():
        manager = make_manager()
        ap = install(manager)

        class Unavailable:
            async def choose(self, config, context):
                raise urllib.error.HTTPError(config["endpoint"], status, "fixture", {}, None)

        ap.dispatcher = Unavailable()
        identity = await register(ap, mission(git_repo, ollama[0]))
        await drive(manager, lambda: any(e["state"] == expected for e in ap.ledger.view(identity)["events"]))
        if expected == "retry":
            for expected_attempt in (2, 3):
                with ap.ledger.transaction() as c:
                    event = ap.ledger.view(identity)["events"][0]
                    payload = dict(event["payload"], retry_at=0)
                    c.execute("UPDATE ap_workflow_events SET payload=? WHERE id=?", (canonical(payload), event["id"]))
                await drive(manager, lambda: any(e["payload"]["attempt"] == expected_attempt and e["state"] != "running"
                                                 for e in ap.ledger.view(identity)["events"]))
            event = ap.ledger.view(identity)["events"][0]
            assert event["state"] == "blocked" and len(event["payload"]["history"]) == 2
        assert not manager.db.list_tasks()
        await manager.shutdown()

    asyncio.run(scenario())


def test_validation_failure_never_audits_or_accepts(make_manager, git_repo, ollama):
    async def scenario():
        manager = make_manager()
        ap = install(manager)
        raw = mission(git_repo, ollama[0])
        raw["nodes"][0]["validation"]["commands"] = [["python3", "-c", "raise SystemExit(1)"]]
        raw["policy"]["max_fix_rounds"] = 0
        identity = await register(ap, raw)
        await drive(manager, lambda: ap.ledger.view(identity)["nodes"][0]["state"].startswith("blocked_"))
        assert not ap.ledger.rows("reviews") and not ap.ledger.rows("acceptances")
        assert ap.ledger.view(identity)["metrics"]["validation_pass_rate"] == 0
        await manager.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [dict(model="remote:cloud"), dict(model="ok", endpoint="https://example.com:443"),
                                dict(model="ok", endpoint="http://localhost:11434"),
                                dict(model="ok", endpoint="http://127.0.0.1:11434@evil:80"),
                                dict(model="ok", timeout=True), dict(model="ok", num_thread=100)])
def test_dsh_rejects_remote_and_unbounded_config(bad):
    with pytest.raises(ValueError):
        dsh.normalize(bad)


def test_dsh_permissions_and_audit_policy(git_repo):
    raw = mission(git_repo, dict(model="local:3b"))
    raw["permissions"]["publish_main"] = True
    with pytest.raises(ValueError, match="publish main"):
        manifest(raw)
    raw["permissions"]["publish_main"] = False
    raw["policy"]["review_provider_policy"] = "claude_then_codex"
    with pytest.raises(ValueError, match="claude_only"):
        manifest(raw)


@pytest.mark.parametrize("invalid", [None, "escape", "cycle", "profile", "commands"])
def test_goal_decomposition_preserves_envelope_and_requires_registration(make_manager, git_repo, invalid):
    async def scenario():
        manager = make_manager()
        ap = install(manager)
        envelope = dict(goal="Add a tested local feature", repository=str(git_repo), paths=["src", "tests"],
                        validation_profiles={"test": {"commands": [["python3", "-m", "unittest"]]}},
                        policy=dict(dsh=dict(model="local:3b")))

        class Planner:
            async def generate(self, config, context, schema, planning=False):
                assert planning and schema["properties"]["tasks"]["maxItems"] == 8
                tasks = [dict(id="implement", prompt="Implement feature and tests", paths=["src", "tests"],
                              depends_on=[], validation_profile="test")]
                if invalid == "escape": tasks[0]["paths"] = [".github"]
                if invalid == "cycle": tasks[0]["depends_on"] = ["implement"]
                if invalid == "profile": tasks[0]["validation_profile"] = "missing"
                if invalid == "commands": tasks[0]["commands"] = [["echo", "bypass"]]
                return {"tasks": tasks}, {"model": "fixture", "usage": {}}

        ap.dispatcher = Planner()
        if invalid:
            with pytest.raises(ValueError):
                await dsh.draft_plan(ap, envelope)
        else:
            result = await dsh.draft_plan(ap, envelope)
            proposed = result["manifest"]
            assert proposed["inputs"]["MISSION.md"] == envelope["goal"]
            assert proposed["nodes"][0]["validation"]["commands"] == [["python3", "-m", "unittest"]]
            assert proposed["policy"]["review_provider_policy"] == "claude_only"
            assert not proposed["permissions"]["publish_main"]
        assert not ap.ledger.rows("workflows") and not manager.db.list_tasks()
        await manager.shutdown()

    asyncio.run(scenario())
