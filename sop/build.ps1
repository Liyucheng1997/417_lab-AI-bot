# 编译 sop\bin\SOP.exe（只需要 Windows 自带的 .NET Framework 4.x，不用装 Visual Studio）
#   用法：powershell -ExecutionPolicy Bypass -File sop\build.ps1
#   编译时 SOP 会被关掉；之后在 Bot 托盘 → 截图与识字 → 重启模块 即可
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$dist = Join-Path $root 'bin'
$out = Join-Path $dist 'SOP.exe'
$ico = Join-Path $root 'src\sop.ico'
New-Item -ItemType Directory -Force $dist | Out-Null

# 正在运行的话先退出
$running = Get-Process SOP -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $out }
if ($running) { & $out quit; Start-Sleep -Milliseconds 800; $running | Stop-Process -Force -ErrorAction SilentlyContinue }

$refs = 'System.dll','System.Core.dll','System.Drawing.dll','System.Windows.Forms.dll','System.Web.Extensions.dll','System.Management.dll' |
    ForEach-Object { "/r:$_" }
$common = @('/nologo', '/target:winexe', '/optimize+', '/codepage:65001', "/out:$out",
            "/win32manifest:$root\src\app.manifest", "/recurse:$root\src\*.cs") + $refs

# 第一次编译没有图标 → 用程序自己画出图标 → 带图标再编译一次
if (-not (Test-Path $ico)) {
    & $csc @common
    if ($LASTEXITCODE -ne 0) { throw '编译失败' }
    & $out --make-icon $ico | Out-Null
    Start-Sleep -Milliseconds 300
}
& $csc @common "/win32icon:$ico"
if ($LASTEXITCODE -ne 0) { throw '编译失败' }
Write-Host "OK -> $out"
