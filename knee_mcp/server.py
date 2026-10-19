"""MCP server (official Python SDK, Streamable HTTP). Images are returned as ImageContent, never as text."""

from __future__ import annotations

import argparse
import base64
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Annotated

import anyio
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, Field

from .auth import SCOPE, OwnerAuthProvider
from .config import Config
from .errors import KneeError
from .service import KneeService, Rendered

log = logging.getLogger("knee_mcp")

INSTRUCTIONS = (
    "Read-only access to the owner's right-knee MRI / X-ray images stored in Google Drive. "
    "Start with list_studies, then list_series, then get_contact_sheet to pick images, then get_slice "
    "(optionally with crop/zoom) or compare across dates. Burned-in patient details are masked before "
    "any image is returned; an image that cannot be masked safely is withheld."
)

Crop = Annotated[
    list[int] | None,
    Field(description="[x0, y0, x1, y1] in original-image pixels (see 'size' in get_slice output)"),
]
SeriesArg = Annotated[str | int, Field(description="Series number, e.g. '7', 'S7' or 'S07' (S4 and S04 are the same)")]
StudyArg = Annotated[str, Field(description="study_id from list_studies (e.g. 2019-03-20_TSGH) or just the date")]


class SliceRef(BaseModel):
    study: str
    series: str | int
    index: int
    crop: list[int] | None = None


def _img(r: Rendered) -> ImageContent:
    return ImageContent(data=base64.b64encode(r.png).decode("ascii"), mime_type="image/png")


def _err(message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(text=message)], is_error=True)


async def _run(fn, *args) -> CallToolResult:
    """Run blocking work off the event loop; turn failures into safe error results."""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args))
    except KneeError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001 - never leak paths or PII from unexpected exceptions
        ref = secrets.token_hex(4)
        log.error("tool failure ref=%s type=%s", ref, type(e).__name__)
        return _err(f"internal error (ref {ref}); see server log")


def build_server(cfg: Config, service: KneeService, provider: OwnerAuthProvider | None = None) -> MCPServer:
    auth = None
    if provider is not None:
        base = cfg.public_url or f"http://localhost:{cfg.port}"
        auth = AuthSettings(
            issuer_url=base,
            resource_server_url=f"{base}/mcp",
            validate_token_resource=False,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=bool(cfg.owner_password), valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
    mcp = MCPServer(
        name="knee-imaging",
        instructions=INSTRUCTIONS,
        version="0.1.0",
        auth_server_provider=provider,
        auth=auth,
    )

    if provider is not None:
        mcp.custom_route("/login", methods=["GET", "POST"])(provider.login_page)

    @mcp.tool(description="List imaging studies (date, hospital, type, series count) plus naming issues.")
    async def list_studies() -> CallToolResult:
        return await _run(lambda: CallToolResult(content=[TextContent(text=service.list_studies())]))

    @mcp.tool(description="List series in a study: series number, image count, plane/sequence and SL range read from the overlay.")
    async def list_series(study: StudyArg) -> CallToolResult:
        return await _run(lambda: CallToolResult(content=[TextContent(text=service.list_series(study))]))

    @mcp.tool(description="Thumbnail grid of a whole series in image-number order; each tile is labelled with its image number.")
    async def get_contact_sheet(
        study: StudyArg, series: SeriesArg, cols: Annotated[int, Field(ge=1, le=10)] = 5
    ) -> CallToolResult:
        def go():
            r = service.contact_sheet(study, series, cols)
            return CallToolResult(content=[_img(r), TextContent(text=r.caption)])

        return await _run(go)

    @mcp.tool(description="One image (PNG) plus a caption line with plane, SL and ST. Optional crop and zoom.")
    async def get_slice(
        study: StudyArg,
        series: SeriesArg,
        index: Annotated[int, Field(description="Image number, e.g. 13 for I0013")],
        crop: Crop = None,
        zoom: Annotated[float, Field(ge=0.25, le=4)] = 1,
    ) -> CallToolResult:
        def go():
            r = service.slice(study, series, index, crop, zoom)
            return CallToolResult(content=[_img(r), TextContent(text=r.caption)])

        return await _run(go)

    @mcp.tool(description="Up to 8 consecutive images (by image number, inclusive range), each followed by its caption.")
    async def get_slices(study: StudyArg, series: SeriesArg, start: int, end: int, crop: Crop = None) -> CallToolResult:
        def go():
            content: list = []
            for r in service.slices(study, series, start, end, crop):
                content += ([_img(r)] if r.png else []) + [TextContent(text=r.caption)]
            return CallToolResult(content=content)

        return await _run(go)

    @mcp.tool(description="Two images side by side (e.g. 2019 vs 2024), each labelled with date and image number.")
    async def compare(a: SliceRef, b: SliceRef) -> CallToolResult:
        def go():
            r = service.compare(a.model_dump(), b.model_dump())
            return CallToolResult(content=[_img(r), TextContent(text=r.caption)])

        return await _run(go)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request):
        from starlette.responses import PlainTextResponse

        return PlainTextResponse("ok")

    return mcp


