; RadarScope 安装包脚本（Inno Setup 6）
;
; 用途：把 dist\RadarScope\ 打成一个带开始菜单、卸载项的安装包，
; 装到 Program Files 也不会因为目录不可写而失败（程序会自动退到 %LOCALAPPDATA%）。
;
; 用法：
;   1. 先跑 pyinstaller --noconfirm --clean installer\Lookout.spec 生成 dist\Lookout\
;   2. 装 Inno Setup 6（https://jrsoftware.org/isdl.php）
;   3. 右键本文件 → Compile，产物在 installer\Output\
;
; 为什么用 Inno 而不是自己写 zip：安装包要处理开始菜单、卸载登记、
; 升级覆盖、中文界面，这些自己写一遍只会有 bug。

#define AppName "Lookout"
#define AppVer "1.4.0"
#define AppExe "Lookout.exe"
#define SrcDir "..\dist\Lookout"

[Setup]
AppId={{8E3A1C2F-4B7D-4E5A-9C11-3F2A6D8B0E41}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName} {#AppVer}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=Output
OutputBaseFilename=Lookout-Setup-{#AppVer}
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
; 普通用户安装到 Program Files 需要提权；保持不变即可
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName} 通用雷达显控台

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
; 整个 exe 目录（含 _internal 依赖与 plugins）
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "立即运行"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只删程序文件；data\ 里的数据库和录制是否保留由用户在卸载时决定，
; 这里刻意不删 data\，避免误删别人的历史记录
Type: filesandordirs; Name: "{app}\_internal"
