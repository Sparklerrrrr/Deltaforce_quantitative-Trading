; DFQ 安装 / 卸载向导（Inno Setup）。由 scripts\build_exe.bat 调用 ISCC 编译，一般不用手动改。
;
; 设计约定
;   - 不需要管理员权限，装在用户自己选的文件夹里（默认 D:\DFQ；没有 D 盘时放在当前用户的程序目录）。
;   - 程序产生的所有文件（config.yaml、.env、data\ 下的数据库/缓存/日志/界面数据）都只在安装文件夹里。
;     装到 C 盘之外的唯一东西是用户勾选的快捷方式（Windows 的规定），卸载时一并删除。
;   - 不随安装包分发任何个人文件：config.yaml 和 .env 由程序首次启动时从模板生成（dfq/layout.py）。
;     升级安装只替换程序文件，不碰配置、密钥和数据。DFQ 正在运行时会提示先退出（AppMutex）。
;   - 卸载程序 unins000.exe 和 DFQ.exe 在同一个文件夹。卸载时可选（保留规则见 dfq/layout.py）：
;       保留配置和交易数据、删除密钥（默认）/ 保留配置、交易数据和密钥 / 完全删除
;     静默卸载用参数指定：unins000.exe /VERYSILENT /KEEP=data|data_env|none（默认 data）
;   - 删除时遇到符号链接 / 目录联接只删链接本身，不会进入它指向的位置。
;   - 为了“完全删除”不误删别的东西：不允许装到盘符根目录、Program Files、Windows 目录；
;     选的文件夹里已有别的文件时会先提醒。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\dist\DFQ"
#endif
#ifndef RepoDir
  #define RepoDir ".."
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define AppName "DFQ 子弹交易决策助手"

