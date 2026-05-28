# setup_tasks.ps1 — recreate all LeelaDayTrade Windows Task Scheduler tasks
# Run elevated (right-click → "Run as Administrator") on a new machine or after a reset.
# Times are LOCAL (BST in summer / GMT in winter). Adjust if clocks have changed.
#
# Schedule (BST / ET):
#   13:30 / 08:30  Research-Alpaca       --research
#   13:30 / 08:30  Research-IBKR         --research
#   14:40 / 09:40  Precheck              --precheck
#   14:45 / 09:45  Morning               --prescan   (Alpaca)
#   14:45 / 09:45  Morning-IBKR          --prescan   (IBKR)
#   14:50 / 09:50  Continuous            --continuous (Alpaca, adaptive loop until 15:30 ET)
#   14:50 / 09:50  Continuous-IBKR       --continuous (IBKR,   adaptive loop until 15:30 ET)
#   15:00 / 10:00  Monitor               --monitor-loop (Alpaca, 45s cadence until flat)
#   15:00 / 10:00  Monitor-IBKR          --monitor-loop (IBKR,   45s cadence until flat)
#   20:30 / 15:30  Cutoff                --cutoff
#   20:44 / 15:44  Close                 --close
#   20:55 / 15:55  Verify                --verify
#   21:15 / 16:15  Report                --report
#   21:30 / 16:30  Performance           --performance

$user    = "$env:USERDOMAIN\$env:USERNAME"
$alpaca  = "C:\Users\leela\leela-daytrading-agent"
$ibkr    = "C:\Users\leela\leela-ibkr-agent"

function New-DailyTask {
    param($Name, $Time, $WorkDir, $Args, $Log)
    $cmd = "cmd /c cd $WorkDir && python agent.py $Args >> $WorkDir\$Log 2>&1"
    schtasks /create /tn $Name /tr $cmd /sc daily /st $Time /ru $user /rl HIGHEST /f
    if ($LASTEXITCODE -eq 0) { Write-Host "OK  $Name  ($Time)" }
    else                     { Write-Host "ERR $Name  exit=$LASTEXITCODE" }
}

New-DailyTask "LeelaDayTrade-Research-Alpaca" "13:30" $alpaca "--research"    "research.log"
New-DailyTask "LeelaDayTrade-Research-IBKR"   "13:30" $ibkr   "--research"    "research.log"
New-DailyTask "LeelaDayTrade-Precheck"         "14:40" $alpaca "--precheck"    "precheck.log"
New-DailyTask "LeelaDayTrade-Morning"          "14:45" $alpaca "--prescan"     "morning.log"
New-DailyTask "LeelaDayTrade-Morning-IBKR"     "14:45" $ibkr   "--prescan"     "morning.log"
New-DailyTask "LeelaDayTrade-Continuous"       "14:50" $alpaca "--continuous"  "continuous.log"
New-DailyTask "LeelaDayTrade-Continuous-IBKR"  "14:50" $ibkr   "--continuous"  "continuous.log"
New-DailyTask "LeelaDayTrade-Monitor"          "15:00" $alpaca "--monitor-loop" "monitor.log"
New-DailyTask "LeelaDayTrade-Monitor-IBKR"     "15:00" $ibkr   "--monitor-loop" "monitor.log"
New-DailyTask "LeelaDayTrade-Cutoff"           "20:30" $alpaca "--cutoff"      "cutoff.log"
New-DailyTask "LeelaDayTrade-Close"            "20:44" $alpaca "--close"       "close.log"
New-DailyTask "LeelaDayTrade-Verify"           "20:55" $alpaca "--verify"      "verify.log"
New-DailyTask "LeelaDayTrade-Report"           "21:15" $alpaca "--report"      "report.log"
New-DailyTask "LeelaDayTrade-Performance"      "21:30" $alpaca "--performance" "performance.log"

Write-Host "`nDone. Verify with: Get-ScheduledTask | Where TaskName -like 'LeelaDayTrade*'"
