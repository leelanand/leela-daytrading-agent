# Leela Day Trading Agent — Full Daily Automation
# Scheduled to run weekdays at 14:30 BST (9:30 ET).
# Handles research → prescan → scan loop → monitor loop → force close → EOD reports.
#
# BST = ET + 5h. All time comparisons use local clock (BST).
#   9:00 ET = 14:00 BST  pre-market research (fundamentals + Claude brief)
#   9:33 ET = 14:33 BST  prescan
#   9:48 ET = 14:48 BST  first scan
#  12:00 ET = 17:00 BST  midday block begins (agent skips internally)
#  13:00 ET = 18:00 BST  midday block ends
#  15:44 ET = 20:44 BST  force close
#  16:00 ET = 21:00 BST  report
#  16:15 ET = 21:15 BST  performance dashboard

param()

$Python   = "C:\Users\leela\AppData\Local\Programs\Python\Python312\python.exe"
$AgentDir = "C:\Users\leela\leela-daytrading-agent"
$LogFile  = "$AgentDir\trading_day.log"

Set-Location $AgentDir

function Write-Log {
    param([string]$Msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

function Run-Agent {
    param([string[]]$AgentArgs)
    $cmd = ($AgentArgs -join " ")
    Write-Log ">>> python agent.py $cmd"
    & $Python "$AgentDir\agent.py" @AgentArgs 2>&1 | ForEach-Object {
        Write-Host $_
        Add-Content -Path $LogFile -Value $_
    }
    Write-Log "<<< done: $cmd"
}

function Now-BST-HHMM {
    # Returns local time as integer HHMM for easy comparison
    $n = Get-Date
    return ($n.Hour * 100 + $n.Minute)
}

function Test-PrescanDone {
    $today = (Get-Date).ToString("yyyy-MM-dd")
    $found = Get-Content "$AgentDir\audit.log" -ErrorAction SilentlyContinue |
             Where-Object { $_ -match $today -and $_ -match "PRESCAN_DONE" }
    return ($null -ne $found -and @($found).Count -gt 0)
}

function Test-LastScanSkipped {
    $today = (Get-Date).ToString("yyyy-MM-dd")
    $lines = @(Get-Content "$AgentDir\audit.log" -ErrorAction SilentlyContinue |
               Where-Object { $_ -match $today -and ($_ -match "SCAN_SKIPPED|SCAN_DONE|ORDER_PLACED|NO_ENTRY") })
    if ($lines.Count -eq 0) { return $false }
    return $lines[-1] -match "SCAN_SKIPPED"
}

Write-Log "=== Trading day started ==="

# ── Pre-market research: 14:00 BST (9:00 ET) ─────────────────────────────────
Write-Log "Waiting for research time (14:00 BST / 9:00 ET)..."
while ((Now-BST-HHMM) -lt 1400) {
    Start-Sleep -Seconds 20
}
Run-Agent @("--research")

# ── Wait for prescan time: 14:33 BST ─────────────────────────────────────────
Write-Log "Waiting for prescan time (14:33 BST / 9:33 ET)..."
while ((Now-BST-HHMM) -lt 1433) {
    Start-Sleep -Seconds 20
}
Run-Agent @("--prescan")
$prescanDone = Test-PrescanDone

# ── Main trading loop ─────────────────────────────────────────────────────────
$lastScan               = [DateTime]::MinValue
$lastMonitor            = [DateTime]::MinValue
$ScanIntervalM          = 5
$MonitorIntervalM       = 2
$forceClosed            = $false
$afternoonPrescanDone   = $false

Write-Log "Entering main loop (scan every ${ScanIntervalM}min, monitor every ${MonitorIntervalM}min)..."

while ($true) {
    $hhmm = Now-BST-HHMM
    $now  = Get-Date

    # Force close at 20:44 BST (15:44 ET)
    if ($hhmm -ge 2044 -and -not $forceClosed) {
        Run-Agent @("--close")
        $forceClosed = $true
        break
    }

    # Stop entering new scans after 20:30 BST (15:30 ET) — let monitor handle final mins
    $scanAllowed = ($hhmm -lt 2030)

    # Afternoon prescan refresh at 18:05 BST (13:05 ET) — fresh RVOL after midday block
    if (-not $afternoonPrescanDone -and $hhmm -ge 1805 -and $hhmm -lt 1815) {
        Write-Log "Afternoon session refresh — running prescan with fresh data (18:05 BST / 13:05 ET)..."
        Run-Agent @("--prescan")
        $afternoonPrescanDone = $true
        $lastScan = [DateTime]::MinValue  # trigger immediate scan after refresh
    }

    if ($scanAllowed -and (($now - $lastScan).TotalMinutes -ge $ScanIntervalM)) {
        # Fix 3: auto-prescan if prescan was previously skipped (regime was NO_TRADE)
        if (-not $prescanDone) {
            Write-Log "Prescan not yet done — running prescan before scan..."
            Run-Agent @("--prescan")
            $prescanDone = Test-PrescanDone
        }

        Run-Agent @("--scan")
        $lastScan = $now

        # Fix 4: if scan was skipped (regime still blocked), retry in 2 min instead of 5
        if (Test-LastScanSkipped) {
            Write-Log "Scan skipped — will retry in ~2 min"
            $lastScan = $now.AddMinutes(-($ScanIntervalM - 2))
        }
    }

    # Monitor every 2 min
    if (($now - $lastMonitor).TotalMinutes -ge $MonitorIntervalM) {
        Run-Agent @("--monitor")
        $lastMonitor = $now
    }

    Start-Sleep -Seconds 60
}

# ── 15:55 ET emergency flatness verification ─────────────────────────────────
Write-Log "Waiting for 15:55 ET verify window (20:55 BST)..."
while ((Now-BST-HHMM) -lt 2055) {
    Start-Sleep -Seconds 15
}
Run-Agent @("--verify")

# ── EOD reports ───────────────────────────────────────────────────────────────
Write-Log "Market closed. Running EOD reports..."
Start-Sleep -Seconds 60   # let final fills settle
Run-Agent @("--report")
Start-Sleep -Seconds 300  # 5 min for Alpaca to fully settle
Run-Agent @("--performance")

Write-Log "=== Trading day complete ==="
