# 打包成 exe：powershell -ExecutionPolicy Bypass -File build_exe.ps1
#   产物：dist\MyAIBot\MyAIBot.exe（绿色版，整个文件夹可拷走）和 dist\MyAIBot-v<版本>-win64.zip
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$py = '.venv\Scripts\python.exe'
$version = & $py -c "from bot import __version__; print(__version__)"
New-Item -ItemType Directory -Force build | Out-Null

# 图标 + exe 版本信息
& $py -c @"
from bot.app import make_icon
make_icon(256).save('build/icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
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

# exe 旁边放可编辑的配置和截图识字模块
$out = 'dist\MyAIBot'
Copy-Item config.yaml, README.md $out -Force
New-Item -ItemType Directory -Force "$out\sop" | Out-Null
robocopy sop\bin "$out\sop\bin" /E /XD logs backup /NFL /NDL /NJH /NJS /NP | Out-Null
Copy-Item sop\README.md "$out\sop\README.md" -Force

$zip = "dist\MyAIBot-v$version-win64.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
& $py -c "import shutil; shutil.make_archive(r'dist\MyAIBot-v$version-win64', 'zip', 'dist', 'MyAIBot')"
$mb = [int]((Get-Item $zip).Length / 1MB)
Write-Host "OK -> $out\MyAIBot.exe ; $zip ($mb MB)"
