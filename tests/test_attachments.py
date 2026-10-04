"""Real uploads/SQLite/task lifecycle; fake Codex only, temporary data and repositories."""
import asyncio
import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.attachments import AttachmentError, MAX_IMAGE_BYTES, inspect_image
from app.codex_runner import CodexRunner
from app.database import Database
from app.main import create_app
from app.task_manager import _Turn
from conftest import FakeRunner
from test_codex_runner import task


def image_bytes(fmt="PNG", size=(24, 16), color="red"):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, format=fmt)
    return data.getvalue()


class ImageRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def check_image_support(self, backend, resume=False):
        pass

    async def spawn(self, task, resume_thread=None):
        self.sent.append((task["id"], resume_thread, list(task.get("image_paths", []))))
        return await super().spawn(task, resume_thread)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, ImageRunner())) as c:
        yield c


def upload(client, data=None, name="参考 スクリーンショット.png"):
    response = client.post("/api/attachments", params={"filename": name}, content=data or image_bytes())
    assert response.status_code == 200, response.text
    return response.json()


def done(client, tid):
    from test_api import wait_done
    return wait_done(client, tid)


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_upload_validates_real_format_not_filename_or_mime(client, fmt, mime):
    data = image_bytes(fmt)
    a = upload(client, data, "C:\\日本語\\../参考 fake.exe")
    assert a["media_type"] == mime and a["width"] == 24 and a["height"] == 16
    assert a["filename"] == "参考 fake.exe"
    response = client.get(a["url"])
    assert response.content == data and response.headers["content-type"] == mime
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("data,reason", [(b"broken", "破損"), (image_bytes("GIF"), "対応形式"),
                                          (image_bytes(size=(8193, 1)), "寸法"), (b"", "空")])
def test_invalid_images_are_rejected_without_files(client, data, reason):
    response = client.post("/api/attachments", content=data)
    assert response.status_code == 400 and reason in response.json()["detail"]["message"]
    assert list(client.app.state.manager.attachments.root.iterdir()) == []


def test_truncated_pixels_and_animation_rejected():
    data = image_bytes("JPEG", (100, 100))
    with pytest.raises(AttachmentError):
        inspect_image(data[:-20])
    out = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(out, format="PNG", save_all=True,
                                           append_images=[Image.new("RGB", (10, 10), "blue")])
    with pytest.raises(AttachmentError, match="アニメーション"):
        inspect_image(out.getvalue())


def test_upload_size_limit_and_untrusted_ids(client, git_repo):
    assert client.post("/api/attachments", content=b"x" * (MAX_IMAGE_BYTES + 1)).status_code == 413
    a = upload(client)
    for ids in (["../../etc/passwd"], ["0" * 32], [a["id"]] * 2, [a["id"]] * 9):
        response = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "attachment_ids": ids})
        assert response.status_code == 400 and response.json()["detail"]["code"] == "invalid_attachment"
    assert client.get("/api/tasks").json()["tasks"] == []


def test_initial_followup_parallel_tasks_history_and_restart(settings, git_repo):
    runner = ImageRunner()
    with TestClient(create_app(settings, runner)) as c:
        # The file chosen in a Windows browser is uploaded as bytes; no client path reaches Codex.
        original = settings.home / "original.png"
        original.write_bytes(image_bytes())
        first = upload(c, original.read_bytes())
        original.unlink()
        second = upload(c, image_bytes("WEBP"), "second.webp")
        third = upload(c, image_bytes("JPEG"), "third.jpg")
        tid = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "attachment_ids": [second["id"], first["id"]]}).json()["id"]
        other = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "attachment_ids": [third["id"]]}).json()["id"]
        row = done(c, tid)
        assert done(c, other)["status"] == "completed"
        thread = row["codex_thread_id"]
        assert c.post(f"/api/tasks/{tid}/messages", json={"prompt": "ok", "attachment_ids": [third["id"]]}).status_code == 200
        assert done(c, tid)["codex_thread_id"] == thread
        assert c.post(f"/api/tasks/{tid}/messages", json={"prompt": "ok"}).status_code == 200
        assert done(c, tid)["status"] == "completed"
        paths = lambda ids: c.app.state.manager.attachments.resolve(ids)
        assert [entry for entry in runner.sent if entry[0] == tid] == [
            (tid, None, paths([second["id"], first["id"]])), (tid, thread, paths([third["id"]])), (tid, thread, [])]
        assert [entry for entry in runner.sent if entry[0] == other] == [(other, None, paths([third["id"]]))]
        messages = c.get(f"/api/tasks/{tid}").json()["messages"]
        assert [[a["id"] for a in m["attachments"]] for m in messages] == [[second["id"], first["id"]], [third["id"]], []]
        # Neither deleting a worktree nor another composer removing its preview can delete shared images.
        c.post(f"/api/tasks/{other}/commit", json={"message": "test"})
        assert c.delete(f"/api/tasks/{other}/worktree").status_code == 200
        assert c.get(third["url"]).status_code == 200
    with TestClient(create_app(settings, ImageRunner())) as c:
        assert c.get(f"/api/tasks/{tid}").json()["messages"] == messages
        assert c.get(first["url"]).content == image_bytes()


