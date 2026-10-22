# Downloads the pinned FFmpeg build (gyan.dev "essentials", via its GitHub release), checks its
# SHA-256, and puts ffmpeg.exe and ffprobe.exe in vendor\ffmpeg. Safe to run again.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar makes downloads very slow in Windows PowerShell
$Root = Split-Path -Parent $PSScriptRoot
$Version = "7.1.1"
$Url = "https://github.com/GyanD/codexffmpeg/releases/download/$Version/ffmpeg-$Version-essentials_build.zip"
$Sha256 = "04861d3339c5ebe38b56c19a15cf2c0cc97f5de4fa8910e4d47e5e6404e4a2d4"
$Target = Join-Path $Root "vendor\ffmpeg"

if ((Test-Path (Join-Path $Target "ffmpeg.exe")) -and (Test-Path (Join-Path $Target "ffprobe.exe"))) {
    Write-Host "FFmpeg already in $Target"
    exit 0
}
New-Item -ItemType Directory -Force -Path $Target | Out-Null
$Zip = Join-Path $env:TEMP "ffmpeg-$Version-essentials_build.zip"
Write-Host "Downloading FFmpeg $Version..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $Url -OutFile $Zip -UseBasicParsing
$Actual = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
if ($Actual -ne $Sha256) {
    Remove-Item $Zip -Force
    throw "FFmpeg download hash mismatch (got $Actual). Not using it."
}
$Unpack = Join-Path $env:TEMP "ffmpeg-$Version-unpack"
if (Test-Path $Unpack) { Remove-Item -Recurse -Force $Unpack }
Expand-Archive -Path $Zip -DestinationPath $Unpack
$Bin = Join-Path $Unpack "ffmpeg-$Version-essentials_build\bin"
Copy-Item (Join-Path $Bin "ffmpeg.exe"), (Join-Path $Bin "ffprobe.exe") $Target
Copy-Item (Join-Path $Unpack "ffmpeg-$Version-essentials_build\LICENSE") (Join-Path $Target "LICENSE.txt")
Remove-Item -Recurse -Force $Unpack
Remove-Item -Force $Zip
Write-Host "FFmpeg ready in $Target"
