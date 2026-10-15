import json
from pathlib import Path
import sys

from app.autopilot.controller import Autopilot
from app.autopilot.providers import ClaudeProvider, ProviderFailure, review_prompt
from app.autopilot.runner import run_bounded
from app.codex_runner import CodexRunner


class ImplementationRunner(CodexRunner):
    def build_command(self, task, resume_thread=None):
        return [sys.executable, str(Path(__file__).with_name("fake_autopilot_implementation.py"))] + (
            ["resume", resume_thread] if resume_thread else []
        )


class FixtureProvider(ClaudeProvider):
    def __init__(self, provider, scenario=None):
        super().__init__()
        self.provider = provider
        self.scenario = scenario or {}
        self.calls = []

    async def probe_capabilities_and_auth(self, policy):
        return dict(version="fixture", auth="fake; no real account", fixture=True)

    async def start_review(self, candidate, policy, root, attempt, on_identity):
        received = json.loads(await review_prompt(candidate, root))
        self.calls.append(dict(candidate=candidate["id"], attempt=attempt["id"], received=received))

        def started(proc):
            self.processes[attempt["id"]] = proc
            on_identity(
                dict(pid=proc.pid, version="synthetic fixture", model="fake", session="independent-" + attempt["id"])
            )

        try:
            _, output, _ = await run_bounded(
                [sys.executable, str(Path(__file__).with_name("fake_autopilot_review.py"))],
                stdin=json.dumps(
                    dict(
                        candidate=candidate,
                        provider=self.provider,
                        scenario=self.scenario,
                        root=str(root),
                        received=received,
                    )
                ),
                on_start=started,
                timeout=15,
                log_path=attempt.get("event_log"),
            )
            events = [json.loads(s) for s in output.splitlines()]
            if events and "fixture_error" in events[0]:
                raise ProviderFailure(events[0]["fixture_error"], "Synthetic fixture provider failure")
            result = self.consume_events(output, "2.1.185", candidate)
            return dict(
                result,
                provider=self.provider,
                model="fake",
                version="synthetic fixture",
                session=result.pop("_session", None),
                independence="independent fixture process",
            )
        finally:
            self.processes.pop(attempt["id"], None)

    async def start_replan(self, packet, policy, root, attempt, on_identity):
        from app.autopilot.replanning import validate_proposal

        self.calls.append(dict(replan=packet["source_node"]["name"], packet=packet))
        rc, output, timed = await run_bounded(
            [sys.executable, str(Path(__file__).with_name("fake_autopilot_replanner.py"))],
            stdin=json.dumps(dict(packet=packet, root=str(root), scenario=self.scenario)),
            timeout=15,
            log_path=attempt.get("event_log"),
            on_start=lambda proc: on_identity(dict(pid=proc.pid, session="replan-process-" + attempt["id"])),
        )
        if rc or timed:
            raise ProviderFailure("invalid_output", "fake replanner did not finish: " + output[-500:])
        value = json.loads(output)
        if "fixture_error" in value:
            raise ProviderFailure(value["fixture_error"], "fixture replanner unavailable")
        validate_proposal({k: v for k, v in value.items() if k != "session"}, packet)
        return dict(value, provider="codex", model="fake", provider_version="synthetic fixture", independence="new independent fixture process")


def install(manager, claude=None, codex=None):
    manager.runner = ImplementationRunner()
    manager.autopilot = Autopilot(
        manager, providers=dict(claude=claude or FixtureProvider("claude"), codex=codex or FixtureProvider("codex"))
    )
    return manager.autopilot


def plan(repo, names=("C",)):
    nodes = []
    for n in names:
        nodes.append(
            dict(
                id=n,
                prompt="fixture " + n,
                paths=[n + ".txt"],
                validation=dict(
                    commands=[["python3", "-c", f"from pathlib import Path; assert Path('{n}.txt').exists()"]],
                    artifacts=[n + ".txt"],
                ),
            )
        )
    return dict(
        version=1,
        name="isolated fixture",
        repository=str(repo),
        base_ref="main",
        nodes=nodes,
        policy=dict(extra_usage_disabled_confirmed=True, provider_backoff=1),
        permissions=dict(commit_owned_changes=True),
        inputs={},
    )


async def register(ap, raw):
    preview = await ap.dry_run(raw)
    w = await ap.register(raw, preview["input_digest"])
    await ap.control(w["id"], "start")
    return w["id"]


async def drive(manager, predicate, timeout=20):
    import asyncio
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        manager.tick()
        if predicate():
            return
        await asyncio.sleep(0.02)
    views = [manager.autopilot.ledger.view(w["id"]) for w in manager.autopilot.ledger.rows("workflows")]
    diagnostics = manager.settings.home / "autopilot-test-failure.json"
    diagnostics.write_text(json.dumps(views, indent=2, default=str))
    raise AssertionError(
        "workflow timed out: "
        + json.dumps([(n["name"], n["state"], n["reason"][:200]) for w in views for n in w["nodes"]])
        + "; diagnostics: "
        + str(diagnostics)
    )
