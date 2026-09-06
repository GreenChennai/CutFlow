# CutFlow 安装脚本:技能 junction 到用户级目录 + 体检
# 用法:powershell -ExecutionPolicy Bypass -File tools\install.ps1

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$skillRoot = Join-Path $env:USERPROFILE ".agents\skills"

Write-Host "==> CutFlow 安装" -ForegroundColor Cyan
Write-Host "仓库: $repo"
Write-Host "技能目录: $skillRoot"

if (-not (Test-Path $skillRoot)) { New-Item -ItemType Directory -Path $skillRoot | Out-Null }

# 同名遮蔽检查
foreach ($name in @("cutflow", "cutflow-prompt")) {
    $target = Join-Path $repo "skills\$name"
    $link = Join-Path $skillRoot $name
    if (Test-Path $link) {
        $item = Get-Item $link -Force
        if ($item.LinkType -eq "Junction" -and $item.Target -eq $target) {
            Write-Host "  [skip] $name 已指向本仓库" -ForegroundColor DarkGray
            continue
        }
        Write-Host "  [警告] $name 已存在且非本仓库 junction,请手动处理: $link" -ForegroundColor Yellow
        continue
    }
    cmd /c mklink /J "`"$link`"" "`"$target`"" | Out-Null
    Write-Host "  [ok] $name -> $target" -ForegroundColor Green
}

# 体检
Write-Host "==> 环境体检" -ForegroundColor Cyan
python (Join-Path $repo "skills\cutflow\scripts\rs_doctor.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "体检有致命失败:先填好 config.json(从 config.example.json 复制)再运行。" -ForegroundColor Yellow
} else {
    Write-Host "安装完成。" -ForegroundColor Green
}
