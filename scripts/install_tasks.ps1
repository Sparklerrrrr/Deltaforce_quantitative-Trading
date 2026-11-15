<#
.SYNOPSIS
    创建 dfq 的 Windows 计划任务。（未在真实 Windows 上实测，首次使用请看下面的「验证」）

.DESCRIPTION
    会创建两个任务（当前用户、无需管理员）：
      dfq-nightly    每晚定时运行主流程      python -m dfq run
      dfq-mail-poll  每隔一段时间读取邮件回复  python -m dfq mail-poll
    加 -WithWatch 时再创建一个默认「禁用」的任务：
      dfq-watch      每小时异常波动检查      python -m dfq watch
    任务只在你登录时运行；moligod 同步需要打开浏览器窗口，所以不要改成「不管用户是否登录都运行」。

.EXAMPLE
    # 在项目根目录打开 PowerShell：
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -NightlyAt 21:30 -PollMinutes 30
    powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove      # 删除全部任务

.NOTES
    验证：  Get-ScheduledTask dfq-*          查看是否创建成功
            Start-ScheduledTask dfq-nightly  立刻手动触发一次
            然后看 data\logs\task-日期.log 里有没有输出
#>
param(
    [string]$NightlyAt = "21:30",
    [int]$PollMinutes = 30,
    [switch]$WithWatch,
    [switch]$Remove,
    [string]$Exe = ""          # 安装版：直接调用 DFQ.exe（不需要 .venv），例如 -Exe "D:\DFQ\DFQ.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $PSScriptRoot "run_dfq.bat"
$names = @("dfq-nightly", "dfq-mail-poll", "dfq-watch")

if ($Remove) {
    foreach ($n in $names) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-Host "已删除 $n"
        }
    }
    return
}

if ($Exe) {
    if (-not (Test-Path $Exe)) { throw "找不到 $Exe" }
    $root = Split-Path -Parent $Exe
} elseif (-not (Test-Path $bat)) { throw "找不到 $bat" }
$envFile = Join-Path $root ".env"
$inEnvFile = (Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^\s*DFQ_XTC_TOKEN\s*=\s*\S' -Quiet)
if (-not $env:DFQ_XTC_TOKEN -and -not $inEnvFile) {
    Write-Warning "没有找到 DFQ_XTC_TOKEN。请在 DFQ.exe 的「设置」页填写（保存在程序文件夹的 .env），否则计划任务拿不到行情。"
}

function New-DfqAction([string]$subcommand) {
    if ($Exe) {   # 安装版：DFQ.exe 带参数就是命令行模式，输出写到 data\logs\task-日期.log
        return New-ScheduledTaskAction -Execute $Exe -Argument $subcommand -WorkingDirectory $root
    }
    # 路径带空格：整体用引号包住
    New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"`"$bat`" $subcommand`"" -WorkingDirectory $root
}

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

# 1) 每晚主流程
Register-ScheduledTask -TaskName "dfq-nightly" -Force -Settings $settings `
    -Action (New-DfqAction "run") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $NightlyAt) `
    -Description "dfq：每晚采集行情、生成交易计划、同步提醒、发邮件" | Out-Null
Write-Host "已创建 dfq-nightly：每天 $NightlyAt"

# 2) 定时读取邮件回复（从现在起每 N 分钟一次，长期重复）
$pollTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) `
    -RepetitionInterval (New-TimeSpan -Minutes $PollMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
Register-ScheduledTask -TaskName "dfq-mail-poll" -Force -Settings $settings `
    -Action (New-DfqAction "mail-poll") -Trigger $pollTrigger `
    -Description "dfq：读取邮件回复并记账" | Out-Null
Write-Host "已创建 dfq-mail-poll：每 $PollMinutes 分钟"

# 3) 可选：每小时异常波动检查（创建后保持禁用）
if ($WithWatch) {
    $watchTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(10) `
        -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
    Register-ScheduledTask -TaskName "dfq-watch" -Force -Settings $settings `
        -Action (New-DfqAction "watch") -Trigger $watchTrigger `
        -Description "dfq：每小时异常波动检查（默认禁用；每天约多花 48 次接口请求）" | Out-Null
    Disable-ScheduledTask -TaskName "dfq-watch" | Out-Null
    Write-Host "已创建 dfq-watch（禁用状态）。要用时：先在 config.yaml 里开 watch.enabled 并调高 xtc.daily_limit，再 Enable-ScheduledTask dfq-watch"
}

Write-Host ""
Write-Host "完成。验证：Start-ScheduledTask dfq-nightly  然后查看 data\logs\ 下的 task-*.log"
