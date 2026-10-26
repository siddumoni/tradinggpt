$ErrorActionPreference = "Stop"

$TaskName = "TradingGPT Paper Candle Scanner"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Could not find $PythonExe. Create/install the project's .venv first, then rerun this installer."
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "-m src.paper_trading_service" -WorkingDirectory $RepoRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$Principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "TradingGPT paper-only Kite service and local dashboard. Never places, modifies, or cancels orders." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Installed and started: $TaskName"
Write-Host "Repository: $RepoRoot"
Write-Host "Log: $(Join-Path $RepoRoot 'logs\paper_trading_service.log')"
Write-Host "The service scans completed candles, monitors Kite ticks for virtual exits, and creates daily reports."
Write-Host "Dashboard: http://127.0.0.1:8000"
Write-Host "Keep this Windows user signed in and the PC awake; the task uses this user's cached Kite session."
