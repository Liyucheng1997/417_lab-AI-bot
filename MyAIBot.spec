# PyInstaller 配置：.venv\Scripts\pyinstaller MyAIBot.spec（或直接运行 build_exe.ps1）
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [("bot/ui", "bot/ui")]
datas += collect_data_files("uiautomation", includes=["bin/*"])
datas += collect_data_files("webview")

hiddenimports = (
    collect_submodules("claude_agent_sdk")
    + collect_submodules("webview.platforms")
    + ["pystray._win32", "clr", "win32timezone", "win32cred", "comtypes.stream",
       "win32file", "win32clipboard", "win32com.shell", "win32com.shell.shellcon"]
)

a = Analysis(
    ["run_bot.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "numpy.tests", "IPython"],
    noarchive=False,
)
# SDK 自带的 320MB Claude CLI 不打进去：用本机已安装、已登录订阅的 claude
a.datas = [d for d in a.datas if "_bundled" not in d[0]]
a.binaries = [b for b in a.binaries if "_bundled" not in b[0]]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MyAIBot",
    icon="build/icon.ico",
    console=False,
    upx=False,
    version="build/version.txt",
)
coll = COLLECT(exe, a.binaries, a.datas, name="MyAIBot", upx=False)
