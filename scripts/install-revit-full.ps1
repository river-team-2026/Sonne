param(
    [string]$SourcePath = (Join-Path $PSScriptRoot '../.state/revit-mcp-source')
)
$ErrorActionPreference = 'Stop'
if ((git -C $SourcePath rev-parse HEAD) -ne '86cf7057785f2423fc0d0892c9a5d9c93deb1aa3') { throw 'Unexpected upstream revision; install the pinned Revit client first.' }
python (Join-Path $PSScriptRoot 'prepare-revit-full.py') $SourcePath
if ($LASTEXITCODE) { throw 'Guarded command preparation failed.' }
dotnet build (Join-Path $SourcePath 'commandset/RevitMCPCommandSet.csproj') -c 'Release R27' --nologo
if ($LASTEXITCODE) { throw 'Full command set build failed.' }
$commandsDirectory = Join-Path $env:APPDATA 'Autodesk/Revit/Addins/2027/revit_mcp_plugin/Commands'
if (!(Test-Path -LiteralPath $commandsDirectory)) { throw 'Install the Revit client first.' }
$destination = Join-Path $commandsDirectory 'SonneUpstreamV2/2027'
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$assemblyName = 'SonneUpstreamCommandsV2.dll'
$output = Join-Path $SourcePath 'commandset/bin/Release R27'
if (!(Test-Path -LiteralPath (Join-Path $output $assemblyName))) { throw 'Command assembly output is missing.' }
Copy-Item -Path (Join-Path $output '*.dll') -Destination $destination -Force
$registryPath = Join-Path $commandsDirectory 'commandRegistry.json'
$registry = Get-Content -LiteralPath $registryPath -Raw -Encoding UTF8 | ConvertFrom-Json
Copy-Item -LiteralPath $registryPath -Destination (Join-Path (Split-Path $PSScriptRoot -Parent) ('.state/revit-registry-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff') + '.json'))
$names = @(Get-Content -LiteralPath (Join-Path $SourcePath 'sonne-full-commands.json') -Raw -Encoding UTF8 | ConvertFrom-Json)
$commands = @($registry.commands | Where-Object commandName -notin $names)
foreach ($name in $names) {
    $commands += @{commandName=$name; assemblyPath=('SonneUpstreamV2/{VERSION}/' + $assemblyName); enabled=$true; supportedRevitVersions=@('2027')}
}
$registry.commands = $commands
[IO.File]::WriteAllText($registryPath, ($registry | ConvertTo-Json -Depth 8))
Push-Location (Join-Path $SourcePath 'server')
try { npm.cmd run build; if ($LASTEXITCODE) { throw 'Windows MCP build failed.' } } finally { Pop-Location }
Write-Host 'Full guarded MCP installed. Reload Revit MCP Switch off/on and restart the Windows client.'
