; RaidBook Lite 설치 스크립트 (Inno Setup 6)
; 사용자 권한 설치(관리자 승인 불필요) — 원격 패널에서 무인 업데이트가 가능하도록.

#define MyAppName "RaidBook"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "RaidBook Team"
#define MyAppExeName "RaidBook.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567891}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} Lite {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
UsePreviousAppDir=yes
LicenseFile=LICENSE.txt
OutputDir=dist\installer
OutputBaseFilename=RaidBook_Setup_{#MyAppVersion}
SetupIconFile=Images\Icon\WatchDog.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no

[Languages]
#if FileExists(AddBackslash(CompilerPath) + "Languages\Korean.isl")
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
#else
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "바탕화면 아이콘 만들기"; GroupDescription: "추가 아이콘:"
Name: "startup"; Description: "Windows 시작 시 자동 실행"; GroupDescription: "추가 옵션:"

[Dirs]
; 설정·데이터·로그는 ProgramData\RaidBook (업데이트해도 유지)
Name: "{commonappdata}\RaidBook"
Name: "{commonappdata}\RaidBook\Config"
Name: "{commonappdata}\RaidBook\Data"
Name: "{commonappdata}\RaidBook\Log"

[Files]
Source: "dist\RaidBook.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\RaidBook_Boss_Bot.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\RaidBook_Music_Bot.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "Config\*.template.*"; DestDir: "{app}\Config"; Flags: ignoreversion
Source: "Config\*.template.*"; DestDir: "{commonappdata}\RaidBook\Config"; Flags: ignoreversion
Source: "Images\*"; DestDir: "{app}\Images"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Images\*"; DestDir: "{commonappdata}\RaidBook\Images"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Guide\*"; DestDir: "{app}\Guide"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} 설정·데이터 폴더"; Filename: "{commonappdata}\RaidBook"
Name: "{group}\{#MyAppName} 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
; 일반 설치: 마지막 화면에서 실행 선택 / 무인 업데이트(/VERYSILENT): 자동 재실행
Filename: "{app}\{#MyAppExeName}"; Description: "RaidBook 실행"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Flags: nowait; Check: WizardSilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\Log"

[Code]
procedure KillRaidBook();
var
  ResultCode: Integer;
begin
  Exec('taskkill.exe', '/F /IM RaidBook.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill.exe', '/F /IM RaidBook_Boss_Bot.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill.exe', '/F /IM RaidBook_Music_Bot.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(1500);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  // RaidBook_Watchdog_GUI.py의 CreateMutexW 이름과 일치해야 함
  if CheckForMutexes('RaidBook_Watchdog_GUI_SingleInstance') then
  begin
    if WizardSilent() then
      KillRaidBook()
    else if MsgBox('RaidBook이 실행 중입니다. 종료하고 계속할까요?', mbConfirmation, MB_YESNO) = IDYES then
      KillRaidBook()
    else
      Result := False;
  end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  KillRaidBook();
end;
