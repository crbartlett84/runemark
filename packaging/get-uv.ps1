# Downloads uv (a small Python installer and package manager, by Astral) into vendor\uv and
# prints the path to uv.exe. uv sets up Python 3.12 just for Runemark, so Python doesn't
# have to be installed on the PC. The download is the official wheel from PyPI, pinned and
# checked against its SHA-256. Safe to run again.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar makes downloads very slow in Windows PowerShell
$Root = Split-Path -Parent $PSScriptRoot
$Version = "0.12.23"
$Url = "https://files.pythonhosted.org/packages/af/fd/816f4e8a76197312ec8037369e5b5c8a5ebde7aa118b6250259e031d3444/uv-0.12.23-py3-none-win_amd64.whl"
$Sha256 = "fb8a4117a5224d73abe2204a1e744ae34b544f1b9a0b761576851deaa7215c12"
$Target = Join-Path $Root "vendor\uv"
$Exe = Join-Path $Target "uv.exe"

if (-not (Test-Path $Exe)) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $Zip = Join-Path $env:TEMP "uv-$Version-win_amd64.zip"   # a wheel is a zip file
    Write-Host "Downloading uv $Version..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
    $Actual = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
    if ($Actual -ne $Sha256) {
        Remove-Item $Zip -Force
        throw "uv download hash mismatch (got $Actual). Not using it."
    }
    $Unpack = Join-Path $env:TEMP "uv-$Version-unpack"
    if (Test-Path $Unpack) { Remove-Item -Recurse -Force $Unpack }
    Expand-Archive -Path $Zip -DestinationPath $Unpack
    Copy-Item (Join-Path $Unpack "uv-$Version.data\scripts\uv.exe") $Exe
    Remove-Item -Recurse -Force $Unpack
    Remove-Item -Force $Zip
}
$Exe
