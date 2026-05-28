# fix_broken_tasks.ps1 — patch the 4 broken tasks that have empty arguments
# Run elevated: right-click → "Run as Administrator"

$user   = "$env:USERDOMAIN\$env:USERNAME"
$alpaca = "C:\Users\leela\leela-daytrading-agent"

$fixes = @(
    @{ Name = "LeelaDayTrade-Precheck";    Time = "14:40"; Flag = "--precheck";    Log = "precheck.log" }
    @{ Name = "LeelaDayTrade-Cutoff";      Time = "20:30"; Flag = "--cutoff";      Log = "cutoff.log" }
    @{ Name = "LeelaDayTrade-Report";      Time = "21:15"; Flag = "--report";      Log = "report.log" }
    @{ Name = "LeelaDayTrade-Performance"; Time = "21:30"; Flag = "--performance"; Log = "performance.log" }
)

foreach ($f in $fixes) {
    $cmd = "cmd /c cd $alpaca && python agent.py $($f.Flag) >> $alpaca\$($f.Log) 2>&1"
    schtasks /create /tn $f.Name /tr $cmd /sc daily /st $f.Time /ru $user /rl HIGHEST /f
    if ($LASTEXITCODE -eq 0) { Write-Host "FIXED  $($f.Name)" }
    else                     { Write-Host "ERROR  $($f.Name)  exit=$LASTEXITCODE" }
}

Write-Host ""
Write-Host "Verify:"
foreach ($f in $fixes) {
    $t = Get-ScheduledTask -TaskName $f.Name -ErrorAction SilentlyContinue
    $args = $t.Actions[0].Arguments
    $status = if ($args) { "OK" } else { "STILL BROKEN" }
    Write-Host "  $status  $($f.Name)  |  $args"
}
