<#
Deploy a commit to the PC's Serena runtime and restart its services.

Run on the PC (from the laptop: ssh pc "powershell -NoProfile -File
C:\Users\ragha\serena-runtime\scripts\deploy-pc.ps1 -Commit <sha>").

Two things went wrong when this was done by hand on 2026-09-22:
  * the brain was force-killed, which leaves no stop record, so the doctor
    texted him "her brain died without recording why";
  * Start-ScheduledTask ran while the old brain's wrapper was still exiting,
    so Windows ignored it as a duplicate (MultipleInstances=IgnoreNew) and
    the brain stayed down until the 5-minute keep-alive fired.
So the stop is recorded as a redeploy first, the task is allowed to settle,
and the script does not return until the brain is listening again.

Fleet is left alone unless -IncludeFleet: restarting it kills running workers.
#>
param(
    [Parameter(Mandatory = $true)][string]$Commit,
    [switch]$IncludeFleet
)
$ErrorActionPreference = 'Stop'
$runtime = (Resolve-Path "$PSScriptRoot\..").Path
$python = Join-Path $runtime '.venv\Scripts\python.exe'
$task = 'Serena Brain Daemon'
$ledger = Join-Path $env:USERPROFILE '.local\state\serena\brain-uptime.jsonl'

Set-Location $runtime
git fetch origin --quiet
git checkout --quiet $Commit
if ($LASTEXITCODE -ne 0) { throw "could not check out $Commit" }
$deployed = (git rev-parse --short HEAD)
"deployed: $deployed"

$pattern = 'automation serve|webhook serve|voice-host|brain_daemon'
if ($IncludeFleet) { $pattern = "$pattern|fleet serve" }
$targets = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -match $pattern }

# Say why she is stopping before she is stopped; a kill leaves no trace itself.
foreach ($brain in $targets | Where-Object { $_.CommandLine -match 'brain_daemon' }) {
    & $python -c "from core import brain_downtime; brain_downtime.record_stop($($brain.ProcessId), 'redeploy', detail='deploy $deployed')"
}
$lastStart = (Get-Content $ledger -Tail 1 -ErrorAction SilentlyContinue)

$targets | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# The wrapper outlives the brain by a moment; starting while it still runs is ignored.
$deadline = (Get-Date).AddSeconds(45)
while ((Get-ScheduledTask -TaskName $task).State -eq 'Running' -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
}
Start-ScheduledTask -TaskName $task

$deadline = (Get-Date).AddSeconds(90)
$up = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    $latest = (Get-Content $ledger -Tail 1 -ErrorAction SilentlyContinue)
    if ($latest -and $latest -ne $lastStart -and $latest -match '"event": "start"') { $up = $true; break }
}
if (-not $up) { throw "the brain did not come back within 90s; check brain.stdout.log" }
"brain: $latest"

Start-Sleep -Seconds 10
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
    Where-Object { $_.CommandLine -match 'automation serve|fleet serve|webhook serve|voice-host|brain_daemon' } |
    ForEach-Object { [regex]::Match($_.CommandLine, 'automation serve|fleet serve|webhook serve|voice-host|brain_daemon').Value } |
    Group-Object | ForEach-Object { "$($_.Name) x$($_.Count)" }
