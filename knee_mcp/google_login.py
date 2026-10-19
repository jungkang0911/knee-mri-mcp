"""One-time Google sign-in for Drive (drive.readonly). Run on a machine with a browser:

    python -m knee_mcp.google_login

Reads GOOGLE_CLIENT_SECRETS (OAuth client JSON, type "Desktop app") and writes GOOGLE_TOKEN_FILE.
"""

from __future__ import annotations

import sys

from google_auth_oauthlib.flow import InstalledAppFlow

from .config import Config
from .drive import SCOPES


def main() -> int:
    cfg = Config.from_env()
    if not cfg.google_client_secrets or not cfg.google_client_secrets.exists():
        print("Set GOOGLE_CLIENT_SECRETS to the OAuth client JSON downloaded from Google Cloud Console.")
        return 2
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.google_client_secrets), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    assert cfg.google_token_file is not None
    cfg.google_token_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.google_token_file.write_text(creds.to_json(), encoding="utf-8")
    print("Saved Drive token (drive.readonly) to the configured token file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
