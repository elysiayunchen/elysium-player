; Elysium 安装包脚本（Inno Setup 6）
;
; 设计取舍：
;   - PrivilegesRequired=lowest + {localappdata}\Programs：按用户安装，
;     不弹 UAC，卸载干净；数据仍在 %APPDATA%\FlacMusic，卸载不动用户数据。
;   - OutputBaseFilename 不带版本号（Elysium-setup.exe）：
;     GitHub releases 的 releases/latest/download/Elysium-setup.exe 永远指向
;     最新版，应用内「下载」直链就不用随版本改 update.json。
;
; 构建：ISCC.exe installer\elysium.iss   （产物在 dist\Elysium-setup.exe）

#define AppName "Elysium"
#define AppVersion "2.2.0-beta2"
#define AppExe "Elysium.exe"

[Setup]
AppId={{6b2f6633-ece0-4ed7-8233-63da90264950}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=..\dist
OutputBaseFilename=Elysium-setup
SetupIconFile=..\app.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
; 数据目录不随卸载删除（用户歌单 / 收藏 / 下载记录都在里面），这里只是显式声明不动它
; CreateAppDir 默认 yes；无需任何对 %APPDATA% 的写操作
ChangesEnvironment=no
MinVersion=10.0

[Messages]
; 向导框架用 Inno 自带英文，这里只给两处放中文，避免混杂大段翻译
SelectDirDesc=Where should {#AppName} be installed?
SelectDirLabel3=Setup will install {#AppName} into the following folder (per-user, no admin rights needed).
FinishedHeadingLabel={#AppName} {#AppVersion} installation complete

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent unchecked

[UninstallDelete]
; 不删除任何用户数据
