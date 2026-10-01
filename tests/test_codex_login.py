import time

import pytest
from fastapi.testclient import TestClient

from app.codex_login import CodexLogin, _web_url
from app.main import create_app

from conftest import FakeAppServer, FakeRunner


@pytest.fixture
def make_client(settings, fake_codex_state, monkeypatch):
    """make(login="ok"|"wait"|"fail"): a GUI whose fake Codex is signed out. The fake reads its mode at spawn time."""
    opened = []

    def make(login="ok"):
        monkeypatch.setenv("FAKE_ACCOUNT", "none")
        monkeypatch.setenv("FAKE_LOGIN", login)
        settings.backend = "app-server"
        server = FakeAppServer("fake", True)
        app = create_app(settings, FakeRunner(), server)
        server.on_global(app.state.manager._on_global_notification)
        c = TestClient(app)
        c.__enter__()
        opened.append(c)
        return c

    yield make
    for c in opened:
        c.__exit__(None, None, None)


def wait_for_status(client, status, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        a = client.get("/api/codex/account").json()
        if a["login"]["status"] == status:
            return a
        time.sleep(0.05)
    raise AssertionError(f"login never became {status}: {a}")


def test_signed_out_then_browser_login(make_client):
    client = make_client("ok")
    a = client.get("/api/codex/account").json()
    assert a["signed_in"] is False and a["account_type"] is None and a["login"] == {"status": "idle"}

    r = client.post("/api/codex/login", json={"method": "browser"})
    # the fake finishes at once, so the answer is either the pending login (with its page) or, if the completion
    # notice arrived first, already the finished one
    assert r.status_code == 200 and (r.json()["status"] == "success" or r.json()["url"].startswith("https://auth.example.com/authorize"))
    a = wait_for_status(client, "success")
    assert a["signed_in"] is True and a["email"] == "me@example.com" and a["plan"] == "pro"


def test_device_code_is_shown_while_waiting_and_can_be_cancelled(make_client):
    client = make_client("wait")
    r = client.post("/api/codex/login", json={"method": "device"}).json()
    assert r["status"] == "pending" and r["user_code"] == "ABCD-1234" and r["url"] == "https://auth.example.com/device"
    a = client.get("/api/codex/account").json()
    assert a["signed_in"] is False and a["login"]["status"] == "pending" and a["login"]["user_code"] == "ABCD-1234"
    assert client.post("/api/codex/login/cancel").json() == {"status": "idle"}
    assert client.get("/api/codex/account").json()["login"] == {"status": "idle"}


def test_failed_login_reports_the_reason(make_client):
    client = make_client("fail")
    client.post("/api/codex/login", json={"method": "browser"})
    a = wait_for_status(client, "failed")
    assert a["login"]["error"] == "access_denied" and a["signed_in"] is False


def test_restarting_login_replaces_the_pending_one(make_client):
    client = make_client("wait")
    first = client.post("/api/codex/login", json={"method": "browser"}).json()
    second = client.post("/api/codex/login", json={"method": "device"}).json()
    assert second["login_id"] != first["login_id"] and second["method"] == "device"


def test_rejects_unknown_method(make_client):
    assert make_client().post("/api/codex/login", json={"method": "password"}).status_code == 422


def test_api_key_login_still_counts_as_signed_out_when_subscription_only(settings, fake_codex_state, monkeypatch):
    monkeypatch.setenv("FAKE_ACCOUNT", "apiKey")
    settings.backend = "app-server"
    app = create_app(settings, FakeRunner(), FakeAppServer("fake", True))
    with TestClient(app) as c:
        a = c.get("/api/codex/account").json()
        assert a["account_type"] == "apiKey" and a["signed_in"] is False


def test_notification_before_reply_is_not_lost():
    login = CodexLogin(lambda: None)
    login.on_notification("account/login/completed", {"loginId": "L1", "success": False, "error": "access_denied"})
    assert login.login == {"status": "idle"}          # not our login yet
    assert login._completed["L1"] == {"status": "failed", "error": "access_denied"}


def test_only_web_urls_are_passed_on():
    assert _web_url("https://auth.openai.com/x") and _web_url("http://localhost:1455/auth")
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "", None, 5):
        assert _web_url(bad) is None
