# CutFlow installer: junction skills into user-level dir + doctor check.
# Usage: powershell -ExecutionPolicy Bypass -File tools\install.ps1
# NOTE: keep this file ASCII-only (PowerShell 5.1 misparses BOM-less UTF-8 with CJK).

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$skillRoot = Join-Path $env:USERPROFILE ".agents\skills"

Write-Host "==> Installing CutFlow skills" -ForegroundColor Cyan
Write-Host "Repo: $repo"
Write-Host "Skill root: $skillRoot"

if (-not (Test-Path $skillRoot)) { New-Item -ItemType Directory -Path $skillRoot | Out-Null }

foreach ($name in @("cutflow", "cutflow-prompt")) {
    $target = Join-Path $repo "skills\$name"
    $link = Join-Path $skillRoot $name
    if (Test-Path $link) {
        $item = Get-Item $link -Force
        if ($item.LinkType -eq "Junction") {
            Write-Host "  [skip] $name already linked: $link" -ForegroundColor DarkGray
            continue
        }
        Write-Host "  [warn] $name exists and is not a junction, handle manually: $link" -ForegroundColor Yellow
        continue
    }
    cmd /c mklink /J "`"$link`"" "`"$target`"" | Out-Null
    Write-Host "  [ok] $name -> $target" -ForegroundColor Green
}

Write-Host "==> Doctor check" -ForegroundColor Cyan
$doctor = Join-Path $repo "skills\cutflow\scripts\rs_doctor.py"
python $doctor
if ($LASTEXITCODE -ne 0) {
    Write-Host "Doctor has fatal failures. Fill config.json (copy from config.example.json) first." -ForegroundColor Yellow
    exit 1
}
Write-Host "Install done." -ForegroundColor Green
