; Instalador do Sysvar Organizer (Inno Setup 6) — Windows 11, 64 bits.
;
; Compile com packaging\build_windows.cmd, que informa AppVersion, SourceDir e
; OutputDir. Instalação POR USUÁRIO, sem elevação administrativa.
;
; O instalador apenas copia o aplicativo, cria os atalhos e registra a
; desinstalação. Não cria serviços, drivers, tarefas agendadas nem
; inicialização automática, e não executa análises de disco.
; Os dados do usuário ficam em %LOCALAPPDATA%\Sysvar\Organizer (fora da pasta
; de instalação) e NÃO são removidos na desinstalação.

#ifndef AppVersion
  #error Informe /DAppVersion=x.y.z (use packaging\build_windows.cmd)
#endif
#ifndef SourceDir
  #error Informe /DSourceDir=<pasta do aplicativo empacotado>
#endif
#ifndef OutputDir
  #error Informe /DOutputDir=<pasta de saída do instalador>
#endif

#define AppName "Sysvar Organizer"
#define AppPublisher "Sysvar"
#define AppDescription "Organizador de arquivos"
#define AppExeName "SysvarOrganizer.exe"
; Mesmo identificador e mutex usados pelo aplicativo (sysvar_organizer\app.py).
#define AppUserModelId "Sysvar.Organizer"
#define AppMutexName "SysvarOrganizer.AppMutex"

[Setup]
; O AppId identifica o produto entre versões: NÃO altere em atualizações.
AppId={{1282F080-427E-413B-A076-26B186E1C3D0}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppComments={#AppDescription}
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Instalador do {#AppName}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 11 (build 22000) ou posterior: Windows 10 ainda não foi validado.
MinVersion=10.0.22000
AppMutex={#AppMutexName}
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir={#OutputDir}
OutputBaseFilename=SysvarOrganizer-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
SetupLogging=yes

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Atualização: remove apenas as bibliotecas da versão anterior DENTRO da pasta
; de instalação, para não misturar versões. Dados do usuário não são tocados.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Comment: "{#AppDescription}"; AppUserModelID: "{#AppUserModelId}"
Name: "{group}\Diagnóstico do {#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "--self-check"; WorkingDir: "{app}"; Comment: "Verificação rápida (somente leitura)"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Comment: "{#AppDescription}"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent unchecked
