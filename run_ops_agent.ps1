# run_ops_agent.ps1 — called by Windows Task Scheduler every 8 minutes Mon-Fri 14:00-21:59 BST
# Native stderr from python must never abort this wrapper. ops_agent.py logs heavily to stderr
# (and to ops_fixes.log itself), so we keep stderr OUT of the success stream — using 2>&1 here
# wraps each stderr line in a NativeCommandError under Windows PowerShell 5.1, which can abort the
# script before it logs/parses and makes Task Scheduler report a false exit-1. (Fixed 2026-06-02.)
$ErrorActionPreference = "Continue"

$PYTHON = "C:\Users\leela\AppData\Local\Programs\Python\Python312\python.exe"
$AGENT  = "C:\Users\leela\leela-daytrading-agent\ops_agent.py"
$LOG    = "C:\Users\leela\leela-ibkr-agent\ops_fixes.log"

# Only run during market hours (09:30-16:00 ET = 14:30-21:00 BST; use 14:00-21:59 as window)
$now = [System.DateTime]::Now
if ($now.DayOfWeek -eq "Saturday" -or $now.DayOfWeek -eq "Sunday") { exit 0 }
$minuteOfDay = $now.Hour * 60 + $now.Minute
if ($minuteOfDay -lt 840 -or $minuteOfDay -gt 1319) { exit 0 }  # 14:00-21:59

# Run ops agent. stdout -> $stdout (the JSON summary). stderr -> discarded here; ops_agent.py
# already mirrors every stderr line into ops_fixes.log, so nothing is lost.
$stdout = & $PYTHON $AGENT 2>$null
$rc     = $LASTEXITCODE

$ts = [System.DateTime]::Now.ToString("yyyy-MM-dd HH:mm:ss")
"[$ts] [OPS-CRON] ran ops_agent.py (python rc=$rc)" | Out-File -Append -Encoding utf8 $LOG

# Locate the JSON summary line in stdout
$json = $stdout | Where-Object { $_ -match '^\s*\{' } | Select-Object -First 1

if (-not $json) {
    if ($rc -ne 0) {
        # Genuine failure: python itself errored without emitting its always-on JSON summary.
        "[$ts] [OPS-CRON] FAILURE: ops_agent.py exited rc=$rc with no JSON output" | Out-File -Append -Encoding utf8 $LOG
        exit 1
    }
    # rc=0 but no JSON: benign (nothing to report). Not a scheduler failure.
    "[$ts] [OPS-CRON] no JSON summary this cycle (benign, rc=0)" | Out-File -Append -Encoding utf8 $LOG
    exit 0
}

try {
    $data   = $json | ConvertFrom-Json
    $fixed  = @($data.fixed)
    $notify = @($data.notify_human)
    if ($fixed.Count  -gt 0) {
        "[$ts] [OPS-CRON] self-healed: $($fixed -join '; ')"  | Out-File -Append -Encoding utf8 $LOG
    }
    if ($notify.Count -gt 0) {
        "[$ts] [OPS-CRON] NEEDS HUMAN: $($notify -join '; ')" | Out-File -Append -Encoding utf8 $LOG
    }
} catch {
    "[$ts] [OPS-CRON] ERROR parsing JSON: $_" | Out-File -Append -Encoding utf8 $LOG
    exit 0
}

exit 0