def test_missing_image_is_not_silently_omitted(client, git_repo):
    a = upload(client)
    tid = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()["id"]
    done(client, tid)
    store = client.app.state.manager.attachments
    store.path(store.get(a["id"])).unlink()
    response = client.post(f"/api/tasks/{tid}/messages", json={"prompt": "keep this input", "attachment_ids": [a["id"]]})
    assert response.status_code == 400 and "読み取れません" in response.json()["detail"]["message"]
    assert len(client.get(f"/api/tasks/{tid}").json()["messages"]) == 1


def test_cli_argv_places_resume_images_after_subcommand_and_handles_special_paths():
    paths = ["/tmp/日本語 空白/one.png", "/tmp/x;$(echo unsafe)/two.webp"]
    for thread in (None, "specific-thread"):
        cmd = CodexRunner().build_command(task(image_paths=paths), thread)
        assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--image"] == paths
        assert cmd[-2:] == ["--", "-"]
        if thread:
            assert cmd[cmd.index("resume") + 1] == thread
            assert cmd.index("resume") < cmd.index("--image")
        assert not any("dangerously" in arg for arg in cmd)


def test_capability_probe_rejects_old_cli_and_windows_cli(tmp_path):
    cli = tmp_path / "old codex"
    cli.write_text('#!/bin/sh\nprintf "Usage: codex exec without images\\n"\n')
    cli.chmod(0o700)
    async def scenario():
        for resume in (True, False):
            with pytest.raises(AttachmentError, match="対応していません"):
                await CodexRunner(str(cli)).check_image_support("exec", resume)
        with pytest.raises(AttachmentError, match="WSL"):
            await CodexRunner("codex.exe").check_image_support("exec")
    asyncio.run(scenario())


def test_unsupported_cli_preserves_task_and_upload(settings, git_repo):
    runner = ImageRunner()
    async def unsupported(*args, **kwargs):
        raise AttachmentError("CLIは画像入力に対応していません")
    runner.check_image_support = unsupported
    with TestClient(create_app(settings, runner)) as c:
        a = upload(c)
        response = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "keep", "attachment_ids": [a["id"]]})
        assert response.status_code == 400 and "対応" in response.json()["detail"]["message"]
        assert c.get(a["url"]).status_code == 200
        assert c.get("/api/tasks").json()["tasks"] == []


def test_turn_recovery_keeps_images_only_until_instruction_confirmed():
    from app.task_manager import TaskManager
    pending = _Turn("request", attachment_ids=["a"], service_tier="default")
    old = _Turn.from_json('{"prompt":"old", "resume_thread":"thread"}')
    assert old.attachment_ids == []
    assert _Turn.from_json(pending.to_json()).attachment_ids == ["a"]
    assert TaskManager._recovery_turn(pending, "auto_retry").attachment_ids == ["a"]
    pending.thread_id = "thread"
    retry = TaskManager._recovery_turn(pending, "auto_retry")
    assert retry.resume_thread == "thread" and retry.attachment_ids == ["a"]
    pending.started = True
    assert TaskManager._recovery_turn(pending, "auto_retry").attachment_ids == []


def test_old_scheduled_schema_is_migrated_idempotently(tmp_path):
    from app.database import SCHEMA
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO scheduled_instructions (task_id,prompt,status,created_at) VALUES ('old','old','cancelled','now')")
    con.commit()
    con.close()
    for _ in range(2):
        db = Database(path)
        assert json.loads(db.get_scheduled(1)["attachment_ids"]) == []
        assert db.list_messages("old") == []
        db.close()


