$ErrorActionPreference = 'Stop'
$runtime = (Resolve-Path "$PSScriptRoot\..\..\..").Path

$env:MEMORY_DIR = "$runtime\memory"
$env:KNOWLEDGE_DIR = "$runtime\knowledge"
$env:PERSONA_FILE = "$runtime\Persona.md"
$env:SERENA_BRAIN_STATE_SNAPSHOT = "$env:USERPROFILE\.config\serena\canonical_state.json"
$env:SERENA_PROJECTS_DIR = 'C:\Users\ragha\Projects'
$env:SERENA_BRAIN_MODEL = 'sonnet'
$env:SERENA_BRAIN_EFFORT = 'low'
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
