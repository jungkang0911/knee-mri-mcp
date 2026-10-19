"""Single-owner authorization.

* Static bearer tokens (KNEE_BEARER_TOKENS) for Claude Code / MCP Inspector / scripts.
* A minimal OAuth 2.1 authorization server (dynamic client registration + PKCE, enforced by the SDK)
  for claude.ai custom connectors. Its /authorize step shows a password page; only someone who knows
  KNEE_OWNER_PASSWORD can mint tokens. Tokens are stored hashed in <cache>/auth.json.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CODE_TTL = 300
PENDING_TTL = 600
SCOPE = "knee.read"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class OwnerAuthProvider:
    def __init__(
        self,
        state_file: Path,
        owner_password: str,
        bearer_tokens: list[str],
        allowed_redirect_prefixes: list[str],
        login_url: str,
    ) -> None:
        self.state_file = state_file
        self.owner_password = owner_password
        self.bearer_hashes = [_h(t) for t in bearer_tokens]
        self.allowed_redirects = allowed_redirect_prefixes
        self.login_url = login_url
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: list[float] = []
        self._state = {"clients": {}, "access": {}, "refresh": {}}
        if state_file.exists():
            self._state.update(json.loads(state_file.read_text("utf-8")))

    def _save(self) -> None:
        now = time.time()
        for kind in ("access", "refresh"):
            self._state[kind] = {k: v for k, v in self._state[kind].items() if (v.get("expires_at") or now + 1) > now}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self.state_file)

    # ---- clients ---------------------------------------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._state["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self.owner_password:
            raise RegistrationError("invalid_client_metadata", "OAuth login is disabled on this server")
        for uri in client_info.redirect_uris or []:
            if not any(str(uri).startswith(p) for p in self.allowed_redirects):
                raise RegistrationError("invalid_redirect_uri", "redirect_uri is not allowed by this server")
        with self._lock:
            self._state["clients"][client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
            self._save()

    # ---- authorization ------------------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.owner_password:
            raise AuthorizeError("access_denied", "OAuth login is disabled on this server")
        req = secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self._pending = {k: v for k, v in self._pending.items() if v[0] > now}
            self._pending[req] = (now + PENDING_TTL, client.client_id, params)
        return f"{self.login_url}?req={req}"

    async def login_page(self, request: Request) -> Response:
        if request.method == "GET":
            req = request.query_params.get("req", "")
            return _page(req, "")
        form = await request.form()
        req = str(form.get("req", ""))
        password = str(form.get("password", ""))
        with self._lock:
            now = time.time()
            self._failures = [t for t in self._failures if t > now - 900]
            if len(self._failures) >= 5:
                return _page(req, "Too many attempts. Try again in 15 minutes.", status=429)
            pending = self._pending.get(req)
            if not pending or pending[0] < now:
                return _page("", "This sign-in request expired. Start again from your MCP client.", status=400)
            if not hmac.compare_digest(_h(password), _h(self.owner_password)):
                self._failures.append(now)
                return _page(req, "Wrong password.", status=401)
            del self._pending[req]
            _, client_id, params = pending
            code = secrets.token_urlsafe(32)
            self._codes[code] = AuthorizationCode(
                code=code,
                scopes=[SCOPE],
                expires_at=now + CODE_TTL,
                client_id=client_id,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject="owner",
            )
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state), 302)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        code = self._codes.get(authorization_code)
        if code and code.client_id == client.client_id and code.expires_at > time.time():
            return code
        return None

    def _issue(self, client_id: str, resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        rec = {"client_id": client_id, "scopes": [SCOPE], "resource": resource}
        with self._lock:
            self._state["access"][_h(access)] = {**rec, "expires_at": now + ACCESS_TTL}
            self._state["refresh"][_h(refresh)] = {**rec, "expires_at": now + REFRESH_TTL}
            self._save()
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=SCOPE, refresh_token=refresh)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        rec = self._state["refresh"].get(_h(refresh_token))
        if not rec or rec["client_id"] != client.client_id or rec["expires_at"] < time.time():
            return None
        return RefreshToken(token=refresh_token, client_id=rec["client_id"], scopes=rec["scopes"],
                            expires_at=rec["expires_at"], resource=rec.get("resource"), subject="owner")

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self._lock:
            self._state["refresh"].pop(_h(refresh_token.token), None)  # rotate
        return self._issue(client.client_id, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        th = _h(token)
        if any(hmac.compare_digest(th, b) for b in self.bearer_hashes):
            return AccessToken(token=token, client_id="static-bearer", scopes=[SCOPE], subject="owner")
        rec = self._state["access"].get(th)
        if not rec or rec["expires_at"] < time.time():
            return None
        return AccessToken(token=token, client_id=rec["client_id"], scopes=rec["scopes"],
                           expires_at=rec["expires_at"], resource=rec.get("resource"), subject="owner")

    async def revoke_token(self, token) -> None:
        with self._lock:
            self._state["access"].pop(_h(token.token), None)
            self._state["refresh"].pop(_h(token.token), None)
            self._save()

    async def exchange_identity_assertion(self, client, params) -> OAuthToken:
        raise TokenError("unsupported_grant_type", "not supported")


def _page(req: str, message: str, status: int = 200) -> HTMLResponse:
    msg = f'<p class="msg">{html.escape(message)}</p>' if message else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Knee MCP sign-in</title>
<style>body{{font-family:system-ui,sans-serif;max-width:360px;margin:15vh auto;padding:0 16px}}
input,button{{font-size:16px;width:100%;padding:10px;margin-top:8px;box-sizing:border-box}}.msg{{color:#b00}}</style>
</head><body><h1>Knee imaging MCP</h1><p>Enter the owner password to allow this client to read your knee images.</p>{msg}
<form method="post" action="login"><input type="hidden" name="req" value="{html.escape(req)}">
<input type="password" name="password" autocomplete="current-password" autofocus required>
<button type="submit">Allow</button></form></body></html>"""
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
