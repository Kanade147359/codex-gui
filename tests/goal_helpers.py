from pathlib import Path
import sys

from app.autopilot.controller import Autopilot
from autopilot_helpers import ImplementationRunner, FixtureProvider, plan


class GoalRunner(ImplementationRunner):
    def build_command(self, task, resume_thread=None):
        return [
            sys.executable,
            str(Path(__file__).with_name("fake_goal_implementation.py")),
        ] + (["resume", resume_thread] if resume_thread else [])


class GoalReviewer(FixtureProvider):
    async def start_review(self, candidate, policy, root, attempt, on_identity):
        result = await super().start_review(candidate, policy, root, attempt, on_identity)
        node = candidate["payload"]["requirements"]["node"]
        context = candidate["payload"].get("review_context", {})
        part = context.get("partition", {})
        path = "G.py" if node == "G" else "C.py"
        scoped = not part or part.get("path") == path
        bad_goal = node == "G" and "remote" in (root / "G.py").read_text() and scoped
        bad_impl = node == "C" and "return 'good'" in (root / "C.py").read_text() and scoped
        if bad_goal or bad_impl:
            identity = "COMMON-no-remote" if bad_goal else "C-read-input"
            result.update(
                verdict="CHANGES_REQUESTED",
                blocking=[
                    dict(
                        id=identity,
                        path=path,
                        line=1,
                        reason="Honor COMMON: No remote access"
                        if bad_goal
                        else "Read the actual input instead of returning a constant",
                    )
                ],
            )
            axis = "goal_alignment" if bad_goal else "task_correctness"
            result["goal_assessment"][axis]["status"] = "VIOLATION"
            if bad_goal:
                assert any(
                    "No remote" in d["body"] for d in candidate["payload"]["requirements"]["documents"]
                )
                result["goal_assessment"]["architectural_consistency"]["status"] = "VIOLATION"
            result["diagnosis"]["finding_ids"] = [identity]
        elif not result["blocking"]:
            result["resolved"] = candidate["payload"].get("unresolved", [])
        return result


def install_goals(manager, **scenario):
    manager.runner = GoalRunner()
    manager.autopilot = Autopilot(
        manager,
        providers=dict(
            claude=GoalReviewer("claude", scenario),
            codex=GoalReviewer("codex", scenario),
        ),
    )
    return manager.autopilot


def root_plan(repo, mode="auto_within_envelope"):
    raw = plan(repo, ("C", "D", "E"))
    raw["inputs"] = {
        "COMMON.md": "# Shared goal\nNo remote access. Parse accepted input locally. Export can be implemented by another task.\n",
        "C.md": "Read the input and produce good. The task assumes Input is JSON.\n",
    }
    raw["goals"] = [
        dict(
            id="G-local",
            kind="invariant",
            text="Keep input processing local",
            source="COMMON.md",
            quote="No remote access",
        )
    ]
    raw["policy"]["replanning"] = dict(mode=mode)
    c = raw["nodes"][0]
    c.update(
        paths=["C.py", "C.txt"],
        review_files=["input.csv"],
        input_files=["C.md"],
        goal_ids=["G-local"],
        assumptions=[dict(id="A-json", text="Input is JSON")],
        acceptance_conditions=[
            dict(
                id="AC-good",
                text="Transform the accepted input to good locally",
                goal_ids=["G-local"],
            )
        ],
    )
    c["validation"] = dict(
        commands=[
            [
                "python3",
                "-c",
                "from pathlib import Path; exec(Path('C.py').read_text()); assert transform(Path('input.csv').read_text()) == 'good'",
            ]
        ],
        artifacts=["C.txt"],
    )
    c["handoff"] = ["C.txt"]
    raw["nodes"][1]["depends_on"] = ["C"]
    return raw
