import pytest
from fastapi.testclient import TestClient

from app import auth, users
from app.auth import AuthError, LoginThrottle, hash_password, safe_next, verify_password
from app.main import create_app

from conftest import FakeRunner

PASSWORD = "correct horse battery"


@pytest.fixture
def secured(settings):
    settings.auth_enabled = True
    app = create_app(settings, FakeRunner())
    with TestClient(app, follow_redirects=False) as c:
        app.state.auth.create_user("alice", PASSWORD)
        yield c


def login(client, password=PASSWORD, username="alice", next_url="/"):
    body = f"username={username}&password={password}&next={next_url}"
    return client.post("/login", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})


def test_password_hash_roundtrip():
    stored = hash_password("s3cret-pass-word")
    assert stored.startswith("scrypt$") and "s3cret" not in stored
    assert verify_password("s3cret-pass-word", stored)
    assert not verify_password("other", stored)
    assert hash_password("s3cret-pass-word") != stored  # random salt
    for junk in ("", "plain", "scrypt$x$y", "md5$1$2$3$a$b", "scrypt$16384$8$1$!!$!!"):
        assert not verify_password("x", junk)


def test_password_and_username_rules(db):
    service = auth.AuthService(db)
    with pytest.raises(AuthError, match="at least"):
        service.create_user("bob", "short")
    with pytest.raises(AuthError, match="username"):
        service.create_user("has space", PASSWORD)
    service.create_user("bob", PASSWORD)
    with pytest.raises(AuthError, match="already exists"):
        service.create_user("BOB", PASSWORD)  # names are case-insensitive


@pytest.mark.parametrize("target,expected", [
    ("/tasks/abc?x=1", "/tasks/abc?x=1"), ("/", "/"), (None, "/"), ("", "/"),
    ("//evil.example", "/"), ("https://evil.example", "/"), ("/\\evil.example", "/"), ("evil", "/"),
    ("/login", "/"), ("/logout", "/"),
])
def test_safe_next(target, expected):
    assert safe_next(target) == expected


