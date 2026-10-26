$ErrorActionPreference = "Stop"

$TaskName = "TradingGPT Paper Candle Scanner"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Could not find $PythonExe. Create/install the project's .venv first, then rerun this installer."
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "-m src.paper_scan_scheduler" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Read-only TradingGPT Kite paper scanner. Never places, modifies, or cancels orders." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started: $TaskName"
Write-Host "Repository: $RepoRoot"
Write-Host "Log: $(Join-Path $RepoRoot 'logs\paper_scan_scheduler.log')"
Write-Host "The scheduler scans after completed 5-minute candle boundaries (09:20-15:15 IST, weekdays)."
Write-Host "Keep this Windows user signed in and the PC awake; the task uses this user's cached Kite session."
