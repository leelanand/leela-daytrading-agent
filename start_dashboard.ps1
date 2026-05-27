# Leela Trading Dashboard — auto-start launcher
# Registered as a Task Scheduler task that runs at user logon.
# Kills any existing dashboard process on port 8765 before starting fresh.

$Python    = "C:\Users\leela\AppData\Local\Programs\Python\Python312\python.exe"
$Script    = "C:\Users\leela\leela-daytrading-agent\dashboard.py"
$AgentDir  = "C:\Users\leela\leela-daytrading-agent"

# Kill any existing instance on port 8765
$existing = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
if ($existing) {
    $pid8765 = $existing | Select-Object -ExpandProperty OwningProcess -First 1
    Stop-Process -Id $pid8765 -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

Set-Location $AgentDir
& $Python $Script
