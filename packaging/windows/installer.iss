; Peak Alphaloop per-user installer (no admin rights needed).
; Built by packaging/build_standalone.py with:
;   ISCC /DAppVersion=<x.y.z> /DSourceDir=<pyinstaller dist folder> /DOutputDir=<dir> /DOutputName=<name> installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{FC402128-A618-472A-AE62-4ED56776646F}
AppName=Peak Alphaloop
AppVersion={#AppVersion}
AppVerName=Peak Alphaloop {#AppVersion}
AppPublisher=Peak Projections
AppPublisherURL=https://peakprojections.com
AppSupportURL=https://github.com/peakprojections/peak-alphaloop/issues
AppUpdatesURL=https://github.com/peakprojections/peak-alphaloop/releases
DefaultDirName={localappdata}\Programs\Peak Alphaloop
DefaultGroupName=Peak Alphaloop
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#SourceDir}\..\..\icon\icon.ico
UninstallDisplayIcon={app}\PeakAlphaloop.exe
LicenseFile={#SourceDir}\..\..\licenses\LICENSE.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Remove files from older versions before copying the new ones.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\Peak Alphaloop"; Filename: "{app}\PeakAlphaloop.exe"
Name: "{group}\Uninstall Peak Alphaloop"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Peak Alphaloop"; Filename: "{app}\PeakAlphaloop.exe"; Tasks: desktopicon

[Run]
; Runs after interactive installs (checkbox) and after silent self-updates.
Filename: "{app}\PeakAlphaloop.exe"; Description: "{cm:LaunchProgram,Peak Alphaloop}"; Flags: nowait postinstall
