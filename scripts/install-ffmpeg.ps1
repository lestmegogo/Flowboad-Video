$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $repoRoot "tools"
$ffmpegDir = Join-Path $toolsDir "ffmpeg"
$zipPath = Join-Path $env:TEMP "flowboard-ffmpeg-release-essentials.zip"
$downloadUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
Write-Host "Downloading FFmpeg..."
Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath

if (Test-Path $ffmpegDir) {
    Remove-Item -LiteralPath $ffmpegDir -Recurse -Force
}

Expand-Archive -LiteralPath $zipPath -DestinationPath $toolsDir -Force
$extracted = Get-ChildItem -LiteralPath $toolsDir -Directory |
    Where-Object { $_.Name -like "ffmpeg-*essentials_build*" } |
    Select-Object -First 1

if ($null -eq $extracted) {
    throw "Extracted FFmpeg directory was not found."
}

Move-Item -LiteralPath $extracted.FullName -Destination $ffmpegDir
$ffmpegExe = Join-Path $ffmpegDir "bin\ffmpeg.exe"
$ffprobeExe = Join-Path $ffmpegDir "bin\ffprobe.exe"
if (!(Test-Path $ffmpegExe) -or !(Test-Path $ffprobeExe)) {
    throw "FFmpeg installation is incomplete."
}

Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
Write-Host "FFmpeg installed at $ffmpegExe"
