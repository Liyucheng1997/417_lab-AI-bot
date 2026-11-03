# 发布新版本：先改 bot\__init__.py 里的 __version__，再运行
#   powershell -ExecutionPolicy Bypass -File release.ps1 [-Notes "这次改了什么"] [-SkipBuild]
# 会打包（build_exe.ps1）、打 git 标签、在 GitHub 创建 Release 并上传安装包和更新包。
# 已安装的 Bot 启动 10 秒后（或设置里点「检查更新」）就能看到新版本，一键更新。
param([string]$Notes = '', [switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$version = & .venv\Scripts\python.exe -c "from bot import __version__; print(__version__)"
$tag = "v$version"

if (-not $SkipBuild) { & "$PSScriptRoot\build_exe.ps1" }
$files = "dist\MyAIBot-Setup-$tag.exe", "dist\MyAIBot-Update-$tag.exe"
foreach ($f in $files) { if (-not (Test-Path $f)) { throw "缺少 $f，先运行 build_exe.ps1" } }

if (-not $Notes) { $Notes = "我的 AI Bot $tag" }
$Notes += "`n`n首次安装下载 MyAIBot-Setup-$tag.exe；已安装的版本在面板「设置 → 检查更新」里一键更新。"
gh release create $tag @files --title "我的 AI Bot $tag" --notes $Notes --target main
if ($LASTEXITCODE -ne 0) { throw 'gh release create 失败' }
Write-Host "已发布 $tag"
