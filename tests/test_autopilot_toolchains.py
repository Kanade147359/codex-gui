"""Opt-in installed toolchain build, always offline and in the real validator sandbox."""

import asyncio
import os
import shutil
import subprocess

import pytest

from app.autopilot.policy import profile
from app.autopilot.runner import Validator


def test_build_outputs_persist_across_required_commands_without_source_writes(tmp_path):
    async def scenario():
        source = tmp_path / "source"
        source.mkdir()
        registered = profile(
            dict(
                outputs=["build"],
                commands=[
                    ["python3", "-c", "from pathlib import Path; Path('build/result').write_text('compiled')"],
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; assert Path('build/result').read_text() == 'compiled'",
                    ],
                ],
            ),
            "code",
        )
        result = await Validator().validate(
            dict(payload=dict(scope={"tree_sha": "fixed"})), source, registered, tmp_path / "logs"
        )
        assert result["state"] == "PASS"
        assert list((source / "build").iterdir()) == []

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.environ.get("CODEX_GUI_AUTOPILOT_TOOLCHAIN_SMOKE") != "1",
    reason="opt-in installed Rust/Node/Python offline sandbox build",
)
def test_installed_rust_node_python_builds_use_registered_runtime_and_scratch(tmp_path):
    async def scenario():
        if not shutil.which("rustc") or not shutil.which("node"):
            pytest.skip("installed toolchains unavailable")
        rust = subprocess.check_output(["rustc", "--print", "sysroot"], text=True).strip()
        source = tmp_path / "source"
        source.mkdir()
        (source / "src").mkdir()
        (source / "src/main.rs").write_text('fn main() { println!("offline-rust-ok"); }\n')
        (source / "Cargo.toml").write_text('[package]\nname="autopilot_probe"\nversion="0.1.0"\nedition="2021"\n')
        # Cargo can use an existing lockfile without writing a read-only source tree.
        (source / "Cargo.lock").write_text('version = 4\n\n[[package]]\nname = "autopilot_probe"\nversion = "0.1.0"\n')
        registered = profile(
            dict(
                outputs=["build"],
                commands=[
                    ["cargo", "build", "--frozen", "--offline", "--target-dir", "/scratch/target"],
                    ["/scratch/target/debug/autopilot_probe"],
                    ["node", "-e", "require('fs').writeFileSync('build/node-result','offline-node-ok')"],
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; assert Path('build/node-result').read_text() == 'offline-node-ok'; assert not Path('/home/kanade/.local/share/codex-gui').exists()",
                    ],
                ],
                environment=dict(
                    offline=True,
                    mounts=[dict(source=rust, target="/opt/validation/rust")],
                    toolchains=[
                        dict(command=["rustc", "--version"], contains="rustc"),
                        dict(command=["node", "--version"], contains="v"),
                        dict(command=["python3", "--version"], contains="Python"),
                    ],
                    env=dict(PATH="/opt/validation/rust/bin:/usr/local/bin:/usr/bin:/bin"),
                ),
            ),
            "code",
        )
        result = await Validator().validate(
            dict(payload=dict(scope={"tree_sha": "installed-toolchain-smoke"})), source, registered, tmp_path / "logs"
        )
        assert result["state"] == "PASS", result
        assert not (source / "target").exists()
        assert list((source / "build").iterdir()) == []

    asyncio.run(scenario())


def test_build_output_symlink_cannot_make_controller_write_outside_scratch(tmp_path):
    async def scenario():
        from app.autopilot.runner import InfrastructureUnavailable

        source = tmp_path / "source"
        source.mkdir()
        forbidden = tmp_path / "forbidden-host-directory"
        # The first sandbox writes only a symlink string; the host must not follow it on the next command.
        attack = (
            "from pathlib import Path; p=Path('/scratch/outputs/build'); p.rename('/scratch/previous-output'); p.symlink_to("
            + repr(str(forbidden))
            + ", target_is_directory=True)"
        )
        registered = profile(
            dict(outputs=["build"], commands=[["python3", "-c", attack], ["python3", "-c", "print(1)"]]), "code"
        )
        with pytest.raises(InfrastructureUnavailable, match="symlink"):
            await Validator().validate(dict(payload=dict(scope={})), source, registered, tmp_path / "logs")
        assert not forbidden.exists()

    asyncio.run(scenario())
