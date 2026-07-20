$ErrorActionPreference = 'Stop'
$runtime = (Resolve-Path "$PSScriptRoot\..\..\..").Path

$env:SERENA_CALL_TTS_BACKEND = 'pocket'
$env:SERENA_CALL_POCKET_PYTHON = "$runtime\.venv-pocket\Scripts\python.exe"
$env:SERENA_CALL_POCKET_THREADS = '2'
$env:SERENA_CALL_POCKET_QUANTIZE = '1'
$env:SERENA_CALL_WHISPER_MODEL = "$runtime\voice\models\faster-whisper-tiny.en"
$env:SERENA_CALL_HOST = '0.0.0.0'
$env:SERENA_CALL_PORT = '8766'

Set-Location $runtime
& "$runtime\.venv\Scripts\python.exe" -u -m voice.call.server `
    1>> "$runtime\call.stdout.log" 2>> "$runtime\call.stderr.log"
