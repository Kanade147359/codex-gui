"""Independent PR #5 safety probes; run against an isolated submitted-HEAD archive.

From archive cwd: PYTHONPATH=<archive>:<archive>/tests python -m pytest -c <archive>/pytest.ini -q this_file.py
Tests assert safety properties and are expected to FAIL on d395d23.
All content, credentials, databases, worktrees and providers are synthetic.
"""

import asyncio
from pathlib import Path
import sys

from fastapi.testclient import TestClient

from app.main import create_app
from app.autopilot.policy import profile
from app.autopilot.runner import Validator
from app.autopilot import planning
from autopilot_helpers import install, ImplementationRunner
from conftest import FakeRunner
from test_autopilot_documents import uploads, api_register, api_wait

pytest_plugins = ("conftest",)


def test_planner_cannot_remove_owner_assigned_document(settings, git_repo, tmp_path):
    app = create_app(settings, FakeRunner())
    with TestClient(app) as client:
        ap = install(app.state.manager)
        # Simulate a faulty planning model, using a real committed output and
        # the fixture reviewer. This does not replace any controller checks.
        helper = Path(__import__("autopilot_helpers").__file__).with_name("fake_autopilot_implementation.py")
        wrapper = tmp_path / "planning_model.py"
        wrapper.write_text(
            "import runpy,json\nfrom pathlib import Path\n"
            + f"runpy.run_path({str(helper)!r},run_name='__main__')\n"
            + "if json.loads(Path('.autopilot-inputs/requirements.json').read_text())['node']=='00':\n"
            + " p=Path('plan.json'); obj=json.loads(p.read_text()); obj['nodes'][0]['input_files']=[]; p.write_text(json.dumps(obj))\n"
        )

        class FaultyPlannerRunner(ImplementationRunner):
            def build_command(self, task, resume_thread=None):
                return [sys.executable, str(wrapper)] + (["resume", resume_thread] if resume_thread else [])

        app.state.manager.runner = FaultyPlannerRunner()

        async def hold_revision(_):
            await asyncio.Event().wait()

        # Stop at the accepted-00 boundary, keeping downstream Draft nodes idle.
        ap._plan_revision = hold_revision
        data = client.post("/api/autopilot/import", files=uploads()).json()
        raw = client.post(
            "/api/autopilot/import/convert",
            json=dict(data, repository=str(git_repo), permissions=dict(commit_owned_changes=True)),
        ).json()["manifest"]
        wid = api_register(client, raw)
        view = api_wait(client, wid, lambda v: v["nodes"][0]["state"] == "accepted")
        planner = view["nodes"][0]
        a = next(n for n in view["nodes"] if n["name"] == "A")
        assert "A.md" in a["spec"]["input_files"]
        client.portal.call(planning.propose, ap, planner)
        revision = ap.ledger.rows("plan_revisions", "workflow_id=?", (wid,))[0]
        assert revision["payload"]["expansions"] == []
        planning.apply(ap, revision)
        final = api_wait(client, wid, lambda v: v["state"] == "completed")
        accepted_a = next(n for n in final["nodes"] if n["name"] == "A")
        bundle = accepted_a["candidates"][-1]["payload"]["requirements"]
        assert accepted_a["state"] == "accepted" and not accepted_a["decisions"]
        delivered = [d["name"] for d in bundle["documents"]]
        client.portal.call(app.state.manager.shutdown)
        assert "documents/A.md" in delivered, (
            "ready/applied without owner decision, assigned instructions omitted: " + repr(delivered)
        )


def test_registered_runtime_cannot_expose_nested_credentials(tmp_path):
    async def scenario():
        runtime = tmp_path / "registered-runtime"
        (runtime / ".aws").mkdir(parents=True)
        (runtime / ".aws/credentials").write_text("synthetic-private-marker")
        (runtime / ".npmrc").write_text("synthetic-private-marker")
        (runtime / ".env").write_text("synthetic-private-marker")
        (runtime / "codex-gui").mkdir()
        (runtime / "codex-gui/state.sqlite").write_text("synthetic-private-marker")
        source = tmp_path / "source"
        source.mkdir()
        check = (
            "from pathlib import Path; "
            "assert all(Path('/opt/validation/cache', n).read_text() == "
            "'synthetic-private-marker' for n in ['.aws/credentials', '.npmrc', '.env', 'codex-gui/state.sqlite'])"
        )
        registered = profile(dict(
            commands=[["python3", "-c", check]],
            environment=dict(mounts=[dict(source=str(runtime), target="/opt/validation/cache")]),
        ), "code")
        result = await Validator().validate(
            dict(payload=dict(scope={"tree_sha": "synthetic"})), source, registered, tmp_path / "logs"
        )
        assert result["state"] != "PASS", "sandbox read three synthetic credentials and nested GUI state"

    asyncio.run(scenario())
