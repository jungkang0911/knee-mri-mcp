"""Runtime configuration, read from environment variables (see README)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass
class Config:
    root_folder_id: str = ""
    cache_dir: Path = Path(".cache")
    # Google OAuth (installed-app) files for the Drive API, drive.readonly only.
    google_client_secrets: Path | None = None
    google_token_file: Path | None = None
    # MCP server
    host: str = "127.0.0.1"
    port: int = 8765
    public_url: str = ""  # e.g. https://knee.example.com  (no trailing slash)
    allowed_hosts: list[str] = field(default_factory=list)
    bearer_tokens: list[str] = field(default_factory=list)
    owner_password: str = ""  # enables the built-in OAuth login (for claude.ai connectors)
    allowed_redirect_prefixes: list[str] = field(default_factory=list)
    # Imaging / OCR
    tesseract_cmd: str = ""
    ocr_langs: str = "eng"
    pii_terms: list[str] = field(default_factory=list)  # optional exact strings that must never survive
    study_types: dict[str, str] = field(default_factory=dict)  # "2018-12-31" -> "X-ray"
    listing_ttl: float = 300.0
    max_png_bytes: int = 1_000_000
    workers: int = 4
    exclude_folders: list[str] = field(default_factory=list)  # skip images under folders containing these
    tool_budget: float = 25.0  # seconds a tool waits for first-time redaction before answering 'still masking'
    keep_raw: bool = False  # cache unmasked originals on disk (off: download, redact, discard)

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ
        study_types: dict[str, str] = {}
        for item in _split(env.get("KNEE_STUDY_TYPES")):
            if "=" in item:
                k, v = item.split("=", 1)
                study_types[k.strip()] = v.strip()
        secrets = env.get("GOOGLE_CLIENT_SECRETS")
        token = env.get("GOOGLE_TOKEN_FILE")
        cache_dir = Path(env.get("KNEE_CACHE_DIR", ".cache")).resolve()
        return cls(
            root_folder_id=env.get("KNEE_ROOT_FOLDER_ID", ""),
            cache_dir=cache_dir,
            google_client_secrets=Path(secrets) if secrets else None,
            google_token_file=Path(token) if token else cache_dir / "google-token.json",
            host=env.get("KNEE_HOST", "127.0.0.1"),
            port=int(env.get("KNEE_PORT", "8765")),
            public_url=env.get("KNEE_PUBLIC_URL", "").rstrip("/"),
            allowed_hosts=_split(env.get("KNEE_ALLOWED_HOSTS")),
            bearer_tokens=_split(env.get("KNEE_BEARER_TOKENS")),
            owner_password=env.get("KNEE_OWNER_PASSWORD", ""),
            allowed_redirect_prefixes=_split(env.get("KNEE_ALLOWED_REDIRECTS"))
            or [
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
                "https://chatgpt.com/connector/oauth/",
                "https://chatgpt.com/connector_platform_oauth_redirect",
                "http://localhost:",
                "http://127.0.0.1:",
            ],
            tesseract_cmd=env.get("TESSERACT_CMD", ""),
            ocr_langs=env.get("KNEE_OCR_LANGS", "eng"),
            pii_terms=_split(env.get("KNEE_PII_TERMS")),
            study_types=study_types,
            listing_ttl=float(env.get("KNEE_LISTING_TTL", "300")),
            workers=int(env.get("KNEE_WORKERS", "4")),
            exclude_folders=_split(env.get("KNEE_EXCLUDE_FOLDERS")),
            tool_budget=float(env.get("KNEE_TOOL_BUDGET", "25")),
            keep_raw=env.get("KNEE_KEEP_RAW", "0").lower() in ("1", "true", "yes"),
        )
