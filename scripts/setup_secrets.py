#!/usr/bin/env python3
"""Generate a local bearer token without printing it or placing it in a URL."""
from pathlib import Path
import os,secrets
folder=Path('.secrets');folder.mkdir(mode=0o700,exist_ok=True)
path=folder/'mri_token'
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(48)+'\n')
print('Created .secrets/mri_token. Do not commit or paste the token into chat.')
if hasattr(os,'getuid'):print(f'For compose: MRI_UID={os.getuid()} MRI_GID={os.getgid()}')
