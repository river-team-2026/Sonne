param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '../.state/revit-client/config.json'),
    [string]$CertificatePath
)
$ErrorActionPreference = 'Stop'
if ($CertificatePath) { $env:NODE_EXTRA_CA_CERTS = [IO.Path]::GetFullPath($CertificatePath) }
node (Join-Path $PSScriptRoot '../integrations/revit/client.mjs') ([IO.Path]::GetFullPath($ConfigPath))
exit $LASTEXITCODE
