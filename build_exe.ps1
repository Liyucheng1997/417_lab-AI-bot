# 打包成安装包：powershell -ExecutionPolicy Bypass -File build_exe.ps1
#   产物：dist\MyAIBot-Setup-v<版本>.exe   完整安装包（默认装到 D:\my softwares\My AI Bot）
#         dist\MyAIBot-Update-v<版本>.exe  更新包（不含 OCR 引擎，客户端内更新下载的就是它）
#   需要 Inno Setup 6（choco install innosetup）。发布到 GitHub：release.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = '.venv\Scripts\python.exe'
$version = & $py -c "from bot import __version__; print(__version__)"
New-Item -ItemType Directory -Force build | Out-Null

# 图标 + exe 版本信息
& $py -c @"
from bot.brand import save_ico
save_ico('build/icon.ico')
v = tuple(int(x) for x in '$version'.split('.')) + (0,)
open('build/version.txt', 'w', encoding='utf-8').write(f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}),
  kids=[StringFileInfo([StringTable('080404B0', [
    StringStruct('ProductName', '我的 AI Bot'), StringStruct('FileDescription', '我的 AI Bot'),
    StringStruct('FileVersion', '$version'), StringStruct('ProductVersion', '$version'),
    StringStruct('OriginalFilename', 'MyAIBot.exe')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])])''')
"@
if ($LASTEXITCODE -ne 0) { throw '生成图标失败' }

& $py -m PyInstaller MyAIBot.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 失败' }

# exe 旁边放可编辑的配置和截图识字模块（安装包从这里取文件）
$out = 'dist\MyAIBot'
Copy-Item config.yaml, README.md $out -Force
New-Item -ItemType Directory -Force "$out\sop" | Out-Null
robocopy sop\bin "$out\sop\bin" /E /XD logs backup /NFL /NDL /NJH /NJS /NP | Out-Null
Copy-Item sop\README.md "$out\sop\README.md" -Force

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw '没有找到 Inno Setup 6，先运行：choco install innosetup' }
& $iscc /Q "/DAppVersion=$version" installer\MyAIBot.iss                    # 完整安装包
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup 编译失败' }
& $iscc /Q "/DAppVersion=$version" /DUpdatePackage installer\MyAIBot.iss    # 更新包
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup 编译失败' }
Get-Item "dist\MyAIBot-*-v$version.exe" | ForEach-Object { Write-Host ("OK -> {0} ({1} MB)" -f $_.FullName, [int]($_.Length / 1MB)) }