def test_throttle_locks_and_expires():
    now = [0.0]
    t = LoginThrottle(limit=3, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert t.retry_after("1.1.1.1", "alice") == 0
        t.failed("1.1.1.1", "alice")
    assert 0 < t.retry_after("1.1.1.1", "ALICE") <= 61
    assert t.retry_after("1.1.1.1", "bob") == 0       # another account from the same address is not locked
    assert t.retry_after("2.2.2.2", "alice") == 0     # nor the same account from another address
    now[0] = 61
    assert t.retry_after("1.1.1.1", "alice") == 0


def test_requires_login(secured):
    r = secured.get("/api/tasks")
    assert r.status_code == 401 and r.json()["detail"]["code"] == "auth_required"
    r = secured.get("/tasks/abc?x=1")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Ftasks%2Fabc%3Fx%3D1"
    assert secured.get("/").status_code == 303
    assert secured.post("/api/tasks/x/stop").status_code == 401
    assert secured.get("/api/fs").status_code == 401
    assert secured.get("/account").status_code == 303


def test_public_endpoints(secured):
    assert secured.get("/login").status_code == 200
    assert secured.get("/healthz").json() == {"ok": True}
    assert secured.get("/static/app.js").status_code == 200
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert secured.get(path).status_code in (401, 404, 303)  # the API description is not published


def test_login_logout_flow(secured):
    r = login(secured, next_url="/tasks/abc")
    assert r.status_code == 303 and r.headers["location"] == "/tasks/abc"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie  # plain http in tests
    assert secured.get("/api/tasks").status_code == 200
    page = secured.get("/")
    assert page.status_code == 200 and "alice" in page.text and "Sign out" in page.text
    assert secured.get("/login").status_code == 303   # already signed in

    assert secured.post("/logout").status_code == 303
    assert secured.get("/api/tasks").status_code == 401


def test_wrong_credentials(secured):
    for username, password in (("alice", "wrong-password"), ("nobody", PASSWORD)):
        r = login(secured, password=password, username=username)
        assert r.status_code == 401 and "Wrong username or password" in r.text
        assert "set-cookie" not in r.headers
    assert secured.get("/api/tasks").status_code == 401


def test_login_throttled_after_repeated_failures(secured):
    for _ in range(5):
        assert login(secured, password="bad-password-x").status_code == 401
    r = login(secured)  # even the right password is refused while locked out
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert secured.get("/api/tasks").status_code == 401


def test_login_next_cannot_leave_the_site(secured):
    r = login(secured, next_url="//evil.example")
    assert r.headers["location"] == "/"


def test_cross_origin_posts_are_refused(secured):
    login(secured)
    assert secured.post("/api/tasks/x/stop", headers={"Origin": "https://evil.example"}).status_code == 403
    assert secured.post("/logout", headers={"Origin": "null"}).status_code == 403
    assert secured.get("/api/tasks").status_code == 200  # still signed in
    # same origin, or behind a proxy that rewrote Host, is fine
    assert secured.post("/api/tasks/x/stop", headers={"Origin": "http://testserver"}).status_code != 403
    ok = secured.post("/api/tasks/x/stop", headers={"Origin": "https://gui.example.com", "X-Forwarded-Host": "gui.example.com"})
    assert ok.status_code != 403


def test_security_headers(secured):
    r = secured.get("/login")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in r.headers["content-security-policy"] and r.headers["cache-control"] == "no-store"
    assert "cache-control" not in secured.get("/static/app.js").headers
    assert "strict-transport-security" not in r.headers


def test_https_sets_secure_cookie_and_hsts(settings):
    settings.auth_enabled = True
    app = create_app(settings, FakeRunner())
    with TestClient(app, base_url="https://gui.example.com", follow_redirects=False) as c:
        app.state.auth.create_user("alice", PASSWORD)
        r = login(c)
        assert "Secure" in r.headers["set-cookie"] and "strict-transport-security" in r.headers


def test_expired_session_is_rejected(secured):
    token = secured.app.state.auth.start_session(1)
    secured.cookies.set(auth.COOKIE_NAME, token)
    assert secured.get("/api/tasks").status_code == 200
    secured.app.state.db._execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000Z'")
    assert secured.get("/api/tasks").status_code == 401


def test_session_token_is_not_stored_in_clear(secured):
    token = secured.app.state.auth.start_session(1)
    hashes = [r["token_hash"] for r in secured.app.state.db._conn.execute("SELECT token_hash FROM sessions")]
    assert token not in hashes


def test_change_password_signs_out_other_browsers(secured):
    login(secured)
    other = secured.app.state.auth.start_session(1)  # a second browser
    form = {"Content-Type": "application/x-www-form-urlencoded"}

    r = secured.post("/account/password", headers=form, content="current_password=nope&new_password=another long one&confirm_password=another long one")
    assert r.status_code == 400 and "incorrect" in r.text
    r = secured.post("/account/password", headers=form, content="current_password=%s&new_password=aaaaaaaaaaaa&confirm_password=bbbbbbbbbbbb" % PASSWORD)
    assert r.status_code == 400 and "do not match" in r.text
    r = secured.post("/account/password", headers=form, content="current_password=%s&new_password=brand+new+password&confirm_password=brand+new+password" % PASSWORD.replace(" ", "+"))
    assert r.status_code == 200 and "Password changed" in r.text

    assert secured.get("/api/tasks").status_code == 200            # this browser stays signed in
    assert secured.app.state.auth.user_for_token(other) is None    # the other one does not
    secured.post("/logout")
    assert login(secured).status_code == 401                       # old password no longer works
    assert login(secured, password="brand new password").status_code == 303


def test_auth_disabled_is_open(settings):
    with TestClient(create_app(settings, FakeRunner())) as c:  # Settings default: auth_enabled False
        assert c.get("/api/tasks").status_code == 200
        assert c.get("/").status_code == 200 and "Sign out" not in c.get("/").text
        assert c.post("/login", content="username=a&password=b").status_code in (200, 303)


def test_settings_from_env(monkeypatch):
    from app.config import Settings
    for var in ("CODEX_GUI_AUTH", "CODEX_GUI_COOKIE_SECURE", "CODEX_GUI_SESSION_HOURS"):
        monkeypatch.delenv(var, raising=False)
    s = Settings.from_env()
    assert s.auth_enabled and s.cookie_secure is None and s.session_hours == 168
    monkeypatch.setenv("CODEX_GUI_AUTH", "0")
    monkeypatch.setenv("CODEX_GUI_COOKIE_SECURE", "1")
    s = Settings.from_env()
    assert not s.auth_enabled and s.cookie_secure is True


def test_users_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_GUI_HOME", str(tmp_path / "gui"))
    monkeypatch.setenv("CODEX_GUI_NEW_PASSWORD", PASSWORD)
    assert users.main(["count"]) == 0 and capsys.readouterr().out.strip() == "0"
    assert users.main(["add", "carol"]) == 0
    assert users.main(["add", "carol"]) == 1 and "already exists" in capsys.readouterr().err
    monkeypatch.setenv("CODEX_GUI_NEW_PASSWORD", "tiny")
    assert users.main(["passwd", "carol"]) == 1 and "at least" in capsys.readouterr().err
    monkeypatch.setenv("CODEX_GUI_NEW_PASSWORD", "a much longer password")
    assert users.main(["passwd", "carol"]) == 0
    capsys.readouterr()
    assert users.main(["list"]) == 0 and capsys.readouterr().out.startswith("carol\t")
    assert users.main(["delete", "carol"]) == 0 and users.main(["delete", "carol"]) == 1
