# Builds Runemark for Windows: dist\Runemark\Runemark.exe and dist\Runemark-<version>-win64.zip
# Usage (from the project folder):   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#   or just double-click "Build Runemark.cmd"
#   -SkipTests   don't run the test suite first
# Python doesn't need to be installed: uv fetches a private Python 3.12 for the build.
# Everything shown is also saved to build-log.txt in the project folder.
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Log = Join-Path $Root "build-log.txt"
try { Stop-Transcript | Out-Null } catch { }
Start-Transcript -Path $Log -Force | Out-Null

function Find-SignedPython {
    # Python 3.12 or 3.13 from python.org (the py launcher, or the usual install folders)
    foreach ($v in @("3.12", "3.13")) {
        try {
            $exe = (& py "-$v" -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe)) { return $exe.Trim() }
        } catch { }
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "$env:ProgramFiles\Python313\python.exe")
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

function Invoke-Native {
    # Runs a program through cmd.exe so its error output is merged as plain text, shows and logs
    # every line, and stops the build if the program fails.
    param([string]$What, [string]$Exe, [string[]]$Arguments)
    $quoted = ($Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + $_ + '"' } else { $_ } }) -join " "
    $line = "`"$Exe`" $quoted 2>&1"
    $blocked = $false
    & cmd.exe /d /s /c "`"$line`"" | ForEach-Object {
        if ("$_" -match "blocked by your organization's Device Guard policy") { $blocked = $true }
        Write-Host $_
    }
    if ($blocked) {
        Write-Host ""
        Write-Host "Windows blocked the Python that builds Runemark (Smart App Control or App Control)." -ForegroundColor Yellow
        Write-Host "It only allows programs with a known publisher. To fix it, install Python 3.12 from" -ForegroundColor Yellow
        Write-Host "https://www.python.org/downloads/ (it's signed), then run Build Runemark.cmd again;" -ForegroundColor Yellow
        Write-Host "the build switches to it automatically. Runemark.exe itself isn't affected." -ForegroundColor Yellow
    }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

try {
    $Uv = (& (Join-Path $PSScriptRoot "get-uv.ps1") | Select-Object -Last 1)
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
    $Cfg = Join-Path $Root ".venv\pyvenv.cfg"

    # A Python installed from python.org is signed, so Windows Smart App Control / App Control
    # lets it run. Prefer it over the unsigned copy uv downloads when it's there.
    $System = Find-SignedPython
    $UsesUvPython = (Test-Path $Cfg) -and ((Get-Content $Cfg -Raw) -match "\\uv\\python\\")
    if ($System -and $UsesUvPython) {
        Write-Host "Switching the build to the Python installed from python.org: $System"
        Remove-Item -Recurse -Force (Join-Path $Root ".venv")
    }
    if (-not (Test-Path $Python)) {
        if ($System) {
            Write-Host "Setting up Runemark's build environment from $System (first run only)..."
            Invoke-Native "Setting up Python" $System @("-m", "venv", ".venv")
        } else {
            Write-Host "Setting up Python 3.12 for Runemark (first run only)..."
            Invoke-Native "Setting up Python" $Uv @("venv", ".venv", "--python", "3.12")
        }
    }
    Write-Host "Installing packages..."
    Invoke-Native "Installing packages" $Uv @("pip", "install", "--quiet", "--python", $Python, "-r", "requirements-dev.txt")

    & (Join-Path $PSScriptRoot "get-ffmpeg.ps1")
    $env:PATH = (Join-Path $Root "vendor\ffmpeg") + ";" + $env:PATH

    if (-not $SkipTests) {
        Write-Host "Running tests..."
        Invoke-Native "The tests" $Python @("-m", "pytest", "-q", "-p", "no:cacheprovider")
    }

    Write-Host "Building the app..."
    if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
    # Portable mode keeps the tag database beside the app: set it aside while the app is rebuilt
    $Keep = Join-Path $Root "portable-data-kept"
    $Kept = @()
    foreach ($item in @("Runemark data", "Runemark-portable.txt")) {
        $src = Join-Path "dist\Runemark" $item
        if (Test-Path $src) {
            if (-not (Test-Path $Keep)) { New-Item -ItemType Directory $Keep | Out-Null }
            Move-Item $src (Join-Path $Keep $item)
            $Kept += $item
            Write-Host "Keeping your portable data: $item"
        }
    }
    if (Test-Path "dist\Runemark") { Remove-Item -Recurse -Force "dist\Runemark" }
    try {
        Invoke-Native "PyInstaller" $Python @("-m", "PyInstaller", "packaging\runemark.spec", "--noconfirm",
                                              "--distpath", "dist", "--workpath", "build", "--log-level", "WARN")
    } finally {
        foreach ($item in $Kept) {
            if (-not (Test-Path "dist\Runemark")) { New-Item -ItemType Directory "dist\Runemark" | Out-Null }
            Move-Item (Join-Path $Keep $item) (Join-Path "dist\Runemark" $item)
        }
        if ((Test-Path $Keep) -and -not (Get-ChildItem $Keep)) { Remove-Item $Keep }
    }

    Write-Host "Self-test of the built app..."
    $Report = Join-Path $Root "dist\selftest.txt"
    $proc = Start-Process -FilePath "dist\Runemark\Runemark.exe" -ArgumentList "--self-test", "`"$Report`"" -Wait -PassThru
    if (Test-Path $Report) { Get-Content -Encoding UTF8 $Report | ForEach-Object { Write-Host $_ } }
    if ($proc.ExitCode -ne 0) { Write-Warning "Some self-test checks failed; see the report above." }

    $Version = (& $Python -c "import runemark; print(runemark.__version__)").Trim()
    $ZipPath = "dist\Runemark-$Version-win64.zip"
    if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
    # The zip is the app only: never your portable tag database
    Get-ChildItem "dist\Runemark" | Where-Object { $_.Name -notin @("Runemark data", "Runemark-portable.txt") } |
        Compress-Archive -DestinationPath $ZipPath
    Write-Host ""
    Write-Host "Done: dist\Runemark\Runemark.exe  (zip: $ZipPath)"
    $code = 0
} catch {
    Write-Host ""
    Write-Host "BUILD FAILED: $_" -ForegroundColor Red
    $code = 1
} finally {
    Stop-Transcript | Out-Null
}
exit $code
