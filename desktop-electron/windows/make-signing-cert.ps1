<#
.SYNOPSIS
Create the self-signed code-signing certificate Serena's Windows builds use.

.DESCRIPTION
electron-updater's real protection on Windows is that an update must be signed
by the same publisher as the installed app. Without any signature that check
degrades to "trust whatever the feed served", so a compromised release channel
could push code that runs as the user.

A paid certificate buys one extra thing: SmartScreen stops warning strangers who
download the app. There are no strangers here, so this generates a free
self-signed key instead. It gives the same-key-as-install guarantee; the only
cost is clicking through SmartScreen on a manual install.

The .pfx is written OUTSIDE the repository on purpose and must never be
committed. Losing it means future updates cannot be applied over the installs
signed with it, so keep a copy somewhere safe.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File windows\make-signing-cert.ps1
  $env:CSC_LINK="file:///C:/Users/ragha/.config/serena/serena-code-signing.pfx"
  $env:CSC_KEY_PASSWORD="<the password you chose>"
  npm run dist:win
#>
[CmdletBinding()]
param(
    [string] $Subject = "CN=Raghav Dua, O=Serena, C=CA",
    [string] $OutDir = "$env:USERPROFILE\.config\serena",
    [int] $Years = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$pfxPath = Join-Path $OutDir "serena-code-signing.pfx"
if (Test-Path -LiteralPath $pfxPath) {
    throw "A signing certificate already exists at $pfxPath. Reuse it: replacing it breaks updates for every install signed with the old key."
}

New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

Write-Host "Creating a self-signed code-signing certificate for $Subject"
$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject $Subject `
    -KeyUsage DigitalSignature `
    -KeyExportPolicy Exportable `
    -KeyLength 3072 `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -NotAfter (Get-Date).AddYears($Years)

$password = Read-Host -AsSecureString "Choose a password for the .pfx"
Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $password | Out-Null

# Trusting it locally is what stops Windows treating Serena's own installer as
# an unknown publisher on this machine.
$publicPath = Join-Path $OutDir "serena-code-signing.cer"
Export-Certificate -Cert $cert -FilePath $publicPath | Out-Null
Import-Certificate -FilePath $publicPath -CertStoreLocation "Cert:\CurrentUser\Root" | Out-Null

Write-Host ""
Write-Host "Certificate written to $pfxPath"
Write-Host "Thumbprint: $($cert.Thumbprint)"
Write-Host ""
Write-Host "Build a signed release with:"
Write-Host "  `$env:CSC_LINK='file:///$($pfxPath -replace '\\','/')'"
Write-Host "  `$env:CSC_KEY_PASSWORD='<password>'"
Write-Host "  npm run dist:win"
Write-Host ""
Write-Host "Back this file up. Losing it means installs signed with it can never be updated."
