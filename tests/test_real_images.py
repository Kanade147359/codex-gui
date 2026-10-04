"""Opt-in visual recognition with a separate CODEX_HOME and temporary GUI data/repository.

CODEX_GUI_REAL_IMAGES=1 pytest tests/test_real_images.py -s
Only the subscription auth file is copied; no production threads, database or config are changed.
"""
import asyncio
import io
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.task_manager import TaskManager
from conftest import wait_for

pytestmark = pytest.mark.skipif(os.environ.get("CODEX_GUI_REAL_IMAGES") != "1", reason="opt-in real Codex image recognition")


def labeled_image(label, color):
    image = Image.new("RGB", (600, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 580, 220), fill=color)
    draw.text((80, 90), label, font=ImageFont.truetype("DejaVuSans.ttf", 64), fill="white")
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


@pytest.mark.parametrize("backend", ["exec", "app-server"])
def test_real_new_and_continued_turn_recognize_their_own_images(settings, db, git_repo, monkeypatch, backend):
    original_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    auth = original_home / "auth.json"
    if not auth.is_file():
        pytest.skip("No file-based subscription auth available for an isolated CODEX_HOME")
    isolated_home = settings.home / "isolated-codex"
    isolated_home.mkdir(mode=0o700)
    shutil.copy2(auth, isolated_home / "auth.json")
    monkeypatch.setenv("CODEX_HOME", str(isolated_home))
    settings.backend = backend
    settings.codex_bin = os.environ.get("CODEX_BIN", "codex")
    m = TaskManager(settings, db)
    first = m.attachments.save(labeled_image("KUMO742", "red"), "参考 赤.png")
    second = m.attachments.save(labeled_image("TSUKI519", "blue"), "参考 青.png")
    prompt = "Read only the image attached to THIS message. Reply only with the exact letters and digits printed in it. Do not use any tools."

    async def scenario():
        try:
            task = await m.create_task(repository=str(git_repo), prompt=prompt, attachment_ids=[first["id"]],
                                       model=os.environ.get("CODEX_GUI_REAL_MODEL", ""), reasoning_effort="low",
                                       sandbox="read-only", auto_retry=False)
            tid = task["id"]
            async def idle():
                return await wait_for(lambda: m.get(tid)["status"] not in ("queued", "starting", "running") and m.get(tid), timeout=180)
            row = await idle()
            assert row["status"] == "completed", row["status_detail"]
            thread = row["codex_thread_id"]
            assert thread
            log = m.read_log(tid)[0]
            assert any("KUMO742" in e["message"] for e in log if "agent" in e["type"])
            offset = m.read_log(tid)[1]
            await m.send_instruction(tid, prompt, attachment_ids=[second["id"]])
            row = await idle()
            assert row["status"] == "completed", row["status_detail"]
            assert row["codex_thread_id"] == thread
            log = m.read_log(tid, offset)[0]
            assert any("TSUKI519" in e["message"] for e in log if "agent" in e["type"])
            assert [r["attachment_ids"] for r in db.list_messages(tid)] == [[first["id"]], [second["id"]]]
            print(f"{backend}: initial and resumed visual recognition passed on one isolated thread")
        finally:
            await m.shutdown()
    try:
        asyncio.run(scenario())
    finally:
        shutil.rmtree(isolated_home)
