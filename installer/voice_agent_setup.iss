#define AppName "Voice Agent"
#define AppVersion "0.1.0"
#define AppPublisher "Voice Agent Team"
#define AppURL "https://example.com/voice-agent"

#ifndef UnityBuildDir
  #define UnityBuildDir "..\\dist\\unity"
#endif

#ifndef ServiceExeDir
  #define ServiceExeDir "..\\dist\\services"
#endif

#ifndef PiperRuntimeDir
  #define PiperRuntimeDir ""
#endif

[Setup]
AppId={{5E8E7F7F-2A53-4B75-82E8-8C2FDC193B8A}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={autopf}\Voice Agent
DefaultGroupName=Voice Agent
UninstallDisplayIcon={app}\start_voice_agent.bat
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=VoiceAgentSetup

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Include only expected Unity player outputs to avoid accidentally packaging
; unrelated large folders from parent/root paths.
Source: "{#UnityBuildDir}\VoiceAgent.exe"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#UnityBuildDir}\UnityPlayer.dll"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#UnityBuildDir}\UnityCrashHandler64.exe"; DestDir: "{app}\app"; Flags: ignoreversion
Source: "{#UnityBuildDir}\MonoBleedingEdge\*"; DestDir: "{app}\app\MonoBleedingEdge"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#UnityBuildDir}\VoiceAgent_Data\*"; DestDir: "{app}\app\VoiceAgent_Data"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#ServiceExeDir}\*"; DestDir: "{app}\runtime\services"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\runtime\live_captions\*"; DestDir: "{app}\runtime\live_captions"; Flags: ignoreversion recursesubdirs createallsubdirs
#if Len(PiperRuntimeDir) > 0
Source: "{#PiperRuntimeDir}\*"; DestDir: "{app}\runtime\piper"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif
Source: "..\scripts\local_services.default.json"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\local_services.user.sample.json"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\scripts\intent_service\manifest.json"; DestDir: "{app}\scripts\intent_service"; Flags: onlyifdoesntexist uninsneveruninstall
Source: "..\installer\start_voice_agent.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\installer\start_voice_agent.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Start Voice Agent"; Filename: "{app}\start_voice_agent.bat"; WorkingDir: "{app}"
Name: "{group}\Open Voice Agent Setup"; Filename: "{app}\start_voice_agent.bat"; Parameters: "-ForceSetup"; WorkingDir: "{app}"
Name: "{group}\Uninstall Voice Agent"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Voice Agent"; Filename: "{app}\start_voice_agent.bat"; WorkingDir: "{app}"

[Run]
Filename: "{app}\start_voice_agent.bat"; Description: "Launch Voice Agent now"; Flags: postinstall nowait skipifsilent
