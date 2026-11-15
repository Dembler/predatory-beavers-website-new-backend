# Restartable local backend; invoked hidden at Windows sign-in.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
Set-Location -LiteralPath $taskRoot
$listener = Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue
if ($listener) { exit 0 }
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw 'The project Python environment is missing.'
}
$taskLogs = Join-Path $taskRoot 'data'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
Start-Process -FilePath $taskPython -ArgumentList 'run.py' -WorkingDirectory $taskRoot `
    -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogs 'api-autostart.out.log') `
    -RedirectStandardError (Join-Path $taskLogs 'api-autostart.err.log') | Out-Null
