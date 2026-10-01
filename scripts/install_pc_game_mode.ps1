param(
    [string]$Python = 'C:\Python313\pythonw.exe',
    [string]$StateDirectory = "$env:LOCALAPPDATA\SerenaGameMode",
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$taskName = 'Serena PC Game Mode'
$source = Join-Path $PSScriptRoot 'pc_game_mode.py'
$config = Join-Path $StateDirectory 'config.json'
if ($Uninstall) {
    $configuration = Get-Content $config -Raw | ConvertFrom-Json
    $configuration.enabled = $false
    $configuration | ConvertTo-Json -Depth 10 | Set-Content $config -Encoding UTF8
    # Let the watcher restore and exit, then remove its watchdog.
    $journal = Join-Path $StateDirectory 'restore.json'
    $pending = $false
    for ($attempt = 0; $attempt -lt 10; $attempt++) {
        Start-Sleep -Seconds 2
        $pending = $false
        if (Test-Path $journal) {
            $saved = Get-Content $journal -Raw | ConvertFrom-Json
            $pending = @($saved.vms.PSObject.Properties).Count -gt 0 -or @($saved.priorities.PSObject.Properties).Count -gt 0
        }
        if (-not $pending) { break }
    }
    if ($pending) { throw 'Restoration is still pending; leaving the watchdog installed to retry.' }
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output 'Watchdog removed; CPU restoration journal is empty.'
    exit
}
if (-not (Test-Path $source)) { throw "Missing source: $source" }
if (-not (Test-Path $Python)) { throw "Missing Python: $Python" }
New-Item $StateDirectory -ItemType Directory -Force | Out-Null
if (-not (Test-Path $config)) {
    Copy-Item (Join-Path $PSScriptRoot '..\config\pc-game-mode.example.json') $config
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$source`" --config `"$config`" --state-dir `"$StateDirectory`" watch"
$login = New-ScheduledTaskTrigger -AtLogOn -User $identity
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(10) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($login, $watchdog) -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName,State
