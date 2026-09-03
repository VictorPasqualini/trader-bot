# Keep Pouch running on this Windows machine until it moves to a real host.
#
# Run once, from an elevated PowerShell, in the project folder:
#
#     powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1
#
# It registers a scheduled task that starts the dashboard at logon and restarts
# it if it dies, and it stops the machine from sleeping. Both matter for the
# same reason: the coverage gate counts every candle close the bot was not
# awake for, and those misses never expire, so an hour asleep costs more than
# an hour of progress.
#
# Undo with:  Unregister-ScheduledTask -TaskName Pouch -Confirm:$false

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) {
    $python = Join-Path $root '.venv\Scripts\python.exe'
}
if (-not (Test-Path $python)) {
    throw "No virtualenv at $root\.venv - create it before registering the task."
}

$action = New-ScheduledTaskAction -Execute $python `
    -Argument 'run.py serve --no-browser' -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# StartWhenAvailable catches the boot where the network was not up yet;
# RestartCount covers a crash; the power flags are what stop Windows from
# killing the task the moment the laptop is unplugged.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName 'Pouch' -Action $action -Trigger $trigger `
    -Settings $settings -Description 'Pouch trading bot and dashboard' -Force | Out-Null

Write-Output 'Scheduled task "Pouch" registered.'

# Never sleep, never hibernate, on either power source. The display can still
# turn off - that costs nothing.
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0
Write-Output 'Sleep and hibernate disabled.'

Start-ScheduledTask -TaskName 'Pouch'
Write-Output 'Started. Dashboard: http://127.0.0.1:8777'
