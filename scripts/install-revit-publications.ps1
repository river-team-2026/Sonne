param([string]$SourcePath = (Join-Path $PSScriptRoot '../.state/revit-mcp-source'))
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
dotnet build (Join-Path $repo 'integrations/revit/addin/SonneRoomPublications.csproj') -c Release --nologo
if ($LASTEXITCODE) { throw 'The Revit publication reader could not be built.' }
$directory = Join-Path $env:APPDATA 'Autodesk/Revit/Addins/2027/revit_mcp_plugin/Commands'
$registryPath = Join-Path $directory 'commandRegistry.json'
$registry = Get-Content -LiteralPath $registryPath -Raw -Encoding UTF8 | ConvertFrom-Json
$destination = Join-Path $directory 'SonneRoomPublications/2027'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'integrations/revit/addin/bin/Release/net10.0-windows10.0.19041.0/SonneRoomPublicationReader.dll') -Destination $destination -Force
$name = 'sonne_extract_room_publication'
$registry.commands = @($registry.commands | Where-Object commandName -ne $name)
$registry.commands += @{commandName=$name;assemblyPath='SonneRoomPublications/{VERSION}/SonneRoomPublicationReader.dll';enabled=$true;supportedRevitVersions=@('2027')}
[IO.File]::WriteAllText($registryPath, ($registry | ConvertTo-Json -Depth 8))
Copy-Item -LiteralPath (Join-Path $repo 'integrations/revit/upstream-publications.ts') -Destination (Join-Path $SourcePath 'server/src/tools/sonne_publications.ts') -Force
New-Item -ItemType Directory -Path (Join-Path $env:LOCALAPPDATA 'Sonne/RevitPublications/inbox') -Force | Out-Null
Push-Location (Join-Path $SourcePath 'server')
try { npm.cmd run build; if ($LASTEXITCODE) { throw 'Windows MCP build failed.' } } finally { Pop-Location }
Write-Host 'Publication reader installed. Reload Revit MCP commands and restart the Windows client.'