def make_app(cfg: Config, service: KneeService):
    if not cfg.bearer_tokens and not cfg.owner_password:
        raise SystemExit("Refusing to start without auth: set KNEE_BEARER_TOKENS and/or KNEE_OWNER_PASSWORD")
    base = cfg.public_url or f"http://localhost:{cfg.port}"
    provider = OwnerAuthProvider(
        cfg.cache_dir / "auth.json", cfg.owner_password, cfg.bearer_tokens, cfg.allowed_redirect_prefixes,
        login_url=f"{base}/login",
    )
    mcp = build_server(cfg, service, provider)
    hosts = [f"localhost:{cfg.port}", f"127.0.0.1:{cfg.port}", *cfg.allowed_hosts]
    if cfg.public_url:
        hosts.append(cfg.public_url.split("://", 1)[1])
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[cfg.public_url, "https://claude.ai", "https://claude.com", f"http://localhost:{cfg.port}",
                         "http://localhost:6274", "http://127.0.0.1:6274"],
    )
    return mcp.streamable_http_app(stateless_http=True, transport_security=security, host=cfg.host)


def _safe_warm_index(service: KneeService) -> None:
    try:
        service.catalog.index()
    except Exception as e:  # noqa: BLE001 - e.g. Drive not authorized yet; tools report it later
        log.warning("initial Drive listing failed: %s", type(e).__name__)


def _load_dotenv(path: Path) -> None:
    """Minimal .env support (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def setup() -> Config:
    """Logging, .env, Tesseract path and cache dir; shared by the server and `knee_mcp.prebuild`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _load_dotenv(Path(".env"))
    cfg = Config.from_env()
    if cfg.tesseract_cmd:
        import pytesseract

        pytesseract.pytesseract.tesseract_cmd = cfg.tesseract_cmd
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def build_service(cfg: Config) -> KneeService:
    from .drive import LazyGoogleDrive, LocalFolderSource

    local_dir = os.environ.get("KNEE_LOCAL_DIR")
    if local_dir:
        log.warning("serving images from KNEE_LOCAL_DIR instead of Google Drive (development mode)")
        source = LocalFolderSource(Path(local_dir))
    else:
        source = LazyGoogleDrive(cfg.root_folder_id, cfg.google_client_secrets, cfg.google_token_file)
    return KneeService(cfg, source)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="knee-mcp")
    parser.add_argument("--warm", action="store_true", help="same as `python -m knee_mcp.prebuild`")
    parser.add_argument("--gen-token", action="store_true", help="print a random bearer token and exit")
    args = parser.parse_args(argv)

    if args.gen_token:
        print(secrets.token_urlsafe(32))
        return 0
    if args.warm:
        from .prebuild import main as prebuild_main

        return prebuild_main()

    cfg = setup()
    service = build_service(cfg)

    import threading

    import uvicorn

    # Build the Drive index right away so the first tool call does not wait ~15 s for it.
    threading.Thread(target=lambda: _safe_warm_index(service), daemon=True).start()

    uvicorn.run(make_app(cfg, service), host=cfg.host, port=cfg.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
