$ErrorActionPreference = 'Stop'
$runtime = (Resolve-Path "$PSScriptRoot\..\..\..").Path

Set-Location $runtime
& "$runtime\.venv\Scripts\python.exe" -u -m core.brain_soak `
    --duration-hours 24 `
    --sample-seconds 60 `
    --checkpoint-hours 0,6,12,18,24 `
    --require-pass `
    1>> "$runtime\brain-soak.stdout.log" `
    2>> "$runtime\brain-soak.stderr.log"
exit $LASTEXITCODE
