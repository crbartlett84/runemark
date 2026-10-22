# Runs Runemark straight from the source folder (sets everything up the first time).
# Usage:  powershell -ExecutionPolicy Bypass -File run.ps1 [video files...]
#   or just double-click "Run Runemark.cmd"
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Uv = (& (Join-Path $PSScriptRoot "packaging\get-uv.ps1") | Select-Object -Last 1)
$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    & $Uv venv .venv --python 3.12
    if ($LASTEXITCODE -ne 0) { throw "Couldn't set up Python" }
}
& $Uv pip install --python $Python -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "Installing packages failed" }
& (Join-Path $PSScriptRoot "packaging\get-ffmpeg.ps1")
& (Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe") -m runemark @args
