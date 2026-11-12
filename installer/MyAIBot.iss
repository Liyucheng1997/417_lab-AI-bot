; 我的 AI Bot 安装包（Inno Setup 6）。由 build_exe.ps1 调用，不要直接编译：
;   完整安装包：ISCC /DAppVersion=1.1.0 MyAIBot.iss            → dist\MyAIBot-Setup-v1.1.0.exe
;   更新包：    ISCC /DAppVersion=1.1.0 /DUpdatePackage MyAIBot.iss → dist\MyAIBot-Update-v1.1.0.exe（不含 OCR 引擎）
; 客户端内更新时以 /VERYSILENT /UPDATE 运行更新包：等 Bot 退出 → 覆盖程序文件 → 重新启动 Bot。

#ifndef AppVersion
  #error 需要 /DAppVersion=x.y.z
#endif
#define AppName "我的 AI Bot"
#define AppExe "MyAIBot.exe"
#define SrcDir "..\dist\MyAIBot"
#define Mutex "MyAIBot.SingleInstance"
#define AppIdStr "{a9a03756-f716-4761-b240-ac35908a24d8}"

[Setup]
AppId={{#AppIdStr}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Yucheng LI
AppPublisherURL=https://github.com/Liyucheng1997/417_lab-AI-bot
VersionInfoVersion={#AppVersion}
; 装在 D 盘，不需要管理员权限；配置、记忆、对话历史都在安装目录里
DefaultDirName=D:\my softwares\My AI Bot
UsePreviousAppDir=yes
DirExistsWarning=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\build\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
; SOP.exe 等被占用时由重启管理器关闭；不让它自动重启（Bot 由下面的 [Run] 启动）
CloseApplications=yes
RestartApplications=no
OutputDir=..\dist
#ifdef UpdatePackage
OutputBaseFilename=MyAIBot-Update-v{#AppVersion}
#else
OutputBaseFilename=MyAIBot-Setup-v{#AppVersion}
#endif

[Languages]
Name: "chs"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[InstallDelete]
; 程序库整个换新，避免旧版本残留的 dll 冲突（配置和数据不在这里）
Type: filesandordirs; Name: "{app}\_internal"
; 旧版本的快捷方式图标（文件名带版本号，原因见 [Icons]）
Type: files; Name: "{app}\MyAIBot-*.ico"

[Files]
Source: "{#SrcDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\_internal\bot\ui\icon.ico"; DestDir: "{app}"; DestName: "MyAIBot-{#AppVersion}.ico"; Flags: ignoreversion
Source: "{#SrcDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#SrcDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SrcDir}\sop\README.md"; DestDir: "{app}\sop"; Flags: ignoreversion
Source: "{#SrcDir}\sop\bin\SOP.exe"; DestDir: "{app}\sop\bin"; Flags: ignoreversion
; 手机投屏组件 scrcpy + adb（约 11 MB），更新包里也带着
Source: "{#SrcDir}\phone\scrcpy\*"; DestDir: "{app}\phone\scrcpy"; Flags: ignoreversion recursesubdirs createallsubdirs
; 用户会改的配置：只在第一次安装时放进去，更新和卸载都不动
Source: "{#SrcDir}\config.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#SrcDir}\sop\bin\config.json"; DestDir: "{app}\sop\bin"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "{#SrcDir}\sop\bin\hotstrings.txt"; DestDir: "{app}\sop\bin"; Flags: onlyifdoesntexist uninsneveruninstall
#ifndef UpdatePackage
; OCR 引擎约 300MB，只在完整安装包里
Source: "{#SrcDir}\sop\bin\engine\*"; DestDir: "{app}\sop\bin\engine"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Icons]
; 快捷方式的图标单独指向带版本号的 .ico：资源管理器按文件路径缓存图标，
; 指向 exe 的话换了图标桌面上还是旧的；每个版本换个文件名，缓存就不会过期
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\MyAIBot-{#AppVersion}.ico"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; IconFilename: "{app}\MyAIBot-{#AppVersion}.ico"; Tasks: desktopicon

[Run]
; 换了图标后刷新 Windows 图标缓存，桌面 / 开始菜单快捷方式不再显示旧图标
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden waituntilterminated
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent
; 客户端内更新（静默）装完后自动重新启动
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Flags: nowait; Check: IsUpdate

[UninstallDelete]
Type: files; Name: "{userstartup}\{#AppName}.vbs"
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\sop\bin\logs"
Type: filesandordirs; Name: "{app}\phone"

[Code]
function IsUpdate: Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), '/UPDATE') = 0 then
      Result := True;
end;

{ Bot 在运行时不能覆盖文件：更新模式下等它自己退出（最多 30 秒），手动安装时提示先退出 }
function WaitForBotExit(Silent: Boolean): Boolean;
var
  i: Integer;
begin
  if Silent then
  begin
    i := 0;
    while CheckForMutexes('{#Mutex}') and (i < 60) do
    begin
      Sleep(500);
      i := i + 1;
    end;
    if CheckForMutexes('{#Mutex}') then
    begin
      { 还没退出（卡住了）：强制结束安装目录里的 Bot 再继续 }
      Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, i);
      Sleep(1500);
    end;
    Result := not CheckForMutexes('{#Mutex}');
    exit;
  end;
  Result := True;
  while CheckForMutexes('{#Mutex}') do
    if MsgBox('{#AppName} 正在运行。' + #13#10 + '请在右下角托盘的蓝色「B」图标上右键 → 退出，然后点「重试」。',
              mbError, MB_RETRYCANCEL) = IDCANCEL then
    begin
      Result := False;
      exit;
    end;
end;

function InitializeSetup: Boolean;
begin
#ifdef UpdatePackage
  if not RegValueExists(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppIdStr}_is1',
                        'Inno Setup: App Path') then
  begin
    if not WizardSilent then
      MsgBox('这是更新包，需要先安装完整版（MyAIBot-Setup-v{#AppVersion}.exe）。', mbError, MB_OK);
    Result := False;
    exit;
  end;
#endif
  Result := WaitForBotExit(IsUpdate or WizardSilent);
end;

// 手机投屏用过之后，adb 服务（安装目录 phone\scrcpy\adb.exe）会在 Bot 退出后继续常驻，
// 重启管理器关不掉它，静默更新就会直接放弃。装 / 卸之前先结束这个目录里的 adb（别人装的 adb 不碰）。
procedure StopBundledAdb(Dir: String);
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
       '-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-Process adb -ErrorAction SilentlyContinue | ' +
       'Where-Object { $_.Path -like ''' + AddBackslash(Dir) + '*'' } | Stop-Process -Force"',
       '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { 在重启管理器检查占用文件之前调用 }
  StopBundledAdb(ExpandConstant('{app}'));
  Result := '';
end;

function InitializeUninstall: Boolean;
begin
  Result := WaitForBotExit(UninstallSilent);
  if Result then
    StopBundledAdb(ExpandConstant('{app}'));
end;
