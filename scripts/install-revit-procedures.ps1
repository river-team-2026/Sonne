param([string]$SourcePath = (Join-Path $PSScriptRoot '../.state/revit-mcp-source'))
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
dotnet build (Join-Path $repo 'integrations/revit/addin/SonneParameterProcedures.csproj') -c Release --nologo
if ($LASTEXITCODE) { throw 'Parameter procedures build failed.' }
$directory = Join-Path $env:APPDATA 'Autodesk/Revit/Addins/2027/revit_mcp_plugin/Commands'
if (!(Test-Path -LiteralPath $directory)) { throw 'Install the Revit client first.' }
$destination = Join-Path $directory 'SonneProceduresV2/2027'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'integrations/revit/addin/bin/Release/net10.0-windows10.0.19041.0/SonneParameterProceduresV2.dll') -Destination $destination -Force
$path = Join-Path $directory 'commandRegistry.json'
$registry = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
Copy-Item -LiteralPath $path -Destination (Join-Path $repo ('.state/revit-procedures-registry-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff') + '.json'))
$names = @('sonne_audit_parameters','sonne_bulk_set_parameters')
$commands = @($registry.commands | Where-Object commandName -notin $names)
foreach ($name in $names) { $commands += @{commandName=$name;assemblyPath='SonneProceduresV2/{VERSION}/SonneParameterProceduresV2.dll';enabled=$true;supportedRevitVersions=@('2027')} }
$registry.commands = $commands
[IO.File]::WriteAllText($path, ($registry | ConvertTo-Json -Depth 8))
Copy-Item -LiteralPath (Join-Path $repo 'integrations/revit/upstream-procedures.ts') -Destination (Join-Path $SourcePath 'server/src/tools/sonne_procedures.ts') -Force
Push-Location (Join-Path $SourcePath 'server')
try { npm.cmd run build; if ($LASTEXITCODE) { throw 'Windows MCP build failed.' } } finally { Pop-Location }
Write-Host 'Parameter procedures installed. Reload Revit MCP Switch off/on and restart the Windows client.'
