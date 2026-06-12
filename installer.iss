#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppName=JAY PC Agent
AppVersion={#AppVersion}
AppPublisher=SugaredHat
DefaultDirName={autopf}\JAY PC Agent
DefaultGroupName=JAY PC Agent
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=JAY-PC-Agent-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\JAY PC Agent\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\JAY PC Agent"; Filename: "{app}\JAY PC Agent.exe"
Name: "{userdesktop}\JAY PC Agent"; Filename: "{app}\JAY PC Agent.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\JAY PC Agent.exe"; Description: "Launch JAY PC Agent"; Flags: nowait postinstall skipifsilent