def test_app_server_start_resume_and_steer_images(git_repo, make_manager, fake_codex_state):
    from test_app_server import calls, finished, running
    m = make_manager(backend="app-server")
    async def supported(*args, **kwargs):
        pass
    m.runner.check_image_support = supported
    a = m.attachments.save(image_bytes(), "a.png")
    b = m.attachments.save(image_bytes("WEBP"), "b.webp")
    async def scenario():
        t = await m.create_task(repository=str(git_repo), prompt="ok", attachment_ids=[a["id"]])
        thread = (await finished(m, t["id"]))["codex_thread_id"]
        await m.send_instruction(t["id"], "sleep", attachment_ids=[b["id"]])
        await running(m, t["id"])
        await m.send_instruction(t["id"], "steered", attachment_ids=[a["id"], b["id"]])
        await m.stop(t["id"])
        await finished(m, t["id"])
        starts = calls(fake_codex_state, "turn/start")
        assert [r["params"]["threadId"] for r in starts] == [thread, thread]
        assert [[i["path"] for i in r["params"]["input"] if i["type"] == "localImage"] for r in starts] == [
            m.attachments.resolve([a["id"]]), m.attachments.resolve([b["id"]])]
        steered = calls(fake_codex_state, "turn/steer")[-1]["params"]
        assert steered["threadId"] == thread
        assert [i["path"] for i in steered["input"] if i["type"] == "localImage"] == m.attachments.resolve([a["id"], b["id"]])
        assert m.db.list_messages(t["id"])[-1]["attachment_ids"] == [a["id"], b["id"]]
        await m.shutdown()
    asyncio.run(scenario())


def test_scheduled_followup_and_dependent_initial_images_survive_restart(settings, git_repo):
    from test_ui_recovery import wait_status
    with TestClient(create_app(settings, ImageRunner())) as c:
        a, b = upload(c), upload(c, image_bytes("WEBP"), "b.webp")
        target = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()["id"]
        thread = done(c, target)["codex_thread_id"]
        dep = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "sleep"}).json()["id"]
        wait_status(c, dep, "running")
        scheduled = c.post(f"/api/tasks/{target}/scheduled", json={"prompt": "ok", "depends_on": [dep],
                                                                 "attachment_ids": [b["id"], a["id"]]}).json()
        waiting = c.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "depends_on": [dep],
                                              "attachment_ids": [a["id"]]}).json()["id"]
        assert scheduled["status"] == "waiting_dependencies"
    runner = ImageRunner()
    with TestClient(create_app(settings, runner)) as c:
        row = c.get(f"/api/tasks/{target}").json()["scheduled_instructions"][0]
        assert [a["id"] for a in row["attachments"]] == [b["id"], a["id"]]
        assert c.post(f"/api/tasks/{dep}/messages", json={"prompt": "ok"}).status_code == 200
        done(c, dep)
        wait_status(c, waiting, "completed")
        import time
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            row = c.get(f"/api/tasks/{target}").json()["scheduled_instructions"][0]
            if row["status"] == "completed":
                break
            time.sleep(.05)
        assert row["status"] == "completed"
        store = c.app.state.manager.attachments
        assert (target, thread, store.resolve([b["id"], a["id"]])) in runner.sent
        assert (waiting, None, store.resolve([a["id"]])) in runner.sent
        assert len([s for s in runner.sent if s[0] == target]) == 1
        history = c.get(f"/api/tasks/{target}").json()["messages"]
        assert len(history) == 2 and history[-1]["attachment_ids"] == [b["id"], a["id"]]


def test_scheduled_missing_image_fails_without_retry_or_releasing_dependents(client, git_repo):
    from test_ui_recovery import wait_status
    a = upload(client)
    target = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"}).json()["id"]
    done(client, target)
    gate = client.app.state.settings.home / "open-gate"
    dep = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": f"gate {gate}"}).json()["id"]
    wait_status(client, dep, "running")
    client.post(f"/api/tasks/{target}/scheduled", json={"prompt": "keep", "depends_on": [dep], "attachment_ids": [a["id"]]})
    store = client.app.state.manager.attachments
    store.path(store.get(a["id"])).write_bytes(b"corrupt")
    gate.touch()
    done(client, dep)
    row = wait_status(client, target, "failed")
    assert row["last_failure_kind"] == "invalid_attachment" and row["retry_count"] == 0
    assert json.loads(client.app.state.manager.db.get_task(target)["pending_turn"])["attachment_ids"] == [a["id"]]
    downstream = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "depends_on": [target]}).json()
    assert downstream["status"] == "blocked"


