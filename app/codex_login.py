"""Sign in to Codex (ChatGPT account) from the GUI, through the shared `codex app-server`.

Same as `codex login` / `codex login --device-auth`, which store the credentials in Codex's own home; nothing is kept
here. Protocol (checked against codex-cli 0.159.2 with `codex app-server generate-json-schema`):

* `account/read`                       -> {"account": null | {"type": "chatgpt", "email", "planType"} | {"type": "apiKey"}}
* `account/login/start` {"type": "chatgpt"}            -> {"loginId", "authUrl"}: open authUrl in a browser; the
                                          sign-in page calls back to a localhost port of the machine Codex runs on
* `account/login/start` {"type": "chatgptDeviceCode"}  -> {"loginId", "verificationUrl", "userCode"}: works from any
                                          browser; type the code on the page
* notification `account/login/completed` {"loginId", "success", "error"}
* `account/login/cancel` {"loginId"}

The GUI never signs in with an API key and never handles a password or token: the browser talks to OpenAI directly.
"""
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

from .appserver import AppServerClient, AppServerError

METHODS = {"browser": "chatgpt", "device": "chatgptDeviceCode"}


class LoginError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _web_url(value) -> Optional[str]:
    """Only an http(s) URL is ever handed to the page, whatever the server says."""
    return value if isinstance(value, str) and urlsplit(value).scheme in ("http", "https") else None


class CodexLogin:
    def __init__(self, get_client: Callable[[], Awaitable[AppServerClient]], subscription_only: bool = True):
        self._get_client = get_client
        self.subscription_only = subscription_only
        self.login: dict = {"status": "idle"}   # idle | pending | success | failed
        self._completed: dict[str, dict] = {}    # loginId -> outcome, for a completion that beats the start reply

    def on_notification(self, method: str, params: dict) -> None:
        if method != "account/login/completed" or not params.get("loginId"):
            return
        outcome = {"status": "success"} if params.get("success") else {
            "status": "failed", "error": str(params.get("error") or "sign-in failed")}
        self._completed = {**dict(list(self._completed.items())[-9:]), params["loginId"]: outcome}
        if self.login.get("login_id") == params["loginId"]:  # not a login we already replaced or cancelled
            self.login = outcome

    async def _request(self, method: str, params: Optional[dict] = None) -> dict:
        try:
            client = await self._get_client()
            result = await client.request(method, params or {}, timeout=20)
        except AppServerError as e:
            raise LoginError(f"cannot reach codex app-server: {e}", 503)
        return result if isinstance(result, dict) else {}

    async def status(self) -> dict:
        info = await self._request("account/read", {"refreshToken": False})
        account = info.get("account") if isinstance(info.get("account"), dict) else None
        kind = account.get("type") if account else None
        # With subscription-only (the default) tasks run only on a ChatGPT sign-in, so an API-key login still needs one.
        ok = kind == "chatgpt" or (kind is not None and not self.subscription_only)
        return {"signed_in": ok, "account_type": kind, "email": (account or {}).get("email"),
                "plan": (account or {}).get("planType"), "login": self.login}

    async def start(self, method: str) -> dict:
        if method not in METHODS:
            raise LoginError("method must be 'browser' or 'device'")
        if self.login.get("status") == "pending":
            await self.cancel()
        result = await self._request("account/login/start", {"type": METHODS[method]})
        url = _web_url(result.get("authUrl") or result.get("verificationUrl"))
        if not url or not result.get("loginId"):
            raise LoginError("Codex did not return a sign-in page", 502)
        self.login = {"status": "pending", "login_id": result["loginId"], "method": method, "url": url}
        if result.get("userCode"):
            self.login["user_code"] = str(result["userCode"])
        if result["loginId"] in self._completed:  # it finished before this reply was handled (e.g. failed at once)
            self.login = self._completed[result["loginId"]]
        return self.login

    async def cancel(self) -> dict:
        login_id = self.login.get("login_id")
        if self.login.get("status") == "pending" and login_id:
            try:
                await self._request("account/login/cancel", {"loginId": login_id})
            except LoginError:
                pass  # the server may already be gone; the state is reset either way
        self.login = {"status": "idle"}
        return self.login
