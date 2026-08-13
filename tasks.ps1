<#
.SYNOPSIS
    Task runner for nl2api (PowerShell).

.EXAMPLE
    .\tasks.ps1 setup     # create venv and install everything
    .\tasks.ps1 check     # lint + format + tests (what CI runs)
    .\tasks.ps1 api       # run the mock business API on :8000
    .\tasks.ps1 assistant # run the assistant API on :8001
    .\tasks.ps1 ui        # run the Streamlit UI on :8501
#>
param(
    [Parameter(Position = 0)]
    [ValidateSet('setup', 'lint', 'fmt', 'test', 'check', 'api', 'assistant', 'ui', 'clean', 'help')]
    [string]$Task = 'help'
)

$ErrorActionPreference = 'Stop'
$Python = if (Test-Path '.\.venv\Scripts\python.exe') { '.\.venv\Scripts\python.exe' } else { 'python' }

function Invoke-Setup {
    if (-not (Test-Path '.\.venv')) { python -m venv .venv }
    & '.\.venv\Scripts\python.exe' -m pip install --upgrade pip
    & '.\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt
    & '.\.venv\Scripts\python.exe' -m pip install -e .
    if (-not (Test-Path '.env')) { Copy-Item .env.example .env }
    Write-Host 'Setup complete. Activate with: .\.venv\Scripts\Activate.ps1'
}

switch ($Task) {
    'setup'     { Invoke-Setup }
    'lint'      { & $Python -m ruff check . }
    'fmt'       { & $Python -m ruff format .; & $Python -m ruff check --fix . }
    'test'      { & $Python -m pytest -q }
    'check'     {
        & $Python -m ruff check .;      if ($LASTEXITCODE) { exit $LASTEXITCODE }
        & $Python -m ruff format --check .; if ($LASTEXITCODE) { exit $LASTEXITCODE }
        & $Python -m pytest -q
    }
    'api'       { & $Python -m uvicorn nl2api.mock_api.main:app --reload --port 8000 }
    'assistant' { & $Python -m uvicorn nl2api.service.main:app --reload --port 8001 }
    'ui'        { & $Python -m streamlit run src/nl2api/ui/app.py }
    'clean'     {
        Get-ChildItem -Recurse -Directory -Include __pycache__, .pytest_cache, .ruff_cache, .mypy_cache |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item nl2api.db, .coverage -Force -ErrorAction SilentlyContinue
    }
    default     { Get-Help $PSCommandPath -Detailed }
}
