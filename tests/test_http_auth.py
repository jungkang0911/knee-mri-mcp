"""Streamable HTTP end to end: auth is required, bearer tokens work, and the claude.ai-style OAuth flow works."""

from __future__ import annotations

import base64
import hashlib
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse


import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from knee_mcp.server import make_app

TOKEN = "test-bearer-" + "x" * 24
PASSWORD = "correct horse battery staple"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live(cfg, service):
    cfg.port = _free_port()
    cfg.bearer_tokens = [TOKEN]
    cfg.owner_password = PASSWORD
    cfg.allowed_redirect_prefixes = [REDIRECT]
    app = make_app(cfg, service)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cfg.port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://localhost:{cfg.port}"
    srv.should_exit = True
    t.join(5)


def test_requires_auth(live):
    r = httpx2.post(f"{live}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")
    bad = httpx2.post(f"{live}/mcp", json={}, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


async def _call_get_slice(url: str, token: str):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120) as http:
        async with Client(streamable_http_client(f"{url}/mcp", http_client=http)) as c:
            return await c.call_tool("get_slice", {"study": "2019-03-20", "series": "S7", "index": 13})


async def test_bearer_token_get_slice_over_http(live):
    r = await _call_get_slice(live, TOKEN)
    assert not r.is_error
    assert r.content[0].type == "image" and r.content[0].mime_type == "image/png"


async def test_oauth_flow_like_claude_ai(live):
    async with httpx2.AsyncClient(base_url=live, follow_redirects=False) as h:
        meta = (await h.get("/.well-known/oauth-authorization-server")).json()
        assert meta["registration_endpoint"].endswith("/register")

        bad = await h.post("/register", json={"redirect_uris": ["https://evil.example/cb"], "client_name": "x"})
        assert bad.status_code == 400

        reg = (await h.post("/register", json={
            "redirect_uris": [REDIRECT], "client_name": "claude", "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        })).json()
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = await h.get("/authorize", params={
            "response_type": "code", "client_id": reg["client_id"], "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st1",
            "resource": f"{live}/mcp",
        })
        assert auth.status_code == 302
        login = urlparse(auth.headers["location"])
        req = parse_qs(login.query)["req"][0]

        wrong = await h.post("/login", data={"req": req, "password": "nope"})
        assert wrong.status_code == 401
        ok = await h.post("/login", data={"req": req, "password": PASSWORD})
        assert ok.status_code == 302
        cb = urlparse(ok.headers["location"])
        assert f"{cb.scheme}://{cb.netloc}{cb.path}" == REDIRECT
        q = parse_qs(cb.query)
        assert q["state"] == ["st1"]

        tok = (await h.post("/token", data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": reg["client_id"], "code_verifier": verifier, "resource": f"{live}/mcp",
        })).json()
        assert tok["token_type"] == "Bearer" and tok["refresh_token"]

        replay = await h.post("/token", data={
            "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": reg["client_id"], "code_verifier": verifier,
        })
        assert replay.status_code == 400

    r = await _call_get_slice(live, tok["access_token"])
    assert not r.is_error and r.content[0].type == "image"