def test_message_capacity_and_pixel_limits_are_checked_by_backend(client, git_repo, monkeypatch):
    import app.attachments as attachments
    a = upload(client)
    monkeypatch.setattr(attachments, "MAX_MESSAGE_BYTES", a["size"] - 1)
    response = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok", "attachment_ids": [a["id"]]})
    assert response.status_code == 400 and "合計上限" in response.json()["detail"]["message"]
    monkeypatch.setattr(attachments, "MAX_PIXELS", 10)
    with pytest.raises(AttachmentError, match="寸法"):
        inspect_image(image_bytes())


def test_rejected_concurrent_message_does_not_write_history(client, git_repo):
    from test_ui_recovery import wait_status
    a = upload(client)
    tid = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "sleep"}).json()["id"]
    wait_status(client, tid, "running")
    response = client.post(f"/api/tasks/{tid}/messages", json={"prompt": "keep", "attachment_ids": [a["id"]]})
    assert response.status_code == 409  # the exec backend still waits for the current turn
    assert len(client.get(f"/api/tasks/{tid}").json()["messages"]) == 1
    assert client.get(a["url"]).status_code == 200


@pytest.mark.parametrize("prompt,reattach", [("crashearly", True), ("crashunstarted", True), ("crash", False)])
def test_process_failure_retry_restores_only_unconfirmed_images(git_repo, make_manager, prompt, reattach):
    from conftest import wait_for
    m = make_manager(backend="exec", retry_backoff_seconds=(.02,))
    m.runner = ImageRunner()
    a = m.attachments.save(image_bytes(), "retry.png")
    async def scenario():
        m.scheduler.start(.02)
        try:
            t = await m.create_task(repository=str(git_repo), prompt=prompt, attachment_ids=[a["id"]])
            await wait_for(lambda: m.get(t["id"])["status"] == "completed", timeout=15)
            paths = m.attachments.resolve([a["id"]])
            assert [r[2] for r in m.runner.sent] == [paths, paths if reattach else []]
            assert len(m.db.list_messages(t["id"])) == 1  # recovery isn't a second user message
            if prompt != "crashearly":
                assert m.runner.sent[1][1] == m.get(t["id"])["codex_thread_id"]
        finally:
            await m.shutdown()
    asyncio.run(scenario())


def test_racing_scheduled_claim_records_ordered_images_exactly_once(db, make_manager):
    from concurrent.futures import ThreadPoolExecutor
    from test_scheduled_instructions import row, THREAD
    m = make_manager()
    row(db, "x", thread=THREAD)
    ids = [m.attachments.save(image_bytes(fmt), fmt)["id"] for fmt in ("JPEG", "PNG")]
    reservation = db.create_scheduled("x", "ok", "default", attachment_ids=ids)
    db.advance_scheduled(reservation["id"])
    turn = _Turn("ok", resume_thread=THREAD, trigger="scheduled_instruction", attachment_ids=ids)
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: db.claim_scheduled(reservation["id"], m._queue_fields(turn), THREAD), range(10)))
    assert results.count(True) == 1
    assert [r["attachment_ids"] for r in db.list_messages("x")] == [ids]
    assert _Turn.from_json(db.get_task("x")["pending_turn"]).attachment_ids == ids


def test_image_only_initial_and_followup(client, git_repo):
    a = upload(client)
    response = client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "", "attachment_ids": [a["id"]]})
    assert response.status_code == 200
    tid = response.json()["id"]
    thread = done(client, tid)["codex_thread_id"]
    assert client.post(f"/api/tasks/{tid}/messages", json={"prompt": "", "attachment_ids": [a["id"]]}).status_code == 200
    assert done(client, tid)["codex_thread_id"] == thread


def test_upload_id_collision_never_deletes_an_existing_image(client, monkeypatch):
    import uuid
    import app.attachments as attachments
    a = upload(client)
    store = client.app.state.manager.attachments
    original = store.path(store.get(a["id"])).read_bytes()
    monkeypatch.setattr(attachments.uuid, "uuid4", lambda: uuid.UUID(hex=a["id"]))
    with pytest.raises(FileExistsError):
        store.save(image_bytes(color="blue"), "collision.png")
    assert store.path(store.get(a["id"])).read_bytes() == original
    assert store.get(a["id"])["filename"] == a["filename"]
