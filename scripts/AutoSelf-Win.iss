; AutoSelf Windows installer (Inno Setup 6)
; 由 scripts/build_win_desktop.ps1 调用，也可手动：
;   ISCC.exe /DMyAppVersion=1.0.0 /DMyAppSource=..\dist\AutoSelf /DMyAppIcon=..\assets\icons\AutoSelf.ico AutoSelf-Win.iss

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#ifndef MyAppSource
  #define MyAppSource "..\dist\AutoSelf"
#endif
#ifndef MyAppIcon
  #define MyAppIcon "..\assets\icons\AutoSelf.ico"
#endif

[Setup]
AppId={{A7C0E5F2-9B41-4D2E-9F8A-AUTOselfWIN01}
AppName=AutoSelf
AppVersion={#MyAppVersion}
AppPublisher=AutoSelf
DefaultDirName={autopf}\AutoSelf
DefaultGroupName=AutoSelf
DisableProgramGroupPage=yes
OutputBaseFilename=AutoSelf-Win-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#MyAppIcon}
UninstallDisplayIcon={app}\AutoSelf.exe

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
Source: "{#MyAppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\AutoSelf"; Filename: "{app}\AutoSelf.exe"
Name: "{autodesktop}\AutoSelf"; Filename: "{app}\AutoSelf.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AutoSelf.exe"; Description: "Launch AutoSelf"; Flags: nowait postinstall skipifsilent
