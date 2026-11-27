# schedule_weekly.ps1 - register the "bylaw-monitor-weekly" Windows scheduled task.
#
# Friday 13:00, powershell.exe -NoProfile -ExecutionPolicy Bypass -File weekly.ps1, working
# directory this folder, StartWhenAvailable (a missed Friday runs at next startup), 3 h limit,
# IgnoreNew, allowed on battery, no wake. Safe to re-run: unregisters and re-registers.
#
# Run it ELEVATED to get LogonType S4U ("run whether user is logged on or not"), the same as
# the mao-assembly tasks. An unelevated run still registers a working task, but as
# Interactive (only runs while you are logged on), and says so in a banner.
# The Claude follow-up task (Friday 15:00, desktop app) reads data\weekly_latest.json.

$ErrorActionPreference = 'Stop'
$name   = 'bylaw-monitor-weekly'
$root   = $PSScriptRoot
$script = Join-Path $root 'weekly.ps1'

$action   = New-ScheduledTaskAction -Execute 'powershell.exe' `
              -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" -WorkingDirectory $root
$trigger  = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At '13:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
  Unregister-ScheduledTask -TaskName $name -Confirm:$false
}
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
  -Description "Manitoba by-law monitor: province snapshot, website checks, register, Gmail digest. See $root\README.md" | Out-Null

$elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
              [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($elevated) {
  $p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
  Set-ScheduledTask -TaskName $name -Principal $p | Out-Null
}
$logon = (Get-ScheduledTask -TaskName $name).Principal.LogonType
Write-Host "$name registered: Friday 13:00, LogonType $logon"
if ($logon -ne 'S4U') {
  Write-Host ''
  Write-Host '*****************************************************************' -ForegroundColor Yellow
  Write-Host "  $name is $logon - it only runs while you are logged on."        -ForegroundColor Yellow
  Write-Host '  Re-run this script from an ELEVATED prompt to make it S4U.'     -ForegroundColor Yellow
  Write-Host '*****************************************************************' -ForegroundColor Yellow
}
