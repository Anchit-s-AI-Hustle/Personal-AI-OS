<#
.SYNOPSIS
    Register Personal AI OS as a Windows Scheduled Task that runs at logon
    and keeps running while the screen is locked.

.DESCRIPTION
    Creates a task named "PersonalAIOS" under the current user. The task:
      - Triggers at user logon
      - Restarts on failure
      - Has no idle / battery / time limits
      - Runs hidden (no console window pop-up)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File startup\install_task.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'PersonalAIOS'
)

$ErrorActionPreference = 'Stop'

# Resolve the run.bat sitting next to this script.
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$runBat     = Join-Path $scriptDir 'run.bat'
$projectDir = (Resolve-Path (Join-Path $scriptDir '..')).Path

if (-not (Test-Path $runBat)) {
    throw "Could not find run.bat at $runBat"
}

Write-Host "Registering scheduled task '$TaskName'"
Write-Host "  Project dir : $projectDir"
Write-Host "  Runner      : $runBat"

$action = New-ScheduledTaskAction `
    -Execute 'cmd.exe' `
    -Argument ('/c "' + $runBat + '"') `
    -WorkingDirectory $projectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$taskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

# Replace any prior registration so re-running this script is idempotent.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Action      $action `
    -Trigger     $trigger `
    -Settings    $taskSettings `
    -Principal   $principal `
    -Description 'Personal AI OS - email + meeting intelligence' | Out-Null

Write-Host ''
Write-Host "Done. The task will start automatically at every logon."
Write-Host "To start it right now:    Start-ScheduledTask -TaskName $TaskName"
Write-Host "To check status:          Get-ScheduledTask -TaskName $TaskName"
Write-Host 'To remove later:          powershell -File startup\uninstall_task.ps1'