[Setup]
AppId={{8E3C5B71-4D2A-4F6B-9C1E-7A2D5F0B3C91}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=DFQ
DefaultDirName={code:DefaultInstallDir}
DisableProgramGroupPage=yes
DisableDirPage=no
DirExistsWarning=no
UsePreviousAppDir=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=DFQ-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=yes
AppMutex=DFQ_SubdanTradingAssistant_Running
UninstallDisplayIcon={app}\DFQ.exe
SetupIconFile={#RepoDir}\dfq.ico
UninstallDisplayName={#AppName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Messages]
chs.ConfirmUninstall=确定要按刚才的选择卸载 %1 吗？
chs.SelectDirBrowseLabel=程序产生的配置、数据、缓存和日志都只会保存在这个文件夹里。建议选一个单独的、不在 C 盘的文件夹。

[Tasks]
Name: "desktopicon"; Description: "在桌面创建快捷方式"; GroupDescription: "快捷方式（只是指向安装文件夹的链接，卸载时会删除）："
Name: "startmenuicon"; Description: "在开始菜单创建快捷方式"; GroupDescription: "快捷方式（只是指向安装文件夹的链接，卸载时会删除）："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoDir}\scripts\install_tasks.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "{#RepoDir}\.env.example"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\DFQ.exe"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userprograms}\{#AppName}"; Filename: "{app}\DFQ.exe"; WorkingDir: "{app}"; Tasks: startmenuicon

[Run]
Filename: "{app}\DFQ.exe"; Description: "立即打开 DFQ"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
var
  UninstallMode: Integer;   { 0 = 完全删除；1 = 保留 config.yaml；2 = 保留 config.yaml 和 .env }

function DefaultInstallDir(Param: String): String;
begin
  if DirExists('D:\') then
    Result := 'D:\DFQ'
  else
    Result := ExpandConstant('{localappdata}\Programs\DFQ');
end;

function StartsWithText(const S, Prefix: String): Boolean;
begin
  Result := (Prefix <> '') and (CompareText(Copy(S, 1, Length(Prefix)), Prefix) = 0);
end;

function IsDangerousDir(Dir: String): Boolean;
begin
  Dir := RemoveBackslashUnlessRoot(Dir);
  Result := (Length(Dir) <= 3)                                   { 盘符根目录，例如 D:\ }
    or StartsWithText(Dir, ExpandConstant('{commonpf64}'))
    or StartsWithText(Dir, ExpandConstant('{commonpf32}'))
    or StartsWithText(Dir, ExpandConstant('{win}'))
    or (CompareText(Dir, RemoveBackslashUnlessRoot(ExpandConstant('{%USERPROFILE}'))) = 0);
end;

function DirHasForeignFiles(const Dir: String): Boolean;
var
  FR: TFindRec;
begin
  Result := False;
  if not DirExists(Dir) then exit;
  { 以前装过 / 卸载时保留下来的配置或密钥，都算 DFQ 自己的文件 }
  if FileExists(AddBackslash(Dir) + 'DFQ.exe') or FileExists(AddBackslash(Dir) + 'config.yaml')
     or FileExists(AddBackslash(Dir) + '.env') then exit;
  if FindFirst(AddBackslash(Dir) + '*', FR) then
  try
    repeat
      if (FR.Name <> '.') and (FR.Name <> '..') then
      begin
        Result := True;
        exit;
      end;
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir: String;
begin
  Result := True;
  if CurPageID <> wpSelectDir then exit;
  Dir := WizardDirValue;
  { 用 SuppressibleMsgBox：有人操作时照常弹窗；静默安装（/SUPPRESSMSGBOXES）时不弹窗、直接用最后一个参数作为答案 }
  if IsDangerousDir(Dir) then
  begin
    SuppressibleMsgBox('不能安装到这个位置：' + #13#10 + Dir + #13#10#13#10 +
           '请不要选盘符根目录、Program Files、Windows 目录或用户主目录，' +
           '而是选一个单独的文件夹，例如 D:\DFQ。' + #13#10 +
           '（卸载时选择“完全删除”会删除整个安装文件夹，所以它必须是专用的。）', mbError, MB_OK, IDOK);
    Result := False;                                              { 静默安装也一样拒绝 }
    exit;
  end;
  if DirHasForeignFiles(Dir) then
    Result := SuppressibleMsgBox('这个文件夹里已经有其他文件：' + #13#10 + Dir + #13#10#13#10 +
                     '卸载时如果选择“完全删除”，这个文件夹里的所有文件都会被删除（包括不是 DFQ 的文件）。' + #13#10 +
                     '建议换一个空文件夹。仍要安装到这里吗？', mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDYES) = IDYES;
                     { 静默安装时目录是调用方用 /DIR 明确指定的，按“是”处理 }
end;

{ ---------------------------------------------------------------- 卸载 }
{ 保留规则必须与 dfq/layout.py 一致（tests/test_layout.py 会比对下面两个列表）。
  UninstallMode：0 = 完全删除；1 = 保留配置和交易数据（删除密钥，默认）；2 = 保留配置、交易数据和密钥 }

function KeepTopList(): String;
begin
  Result := '|config.yaml|';                                    { 程序文件夹根部，模式 1、2 保留 }
end;

function KeepDataList(): String;
begin
  Result := '|ui-settings.json|dfq.db|dfq.db-wal|dfq.db-shm|overrides.yaml|plans|outbox|inbox|reflections|reports|backups|';
end;

function KeepSecretList(): String;
begin
  Result := '|.env|moligod_profile|';                           { 只有模式 2 保留：密钥与登录状态 }
end;

function InList(const Name, List: String): Boolean;
begin
  Result := Pos('|' + LowerCase(Name) + '|', LowerCase(List)) > 0;
end;

function KeepParam(): Integer;
var
  I: Integer;
  S: String;
begin
  Result := 1;                                                  { 静默卸载默认：保留配置和交易数据、删除密钥 }
  for I := 1 to ParamCount do
  begin
    S := LowerCase(ParamStr(I));
    if Copy(S, 1, 6) = '/keep=' then
    begin
      S := Copy(S, 7, Length(S));
      if S = 'none' then Result := 0
      else if (S = 'data') or (S = 'config') then Result := 1
      else if (S = 'data_env') or (S = 'config_env') then Result := 2;
    end;
  end;
end;

function AskUninstallMode(): Integer;
var
  Form: TSetupForm;
  Title, Note: TNewStaticText;
  RKeep, RKeepEnv, RFull: TNewRadioButton;
  OK, Cancel: TNewButton;
begin
  Result := -1;
  Form := CreateCustomForm(500, 330, False, False);          { 尺寸按 96 DPI 写，Inno 会自己按屏幕缩放 }
  try
    Form.Caption := '卸载 {#AppName}';

    Title := TNewStaticText.Create(Form);
    Title.Parent := Form;
    Title.Left := ScaleX(20);
    Title.Top := ScaleY(18);
    Title.Width := Form.ClientWidth - ScaleX(40);
    Title.AutoSize := False;
    Title.WordWrap := True;
    Title.Height := ScaleY(56);
    Title.Caption := '请选择卸载方式。安装文件夹：' + #13#10 + ExpandConstant('{app}');

    RKeep := TNewRadioButton.Create(Form);
    RKeep.Parent := Form;
    RKeep.Left := ScaleX(20);
    RKeep.Top := ScaleY(86);
    RKeep.Width := Form.ClientWidth - ScaleX(40);
    RKeep.Caption := '保留配置和交易数据，删除密钥（推荐）';
    RKeep.Checked := True;

    RKeepEnv := TNewRadioButton.Create(Form);
    RKeepEnv.Parent := Form;
    RKeepEnv.Left := ScaleX(20);
    RKeepEnv.Top := ScaleY(114);
    RKeepEnv.Width := Form.ClientWidth - ScaleX(40);
    RKeepEnv.Caption := '保留配置、交易数据和密钥（.env、moligod 登录状态）';

    RFull := TNewRadioButton.Create(Form);
    RFull.Parent := Form;
    RFull.Left := ScaleX(20);
    RFull.Top := ScaleY(142);
    RFull.Width := Form.ClientWidth - ScaleX(40);
    RFull.Caption := '完全删除（安装文件夹里的所有文件，包括交易记录，都删除）';

    Note := TNewStaticText.Create(Form);
    Note.Parent := Form;
    Note.Left := ScaleX(20);
    Note.Top := ScaleY(176);
    Note.Width := Form.ClientWidth - ScaleX(40);
    Note.AutoSize := False;
    Note.WordWrap := True;
    Note.Height := ScaleY(96);
    Note.Caption := '“配置和交易数据”包括：config.yaml、界面设置、data\dfq.db（交易记录与行情历史）、' +
                    '采纳的调参、历次计划、报告和备份。以后重装到同一个文件夹会直接接着用。' + #13#10 +
                    '无论选哪一项，都会删除程序本体、缓存、日志、临时文件，以及程序创建的 Windows 计划任务。' + #13#10 +
                    '.env 里是明文的 API Key / token，保留时请妥善保管这个文件夹。';

    OK := TNewButton.Create(Form);
    OK.Parent := Form;
    OK.Caption := '下一步';
    OK.ModalResult := mrOk;
    OK.Default := True;
    OK.Width := ScaleX(90);
    OK.Height := ScaleY(26);
    OK.Left := Form.ClientWidth - ScaleX(20) - ScaleX(90) * 2 - ScaleX(10);
    OK.Top := Form.ClientHeight - ScaleY(42);

    Cancel := TNewButton.Create(Form);
    Cancel.Parent := Form;
    Cancel.Caption := '取消';
    Cancel.ModalResult := mrCancel;
    Cancel.Cancel := True;
    Cancel.Width := ScaleX(90);
    Cancel.Height := ScaleY(26);
    Cancel.Left := Form.ClientWidth - ScaleX(20) - ScaleX(90);
    Cancel.Top := Form.ClientHeight - ScaleY(42);

    if Form.ShowModal() = mrOk then
    begin
      if RFull.Checked then Result := 0
      else if RKeepEnv.Checked then Result := 2
      else Result := 1;
    end;
  finally
    Form.Free();
  end;
end;

function InitializeUninstall(): Boolean;
begin
  if UninstallSilent then
    UninstallMode := KeepParam()
  else
    UninstallMode := AskUninstallMode();
  Log('DFQ: uninstall mode = ' + IntToStr(UninstallMode));        { 只在带 /LOG 参数时写日志 }
  Result := UninstallMode >= 0;
end;

procedure RemoveScheduledTasks();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN dfq-nightly /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN dfq-mail-poll /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Exec(ExpandConstant('{sys}\schtasks.exe'), '/Delete /TN dfq-watch /F', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function IsReparsePoint(Attr: Cardinal): Boolean;
begin
  Result := (Attr and $400) <> 0;                                { FILE_ATTRIBUTE_REPARSE_POINT：符号链接 / 目录联接 }
end;

function PathAttr(const P: String): Cardinal;
var
  FR: TFindRec;
begin
  Result := 0;
  if FindFirst(P, FR) then
  try
    Result := FR.Attributes;
  finally
    FindClose(FR);
  end;
end;

procedure SafeDelete(const P: String; Attr: Cardinal);
{ 安全删除：遇到符号链接 / 目录联接只删链接本身，绝不进入它指向的地方。 }
var
  FR: TFindRec;
  Child: String;
begin
  if (Attr and FILE_ATTRIBUTE_DIRECTORY) = 0 then
  begin
    DeleteFile(P);
    exit;
  end;
  if not IsReparsePoint(Attr) then
  begin
    if FindFirst(P + '\*', FR) then
    try
      repeat
        if (FR.Name <> '.') and (FR.Name <> '..') then
        begin
          Child := P + '\' + FR.Name;
          SafeDelete(Child, FR.Attributes);
        end;
      until not FindNext(FR);
    finally
      FindClose(FR);
    end;
  end;
  RemoveDir(P);
end;

function IsRunningUninstaller(const Name: String): Boolean;
begin
  { 正在运行的这个卸载程序（unins00N.exe / .dat）由卸载程序最后自己删除；以前残留的其他 unins 文件照常清理 }
  Result := CompareText(ChangeFileExt(Name, ''), ChangeFileExt(ExtractFileName(ExpandConstant('{uninstallexe}')), '')) = 0;
end;

procedure CleanDir(const Dir: String; const KeepList: String);
var
  FR: TFindRec;
  P: String;
begin
  if FindFirst(Dir + '\*', FR) then
  try
    repeat
      if (FR.Name = '.') or (FR.Name = '..') or IsRunningUninstaller(FR.Name) then
        continue;
      P := Dir + '\' + FR.Name;
      if InList(FR.Name, KeepList) then
      begin
        Log('DFQ: keeping ' + P);
        continue;
      end;
      Log('DFQ: removing ' + P);
      SafeDelete(P, FR.Attributes);
    until not FindNext(FR);
  finally
    FindClose(FR);
  end;
end;

procedure CleanAppDir();
var
  App, TopKeep, DataKeep: String;
begin
  App := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
  if IsDangerousDir(App) then exit;                             { 双保险：绝不清空根目录或系统目录 }
  if UninstallMode = 0 then
  begin
    CleanDir(App, '');
    exit;
  end;
  TopKeep := KeepTopList();
  DataKeep := KeepDataList();
  if UninstallMode = 2 then
  begin
    TopKeep := TopKeep + KeepSecretList();
    DataKeep := DataKeep + KeepSecretList();
  end;
  { data 本身保留，进去按清单清理；它是链接时不进入 }
  if DirExists(App + '\data') and not IsReparsePoint(PathAttr(App + '\data')) then
    CleanDir(App + '\data', DataKeep);
  CleanDir(App, TopKeep + 'data|');
  RemoveDir(App + '\data');                                     { 如果 data 里什么都没留下，顺手删掉空目录 }
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveScheduledTasks();
  if CurUninstallStep = usPostUninstall then
  begin
    CleanAppDir();
    RemoveDir(ExpandConstant('{app}'));                         { 什么都没留下时删掉空的安装文件夹 }
  end;
end;
