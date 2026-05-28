# Leela Trading Dashboard — auto-start launcher
# Registered as a Task Scheduler task that runs at user logon.
# Kills any existing dashboard process on port 8765 before starting fresh.

$Python    = "C:\Users\leela\AppData\Local\Programs\Python\Python312\python.exe"
$Script    = "C:\Users\leela\leela-daytrading-agent\dashboard.py"
$AgentDir  = "C:\Users\leela\leela-daytrading-agent"

# Kill ALL existing instances on port 8765 (guard against zombie duplicates)
$existing = Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
if ($existing) {
    $existing | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
        Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
}

Set-Location $AgentDir
& $Python $Script
