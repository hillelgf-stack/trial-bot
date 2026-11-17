# Register the hourly Windows task that runs the trial bot (at minute 7 of every hour).
param([string]$BotDir = $PSScriptRoot)

$python = (Get-Command python).Source
$logDir = Join-Path $HOME "trial-bot-data"
New-Item -ItemType Directory -Force $logDir | Out-Null
$cmd = "/c cd /d `"$BotDir`" && `"$python`" -X utf8 local_bot.py run >> `"$logDir\run.log`" 2>&1"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmd
$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1).AddMinutes(7)
$trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 55) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "TrialBot" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Posts best reels as Instagram Trial Reels" -Force | Out-Null
Write-Output "TrialBot task registered, first run at $start"
