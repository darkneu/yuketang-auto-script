param([switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '请先创建 .venv：python -m venv .venv' }
# Confined to this build process; do not change global environment settings.
$env:TEMP = Join-Path $PSScriptRoot '.build_tmp/tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
if ($InstallDependencies) {
    & $python -m pip install -r requirements-desktop.txt
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
}
$savedBuildPath = $env:PATH
$package = Join-Path $PSScriptRoot 'dist/YuKeTang'
$targetExe = Join-Path $package 'YuKeTang.exe'
$runningTarget = @(Get-Process -Name YuKeTang -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $targetExe })
if ($runningTarget.Count -gt 0) { throw '目标 YuKeTang.exe 正在运行，请关闭本项目程序后重新打包' }
$runtimeData = Join-Path $package 'data'
$backupRoot = Join-Path $PSScriptRoot ('.build_tmp/runtime-backup-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $runtimeData) {
    New-Item -ItemType Directory -Path $backupRoot | Out-Null
    Copy-Item -LiteralPath $runtimeData -Destination $backupRoot -Recurse -Force
}
try {
    # Do not resolve Qt's system ICU import against unrelated tools on the caller's PATH.
    $pythonBase = & $python -c 'import sys; print(sys.base_prefix)'
    $env:PATH = @((Split-Path -Parent $python), $pythonBase,
        (Join-Path $pythonBase 'DLLs'), (Join-Path $env:SystemRoot 'System32'), $env:SystemRoot) -join ';'
    & $python -m PyInstaller --noconfirm desktop.spec
    if ($LASTEXITCODE -ne 0) { throw '桌面打包失败' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'DESKTOP.md') -Destination $package
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination $package
    if (Test-Path -LiteralPath $runtimeData) { throw '打包目录含运行数据，已取消压缩' }
    $archive = Join-Path $PSScriptRoot 'dist/YuKeTang-Windows-x64.zip'
    Compress-Archive -LiteralPath $package -DestinationPath $archive -Force
    Write-Output "软件包：$archive"
} finally {
    $env:PATH = $savedBuildPath
    $backupData = Join-Path $backupRoot 'data'
    if (Test-Path -LiteralPath $backupData) {
        New-Item -ItemType Directory -Force -Path $package | Out-Null
        Copy-Item -LiteralPath $backupData -Destination $package -Recurse -Force
    }
}
