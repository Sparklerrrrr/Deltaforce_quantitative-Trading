<#
.SYNOPSIS
    DFQ 安装包验收：在真实 Windows 上把安装、首次启动、升级、卸载、计划任务、两个安装目录走一遍。

.DESCRIPTION
    每一项输出 PASS / FAIL / SKIP，最后给出计数；有任何 FAIL 时退出码为 1。
    只会改动 -Dir 和 -Dir 加 "-second" 这两个文件夹，以及（不加 -SkipTasks 时）名为 dfq-* 的计划任务。
    不联网：命令行只跑 status 和 run --mock --offline（模拟数据、不调大模型、默认配置里邮件是关的）。

    安全措施（为什么）：
      - -Dir 已存在且不像上次验收留下的文件夹时拒绝运行：卸载测试会整个删除它；
      - 这台电脑上已经装过 DFQ（「应用和功能」里有它）时拒绝运行：同一个 AppId 只有一条卸载登记，
        验收会改掉它，你原来的安装就无法从「应用和功能」卸载了。请在干净的电脑 / 虚拟机 / CI 上运行；
      - 已经有 dfq-* 计划任务时跳过计划任务这一节：创建测试任务会覆盖你自己的任务。

.PARAMETER Setup
    要验收的安装包，例如 dist\DFQ-Setup-1.0.0.exe
.PARAMETER Dir
    临时安装目录（会被删除），例如 $env:TEMP\dfq-release-test
.PARAMETER OldSetup
    可选：旧版本的安装包。给了就先装旧版、再用 -Setup 升级，验证跨版本升级保留数据。
.PARAMETER ScanScript
    可选：scripts\scan_secrets.py 的路径（需要 python 在 PATH 里）。给了就扫描全新安装后的文件夹。
.PARAMETER SkipGui
    跳过需要桌面会话的项（打开窗口、首次启动生成配置、单实例）。没有桌面的 CI 里用；这些项会标为 SKIP。
.PARAMETER SkipTasks
    跳过计划任务的创建与删除。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\release_test.ps1 -Setup dist\DFQ-Setup-1.0.0.exe -Dir $env:TEMP\dfq-rt
#>
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [Parameter(Mandatory = $true)][string]$Dir,
    [string]$OldSetup = "",
    [string]$ScanScript = "",
    [switch]$SkipGui,
    [switch]$SkipTasks
)

$ErrorActionPreference = "Stop"
$Reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{8E3C5B71-4D2A-4F6B-9C1E-7A2D5F0B3C91}_is1"
$TaskNames = @("dfq-nightly", "dfq-mail-poll", "dfq-watch")
$Dummy = "dummy-test-token-0000"
$script:pass = 0; $script:fail = 0; $script:skip = 0

function Check([string]$what, [bool]$ok) {
    if ($ok) { $script:pass++; Write-Output "  PASS  $what" } else { $script:fail++; Write-Output "  FAIL  $what" }
}
function Skip([string]$what, [string]$why) { $script:skip++; Write-Output "  SKIP  $what ($why)" }
function Section([string]$title) { Write-Output ""; Write-Output "== $title" }

