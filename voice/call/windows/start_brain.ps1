$ErrorActionPreference = 'Stop'
$runtime = (Resolve-Path "$PSScriptRoot\..\..\..").Path

$env:SERENA_PROJECTS_DIR = 'C:\Users\ragha\Projects'

# Her memory and knowledge live in the synced Projects tree, not in whatever
# checkout happens to be running her. A runtime copy is a snapshot: the brain
# that read one answered calls with a July view of his life, hundreds of
# memories and knowledge files behind, and sounded stupid for it.
$live = Join-Path $env:SERENA_PROJECTS_DIR 'serena'
foreach ($pair in @(
    @{ Name = 'MEMORY_DIR';   Live = (Join-Path $live 'memory');      Fallback = "$runtime\memory" },
    @{ Name = 'KNOWLEDGE_DIR'; Live = (Join-Path $live 'knowledge');  Fallback = "$runtime\knowledge" },
    @{ Name = 'PERSONA_FILE'; Live = (Join-Path $live 'Persona.md');  Fallback = "$runtime\Persona.md" }
)) {
    $value = if (Test-Path $pair.Live) { $pair.Live } else { $pair.Fallback }
    Set-Item -Path "Env:$($pair.Name)" -Value $value
}
$env:SERENA_BRAIN_STATE_SNAPSHOT = "$env:USERPROFILE\.config\serena\canonical_state.json"

# Which brain answers a call is a setting, not a constant. The file is read
# first so it can name a provider and model, and every value below is a
# default that only applies when nothing has chosen one.
$brainEnv = Join-Path $env:USERPROFILE '.config\serena\service-brain.env'
if (Test-Path $brainEnv) {
    foreach ($line in Get-Content $brainEnv) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $name, $value = $line -split '=', 2
        Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim().Trim("'", '"')
    }
}
# A headless brain cannot complete an interactive OAuth flow, so the Claude
# providers need a long-lived setup token on disk. Without it every turn ends
# as "OAuth session expired" -- an answer he hears, not an error he sees.
$oauth = Join-Path $env:USERPROFILE '.config\serena\claude-oauth.env'
if (Test-Path $oauth) {
    foreach ($line in Get-Content $oauth) {
        $name, $value = $line -split '=', 2
        if ($name -eq 'CLAUDE_CODE_OAUTH_TOKEN' -and $value) {
            $env:CLAUDE_CODE_OAUTH_TOKEN = $value.Trim().Trim("'", '"')
        }
    }
}
if (-not $env:SERENA_BRAIN_MODEL) { $env:SERENA_BRAIN_MODEL = 'sonnet' }
if (-not $env:SERENA_BRAIN_EFFORT) { $env:SERENA_BRAIN_EFFORT = 'low' }
$meteredOverrides = @(
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_AWS_API_KEY',
    'ANTHROPIC_AWS_BASE_URL',
    'ANTHROPIC_BASE_URL',
    'ANTHROPIC_BEDROCK_BASE_URL',
    'ANTHROPIC_BEDROCK_MANTLE_BASE_URL',
    'ANTHROPIC_CUSTOM_HEADERS',
    'ANTHROPIC_FOUNDRY_API_KEY',
    'ANTHROPIC_FOUNDRY_AUTH_TOKEN',
    'ANTHROPIC_FOUNDRY_BASE_URL',
    'ANTHROPIC_FOUNDRY_RESOURCE',
    'ANTHROPIC_IDENTITY_TOKEN',
    'ANTHROPIC_IDENTITY_TOKEN_FILE',
    'ANTHROPIC_VERTEX_BASE_URL',
    'ANTHROPIC_VERTEX_PROJECT_ID',
    'AWS_BEARER_TOKEN_BEDROCK',
    'CLAUDE_CODE_API_BASE_URL',
    'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR',
    'CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL',
    'CLAUDE_CODE_CUSTOM_OAUTH_URL',
    'CLAUDE_CODE_ENABLE_PROXY_AUTH_HELPER',
    'CLAUDE_CODE_GB_BASE_URL',
    'CLAUDE_CODE_HOST_AUTH_ENV_VAR',
    'CLAUDE_CODE_HTTP_PROXY',
    'CLAUDE_CODE_HTTPS_PROXY',
    'CLAUDE_CODE_PROXY_AUTHENTICATE',
    'CLAUDE_CODE_PROXY_URL',
    'CLAUDE_CODE_SESSION_ACCESS_TOKEN',
    'CLAUDE_CODE_USE_ANTHROPIC_AWS',
    'CLAUDE_CODE_USE_BEDROCK',
    'CLAUDE_CODE_USE_FOUNDRY',
    'CLAUDE_CODE_USE_MANTLE',
    'CLAUDE_CODE_USE_VERTEX'
)
foreach ($name in $meteredOverrides) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
$subscriptionOAuth = @(
    'CLAUDE_CODE_OAUTH_REFRESH_TOKEN',
    'CLAUDE_CODE_OAUTH_TOKEN',
    'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR'
)
Get-ChildItem Env: | Where-Object {
    ($_.Name -like 'ANTHROPIC_*') -or
    ($_.Name -like 'FOUNDRY_*') -or
    ($_.Name -like 'VERTEX_*') -or
    (($_.Name -like 'CLAUDE_CODE_*') -and ($subscriptionOAuth -notcontains $_.Name))
} | ForEach-Object {
    Remove-Item "Env:$($_.Name)" -ErrorAction SilentlyContinue
}

Set-Location $runtime
& "$runtime\.venv\Scripts\python.exe" -u -m core.brain_daemon `
    1>> "$runtime\brain.stdout.log" 2>> "$runtime\brain.stderr.log"
exit $LASTEXITCODE
