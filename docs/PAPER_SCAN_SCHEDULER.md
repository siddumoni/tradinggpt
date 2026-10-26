# Automatic Windows paper-scan scheduler

## What it does

`src.paper_scan_scheduler` runs the existing advisory scanner on the Windows computer using the current Python environment and cached Kite session. It checks the clock every five seconds and starts one scan after each 5-minute candle close, with a 10-second publication grace period. On normal weekdays it schedules boundaries from 09:20 through 15:15 IST.

The scheduler persists the last attempted candle boundary to avoid duplicate scans after a restart. If it notices a candle more than two minutes late, it skips that boundary instead of scanning with stale quotes. A failed scan is logged and retried on the next candle. It does not loosen strategy filters, place orders, or claim hypothetical paper results are real fills.

## Install on Windows 11

First pull the merged repository changes and ensure dependencies and the current day's Kite session are ready:

```powershell
git pull origin main
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m src.main check
```

Register the scheduler to start automatically at user sign-in and start it now:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_paper_scan_task.ps1
```

The installer uses the repository's `.venv\Scripts\python.exe`, sets the repository as the working directory, and registers a Task Scheduler task for the current Windows user. Run it once after updating the repository. It is safe to run again to update the existing task.

## Outputs and monitoring

- `logs/paper_signals.csv`: only deduplicated directional observations.
- `logs/paper_scan_runs.csv`: one row per scan, including NO_TRADE and health counts.
- `data/paper_candles_kite/`: completed Kite candles by symbol.
- `logs/paper_scan_scheduler.log`: scheduler activity, subprocess output and errors.
- `logs/paper_scan_scheduler_state.json`: last attempted candle boundary used for duplicate prevention.

Tail the logs:

```powershell
Get-Content .\logs\paper_scan_scheduler.log -Tail 50 -Wait
```

Check Task Scheduler status:

```powershell
Get-ScheduledTask -TaskName "TradingGPT Paper Candle Scanner" | Get-ScheduledTaskInfo
```

Stop and remove the task:

```powershell
Stop-ScheduledTask -TaskName "TradingGPT Paper Candle Scanner" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "TradingGPT Paper Candle Scanner" -Confirm:$false
```

## Operating limits

The computer must be awake and this Windows user must remain signed in so the task can access the same user's cached Kite session. Sleeping or powering off can cause a candle to be skipped; the scheduler intentionally does not replay stale intervals. It runs on weekdays based on local time and does not contain an exchange-holiday calendar, so on a weekday holiday it may attempt scans that return no valid market data. The existing scanner's read-only and fail-closed behavior remains authoritative.