# ------------------------------------------------------------------ 准备
$Setup = (Resolve-Path $Setup).Path
if ($OldSetup) { $OldSetup = (Resolve-Path $OldSetup).Path }
$Dir = [IO.Path]::GetFullPath($Dir).TrimEnd("\")
$Dir2 = "$Dir-second"

function Assert-Disposable([string]$d) {
    # 只删除空文件夹，或上次验收留下的（里面有 DFQ.exe / unins000.exe / 本脚本的标记文件）
    if (-not (Test-Path $d)) { return }
    $items = @(Get-ChildItem $d -Force)
    $ours = (Test-Path (Join-Path $d "DFQ.exe")) -or (Test-Path (Join-Path $d "unins000.exe")) -or
            (Test-Path (Join-Path $d ".dfq-release-test"))
    if ($items.Count -gt 0 -and -not $ours) { throw "$d already exists and does not look like a DFQ test folder. Pick an empty -Dir." }
    Remove-Item $d -Recurse -Force
}
if ([IO.Path]::GetPathRoot($Dir).TrimEnd("\") -eq $Dir) { throw "-Dir must not be a drive root." }
if (Test-Path $Reg) {
    throw "DFQ is already installed on this computer (found in Apps & features). Run this test on a clean machine or VM."
}
Assert-Disposable $Dir
Assert-Disposable $Dir2
$tasksBefore = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $TaskNames -contains $_.TaskName })
$doTasks = -not $SkipTasks -and $tasksBefore.Count -eq 0

function WaitGone() {
    for ($i = 0; $i -lt 240; $i++) {
        $busy = Get-Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ProcessName -like "_iu*" -or $_.ProcessName -like "_unins*" -or $_.ProcessName -like "unins0*" -or
            $_.ProcessName -like "DFQ-Setup*" -or $_.ProcessName -like "*.tmp" }
        if (-not $busy) { break }
        Start-Sleep -Milliseconds 500
    }
    Start-Sleep 1
}
function Install([string]$exe, [string]$target) {
    $p = Start-Process $exe -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/TASKS=""""", "/DIR=""$target""" -Wait -PassThru
    WaitGone
    return $p.ExitCode
}
function Uninstall([string]$target, [string]$keep) {
    $u = Join-Path $target "unins000.exe"
    if (-not (Test-Path $u)) { return -1 }
    $p = Start-Process $u -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/KEEP=$keep" -Wait -PassThru
    WaitGone
    return $p.ExitCode
}
function Has([string]$rel, [string]$base = $Dir) { Test-Path (Join-Path $base $rel) }
function ReadText([string]$rel, [string]$base = $Dir) {
    $p = Join-Path $base $rel
    if (Test-Path $p) { return [IO.File]::ReadAllText($p) } else { return "" }
}
function Hash([string]$rel, [string]$base = $Dir) {
    $p = Join-Path $base $rel
    if (Test-Path $p) { return (Get-FileHash $p -Algorithm SHA256).Hash } else { return "missing" }
}
function RunCli([string]$base, [string[]]$cliArgs) {
    $p = Start-Process (Join-Path $base "DFQ.exe") -ArgumentList $cliArgs -WorkingDirectory $env:SystemRoot -Wait -PassThru
    return $p.ExitCode
}
function AppProcs([string]$base = $Dir) {
    Get-Process DFQ -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith("$base\", "OrdinalIgnoreCase") }
}
function StopApp() { AppProcs | Stop-Process -Force -ErrorAction SilentlyContinue; Start-Sleep 2 }
function NewOutside([datetime]$since) {
    # 程序运行期间在用户临时目录 / AppData 里新出现的、看起来属于 DFQ 的东西（应该一个都没有）
    $found = @()
    foreach ($w in @($env:TEMP, $env:APPDATA, $env:LOCALAPPDATA)) {
        $found += @(Get-ChildItem $w -Force -ErrorAction SilentlyContinue | Where-Object {
            $_.CreationTime -ge $since -and $_.Name -match "pywebview|_MEI|^DFQ|^dfq|EBWebView" })
    }
    return $found
}
function Seed() {
    Add-Content (Join-Path $Dir "config.yaml") "`n# MY-SETTING-MARKER" -Encoding utf8
    Set-Content (Join-Path $Dir ".env") "DFQ_XTC_TOKEN=$Dummy" -Encoding utf8
    foreach ($d in "data\plans", "data\cache", "data\logs", "data\exports", "data\moligod_profile", "data\reports", "data\backups\db") {
        New-Item (Join-Path $Dir $d) -ItemType Directory -Force | Out-Null
    }
    Set-Content (Join-Path $Dir "data\overrides.yaml") "# MY-OVERRIDES" -Encoding utf8
    Set-Content (Join-Path $Dir "data\plans\plan.json") "{}"
    Set-Content (Join-Path $Dir "data\backups\db\dfq-backup.db") "backup"
    Set-Content (Join-Path $Dir "data\cache\c.json") "{}"
    Set-Content (Join-Path $Dir "data\logs\x.log") "log"
    Set-Content (Join-Path $Dir "data\exports\logs.zip") "zip"
    Set-Content (Join-Path $Dir "data\moligod_profile\cookies") "c"
}

Write-Output "DFQ release test"
Write-Output "  installer : $Setup"
if ($OldSetup) { Write-Output "  upgrade from: $OldSetup" }
Write-Output "  folders   : $Dir  and  $Dir2"
Write-Output "  started   : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  on  $([Environment]::OSVersion.VersionString)"

# ------------------------------------------------------------------ 1. 全新安装：不带任何个人文件
Section "1. fresh install ships nothing personal"
$first = $Setup; if ($OldSetup) { $first = $OldSetup }
Check "installer exits 0" ((Install $first $Dir) -eq 0)
Check "DFQ.exe and the uninstaller are in the install folder" ((Has "DFQ.exe") -and (Has "unins000.exe"))
Check "no config.yaml / .env / data shipped" (-not (Has "config.yaml") -and -not (Has ".env") -and -not (Has "data"))
Check ".env.example template shipped" (Has ".env.example")
Check "Apps & features entry created" (Test-Path $Reg)
if ($ScanScript) {
    & python $ScanScript --dir $Dir
    Check "scan_secrets.py finds no secrets or personal files in the fresh install" ($LASTEXITCODE -eq 0)
} else { Skip "secret / personal-file scan of the install folder" "no -ScanScript given" }

# ------------------------------------------------------------------ 2. 命令行模式（计划任务用）
Section "2. command-line mode (what scheduled tasks run)"
$t0 = Get-Date
Check "DFQ.exe status exits 0 (started from another working directory)" ((RunCli $Dir @("status")) -eq 0)
Check "output logged inside the install folder (data\logs\task-*.log)" ([bool](Get-ChildItem (Join-Path $Dir "data\logs") -Filter "task-*.log" -ErrorAction SilentlyContinue))
Check "command-line mode creates folders only, no config.yaml / .env" (-not (Has "config.yaml") -and -not (Has ".env"))
Check "nothing written to the working directory" (-not (Test-Path (Join-Path $env:SystemRoot "data\logs")))
Check "DFQ.exe run --mock --offline --force exits 0" ((RunCli $Dir @("run", "--mock", "--offline", "--force")) -eq 0)
Check "mock data kept apart in data\mock" (Has "data\mock\dfq.db")
Check "nothing new in TEMP / APPDATA / LOCALAPPDATA" (@(NewOutside $t0).Count -eq 0)

# ------------------------------------------------------------------ 3. 首次打开界面
Section "3. first launch of the window initialises the folder"
if ($SkipGui) {
    foreach ($w in "window opens", "config.yaml / .env generated from the templates", ".env has no values",
                   "folders and database created", "no module loaded from a Python install / .venv",
                   "nothing new in TEMP / APPDATA / LOCALAPPDATA", "single instance") { Skip $w "-SkipGui: needs a desktop session" }
} else {
    $t0 = Get-Date
    $p = Start-Process (Join-Path $Dir "DFQ.exe") -WorkingDirectory $env:SystemRoot -PassThru
    Start-Sleep 12
    $p.Refresh()
    Check "window opens" ($p.MainWindowHandle -ne 0)
    Check "config.yaml generated from the template" (Has "config.yaml")
    Check ".env generated" (Has ".env")
    Check ".env has no values (empty placeholders only)" (-not ((ReadText ".env") -match '(?m)^[ \t]*DFQ_[A-Z_]+[ \t]*=[ \t]*[^\s#]'))
    foreach ($d in "data", "data\logs", "data\cache", "data\tmp", "data\backups", "data\reports", "data\webview") { Check "created $d" (Has $d) }
    Check "database initialised (data\dfq.db)" (Has "data\dfq.db")
    $mods = @((Get-Process -Id $p.Id).Modules | Where-Object { $_.FileName -match '\\(\.venv|site-packages)\\' })
    Check "standalone: no module loaded from a Python install / .venv" ($mods.Count -eq 0)
    Check "nothing new in TEMP / APPDATA / LOCALAPPDATA" (@(NewOutside $t0).Count -eq 0)
    Start-Process (Join-Path $Dir "DFQ.exe") -WorkingDirectory $Dir -Wait | Out-Null
    Check "second launch exits, one window stays (single instance)" (@(AppProcs | Where-Object { $_.MainWindowHandle -ne 0 }).Count -eq 1)
    StopApp
}
if (-not (Has "config.yaml")) {
    # 没有界面时用模板补上，后面的升级 / 卸载测试才有东西可比
    Copy-Item (Join-Path $Dir "_internal\config.example.yaml") (Join-Path $Dir "config.yaml")
    Copy-Item (Join-Path $Dir ".env.example") (Join-Path $Dir ".env")
}

# ------------------------------------------------------------------ 4. 升级安装
Section "4. upgrade install keeps config, keys and data"
Seed
Set-Content (Join-Path $Dir "_internal\stale-old-library.txt") "left over from the old version"
$before = @{ cfg = (Hash "config.yaml"); env = (Hash ".env"); db = (Hash "data\dfq.db"); ov = (Hash "data\overrides.yaml") }
Check "installer exits 0" ((Install $Setup $Dir) -eq 0)
Check "config.yaml unchanged" ((Hash "config.yaml") -eq $before.cfg)
Check ".env unchanged" ((Hash ".env") -eq $before.env)
Check "data\dfq.db unchanged" ((Hash "data\dfq.db") -eq $before.db)
Check "data\overrides.yaml and plans unchanged" (((Hash "data\overrides.yaml") -eq $before.ov) -and (Has "data\plans\plan.json"))
Check "old program files replaced (_internal cleaned)" (-not (Has "_internal\stale-old-library.txt") -and (Has "_internal"))
Check "DFQ.exe status still works after the upgrade" ((RunCli $Dir @("status")) -eq 0)
if (-not $SkipGui) {
    $p = Start-Process (Join-Path $Dir "DFQ.exe") -WorkingDirectory $Dir -PassThru; Start-Sleep 8; StopApp
    Check "opening the window after the upgrade does not regenerate config / .env" (((ReadText "config.yaml") -match "MY-SETTING-MARKER") -and ((ReadText ".env") -match $Dummy))
} else { Skip "opening the window after the upgrade keeps config / .env" "-SkipGui" }

# ------------------------------------------------------------------ 5. 计划任务
Section "5. scheduled tasks"
$exe = Join-Path $Dir "DFQ.exe"
if (-not $doTasks) {
    $why = "-SkipTasks"; if (-not $SkipTasks) { $why = "dfq-* tasks already exist on this computer and would be overwritten" }
    Skip "create / inspect / remove scheduled tasks" $why
} else {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir "scripts\install_tasks.ps1") -Exe $exe | Out-Null
    foreach ($n in "dfq-nightly", "dfq-mail-poll") {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        Check "$n created" ([bool]$t)
        if ($t) {
            $a = $t.Actions[0]
            Check "$n runs the installed DFQ.exe in the install folder" (($a.Execute.Trim('"') -ieq $exe) -and ($a.WorkingDirectory.TrimEnd("\") -ieq $Dir))
        }
    }
}

# ------------------------------------------------------------------ 6. 两个安装目录互不影响
Section "6. a second install folder is independent"
$keepA = @{ cfg = (Hash "config.yaml"); env = (Hash ".env"); db = (Hash "data\dfq.db") }
Check "second install exits 0" ((Install $Setup $Dir2) -eq 0)
Check "second folder starts empty (no config / .env / data from the first)" (-not (Has "config.yaml" $Dir2) -and -not (Has ".env" $Dir2) -and -not (Has "data" $Dir2))
Check "second folder: DFQ.exe status exits 0" ((RunCli $Dir2 @("status")) -eq 0)
Check "second folder keeps its own data" ((Has "data\dfq.db" $Dir2) -and -not ((ReadText ".env" $Dir2) -match $Dummy))
Check "first folder untouched" (((Hash "config.yaml") -eq $keepA.cfg) -and ((Hash ".env") -eq $keepA.env) -and ((Hash "data\dfq.db") -eq $keepA.db))
Check "uninstalling the second folder exits 0" ((Uninstall $Dir2 "none") -eq 0)
Check "second folder removed" (-not (Test-Path $Dir2))
Check "first folder still complete after that" ((Has "DFQ.exe") -and ((Hash "config.yaml") -eq $keepA.cfg) -and ((Hash "data\dfq.db") -eq $keepA.db))
if ($doTasks) {
    Check "first folder's scheduled tasks survive the second folder's uninstall" ([bool](Get-ScheduledTask -TaskName "dfq-nightly" -ErrorAction SilentlyContinue))
}

# ------------------------------------------------------------------ 7. 默认卸载：保留配置和交易数据、删除密钥
Section "7. uninstall (default): keep config and trade data, delete keys"
Check "uninstaller exits 0" ((Uninstall $Dir "data") -eq 0)
Check "kept config.yaml, dfq.db, overrides, plans, reports, backups (incl. backups\db)" ((Has "config.yaml") -and (Has "data\dfq.db") -and
    (Has "data\overrides.yaml") -and (Has "data\plans\plan.json") -and (Has "data\reports") -and (Has "data\backups\db\dfq-backup.db"))
Check "removed .env and the moligod login state" (-not (Has ".env") -and -not (Has "data\moligod_profile"))
Check "removed program, cache, logs, exports, webview, tmp, mock" (-not (Has "DFQ.exe") -and -not (Has "_internal") -and
    -not (Has "data\cache") -and -not (Has "data\logs") -and -not (Has "data\exports") -and -not (Has "data\webview") -and
    -not (Has "data\tmp") -and -not (Has "data\mock"))
if ($doTasks) {
    Check "scheduled tasks of this folder removed" (@(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $TaskNames -contains $_.TaskName }).Count -eq 0)
}

# ------------------------------------------------------------------ 8. 重装接着用；卸载时连密钥一起保留
Section "8. reinstall continues with kept data; uninstall keeping keys too"
Check "installer exits 0" ((Install $Setup $Dir) -eq 0)
Check "kept config still there after reinstall" ((ReadText "config.yaml") -match "MY-SETTING-MARKER")
Seed
Check "uninstaller exits 0" ((Uninstall $Dir "data_env") -eq 0)
Check "kept .env and the moligod login state" ((Has ".env") -and (Has "data\moligod_profile\cookies"))
Check "kept trade data" ((Has "data\dfq.db") -and (Has "data\plans\plan.json"))

# ------------------------------------------------------------------ 9. 完全删除不会顺着目录联接删到外面
Section "9. full delete never follows a junction out of the folder"
Check "installer exits 0" ((Install $Setup $Dir) -eq 0)
Check "Apps & features entry present" (Test-Path $Reg)
$outside = "$Dir-outside-victim"
New-Item $outside -ItemType Directory -Force | Out-Null
Set-Content (Join-Path $outside "precious.txt") "must survive"
New-Item (Join-Path $Dir "data\cache") -ItemType Directory -Force | Out-Null
cmd /c mklink /J "$(Join-Path $Dir 'data\cache\link-to-outside')" "$outside" | Out-Null
Check "junction created for the test" (Test-Path (Join-Path $Dir "data\cache\link-to-outside\precious.txt"))
Check "uninstaller exits 0" ((Uninstall $Dir "none") -eq 0)
Check "install folder completely removed" (-not (Test-Path $Dir))
Check "file behind the junction survived" (Test-Path (Join-Path $outside "precious.txt"))
Check "Apps & features entry removed" (-not (Test-Path $Reg))
Remove-Item $outside -Recurse -Force -ErrorAction SilentlyContinue

Write-Output ""
Write-Output "RESULT: $($script:pass) passed, $($script:fail) failed, $($script:skip) skipped"
if ($script:fail -gt 0) { exit 1 }
exit 0
