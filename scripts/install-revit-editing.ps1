param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '../.state/revit-client/config.json'),
    [string]$SourcePath = (Join-Path $PSScriptRoot '../.state/revit-mcp-source'),
    [switch]$EnableWrites
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
dotnet build (Join-Path $repo 'integrations/revit/addin/SonneRevitCommands.csproj') -c Release --nologo
if ($LASTEXITCODE) { throw 'Sonne Revit commands build failed.' }
$commandsDirectory = Join-Path $env:APPDATA 'Autodesk/Revit/Addins/2027/revit_mcp_plugin/Commands'
if (!(Test-Path -LiteralPath $commandsDirectory)) { throw 'Install the Revit client first.' }
$destination = Join-Path $commandsDirectory 'SonneCommands/2027'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'integrations/revit/addin/bin/Release/net10.0-windows10.0.19041.0/SonneRevitCommands.dll') -Destination $destination -Force
$registryPath = Join-Path $commandsDirectory 'commandRegistry.json'
$registry = Get-Content -LiteralPath $registryPath -Raw -Encoding UTF8 | ConvertFrom-Json
$commands = @($registry.commands | Where-Object commandName -notin @('sonne_get_element','sonne_edit_element'))
foreach ($name in @('sonne_get_element','sonne_edit_element')) {
    $commands += @{commandName=$name; assemblyPath='SonneCommands/{VERSION}/SonneRevitCommands.dll'; enabled=$true; supportedRevitVersions=@('2027')}
}
$registry.commands = $commands
[IO.File]::WriteAllText($registryPath, ($registry | ConvertTo-Json -Depth 8))
Copy-Item -LiteralPath (Join-Path $repo 'integrations/revit/upstream-tools.ts') -Destination (Join-Path $SourcePath 'server/src/tools/sonne_elements.ts') -Force
Push-Location (Join-Path $SourcePath 'server')
try { npm.cmd run build; if ($LASTEXITCODE) { throw 'Windows MCP build failed.' } } finally { Pop-Location }
$client = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($EnableWrites -or !($client.PSObject.Properties.Name -contains 'enableWrites')) {
    $client | Add-Member -NotePropertyName enableWrites -NotePropertyValue ([bool]$EnableWrites) -Force
}
[IO.File]::WriteAllText($ConfigPath, ($client | ConvertTo-Json -Depth 8))
Write-Host 'Sonne commands installed. Toggle Revit MCP Switch off/on, then restart the Windows client.'
