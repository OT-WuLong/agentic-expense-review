$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
uv run --project $projectRoot uvicorn app.main:app --reload --reload-dir $projectRoot --app-dir $projectRoot
exit $LASTEXITCODE
