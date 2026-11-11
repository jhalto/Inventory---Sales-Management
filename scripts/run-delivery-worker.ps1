$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath 'venv\Scripts\python.exe'
& $pythonPath -B (Join-Path $projectPath 'manage.py') process_delivery_jobs --watch
exit $LASTEXITCODE
