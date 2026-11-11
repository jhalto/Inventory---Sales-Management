$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath 'venv\Scripts\python.exe'
$managePath = Join-Path $projectPath 'manage.py'
& $pythonPath -B $managePath cleanup_auth_artifacts --verbosity 0
exit $LASTEXITCODE
