# Start knee-mcp from the repo folder (reads .env there). Used by the scheduled task too.
# Logs: knee-mcp.err.log (server log, stderr) and knee-mcp.out.log.
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
Start-Process -FilePath (Join-Path $repo ".venv\Scripts\python.exe") -ArgumentList "-m", "knee_mcp" `
    -WorkingDirectory $repo -NoNewWindow -Wait `
    -RedirectStandardOutput (Join-Path $repo "knee-mcp.out.log") `
    -RedirectStandardError (Join-Path $repo "knee-mcp.err.log")
