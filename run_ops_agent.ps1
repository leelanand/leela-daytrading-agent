# run_ops_agent.ps1 — called by Windows Task Scheduler every 8 minutes Mon-Fri 14:00-21:59 BST
$PYTHON = "C:\Users\leela\AppData\Local\Programs\Python\Python312\python.exe"
$AGENT  = "C:\Users\leela\leela-daytrading-agent\ops_agent.py"
$LOG    = "C:\Users\leela\leela-ibkr-agent\ops_fixes.log"

# Only run during market hours (09:30-16:00 ET = 14:30-21:00 BST; use 14:00-21:59 as window)
$now = [System.DateTime]::Now
if ($now.DayOfWeek -eq "Saturday" -or $now.DayOfWeek -eq "Sunday") { exit 0 }
$minuteOfDay = $now.Hour * 60 + $now.Minute
if ($minuteOfDay -lt 840 -or $minuteOfDay -gt 1319) { exit 0 }  # 14:00-21:59

# Run ops agent and capture stdout (JSON result) and stderr (log output)
$result = & $PYTHON $AGENT 2>&1
$json   = $result | Where-Object { $_ -match '^\s*\{' } | Select-Object -First 1

# Log the raw output for diagnostics
$ts = [System.DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")
"[$ts] [OPS-CRON] ran ops_agent.py" | Out-File -Append -Encoding utf8 $LOG

if (-not $json) {
    "[$ts] [OPS-CRON] WARNING: no JSON output from ops_agent.py" | Out-File -Append -Encoding utf8 $LOG
    exit 1
}

try {
    $data = $json | ConvertFrom-Json
    $fixed  = @($data.fixed)
    $notify = @($data.notify_human)
    if ($fixed.Count -gt 0) {
        "[$ts] [OPS-CRON] self-healed: $($fixed -join '; ')" | Out-File -Append -Encoding utf8 $LOG
    }
    if ($notify.Count -gt 0) {
        "[$ts] [OPS-CRON] NEEDS HUMAN: $($notify -join '; ')" | Out-File -Append -Encoding utf8 $LOG
    }
} catch {
    "[$ts] [OPS-CRON] ERROR parsing JSON: $_" | Out-File -Append -Encoding utf8 $LOG
}
