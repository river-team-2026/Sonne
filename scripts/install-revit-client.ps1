param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot '../.state/revit-client/config.json'),
    [string]$SourcePath,
    [ValidateSet('2026','2027')][string]$RevitVersion = '2027'
)
$ErrorActionPreference = 'Stop'
if (Get-Process Revit -ErrorAction SilentlyContinue) { throw 'Close Revit before installing the add-in.' }
$repo = Split-Path $PSScriptRoot -Parent
if (!$SourcePath) {
    $sourceFolder = if ($RevitVersion -eq '2026') { 'revit-mcp-source-2026' } else { 'revit-mcp-source' }
    $SourcePath = Join-Path $repo ".state/$sourceFolder"
}
$buildConfiguration = 'Release R' + $RevitVersion.Substring(2)
$SourcePath = [IO.Path]::GetFullPath($SourcePath)
$ConfigPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($ConfigPath)
if (!(Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw 'Copy the private enrollment file to .state/revit-client/config.json or supply -ConfigPath.' }
$revision = '86cf7057785f2423fc0d0892c9a5d9c93deb1aa3'
if (!(Test-Path -LiteralPath $SourcePath)) {
    git clone https://github.com/mcp-servers-for-revit/mcp-servers-for-revit.git $SourcePath
    if ($LASTEXITCODE) { throw 'Upstream clone failed.' }
    git -C $SourcePath checkout --detach $revision
}
if ((git -C $SourcePath rev-parse HEAD) -ne $revision) { throw 'Unexpected upstream revision; use a new source directory.' }
python (Join-Path $PSScriptRoot 'prepare-revit-2027.py') $SourcePath
if ($LASTEXITCODE) { throw 'Sonne Revit adaptation failed.' }
Copy-Item -LiteralPath (Join-Path $repo 'integrations/revit/upstream-package-lock.json') -Destination (Join-Path $SourcePath 'server/package-lock.json')
Push-Location (Join-Path $SourcePath 'server')
try {
    npm.cmd ci
    if ($LASTEXITCODE) { throw 'Upstream dependencies failed.' }
    npm.cmd run build
    if ($LASTEXITCODE) { throw 'Upstream MCP build failed.' }
} finally { Pop-Location }
dotnet build (Join-Path $SourcePath 'plugin/RevitMCPPlugin.csproj') -c $buildConfiguration --nologo
if ($LASTEXITCODE) { throw 'Revit add-in build failed.' }
dotnet build (Join-Path $SourcePath 'commandset/RevitMCPCommandSet.csproj') -c $buildConfiguration --nologo
if ($LASTEXITCODE) { throw 'Revit commands build failed.' }
$output = Join-Path $SourcePath "plugin/bin/AddIn $RevitVersion $buildConfiguration"
$target = Join-Path $env:APPDATA "Autodesk/Revit/Addins/$RevitVersion"
New-Item -ItemType Directory -Path $target -Force | Out-Null
# Back up this add-in before an update; other add-ins stay in place.
$existing = Join-Path $target 'revit_mcp_plugin'
if (Test-Path -LiteralPath $existing) {
    $backup = Join-Path $repo ('.state/revit-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $backup | Out-Null
    Copy-Item -LiteralPath $existing -Destination $backup -Recurse
}
Copy-Item -LiteralPath (Join-Path $output 'revit_mcp_plugin') -Destination $target -Recurse -Force
Copy-Item -LiteralPath (Join-Path $output 'mcp-servers-for-revit.addin') -Destination $target -Force
$commands = @('get_current_view_info', 'get_selected_elements', 'ai_element_filter', 'analyze_model_statistics') | ForEach-Object {
    @{commandName=$_; assemblyPath='RevitMCPCommandSet/{VERSION}/RevitMCPCommandSet.dll'; enabled=$true; supportedRevitVersions=@($RevitVersion)}
}
[IO.File]::WriteAllText((Join-Path $existing 'Commands/commandRegistry.json'), (@{commands=@($commands)} | ConvertTo-Json -Depth 8))
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
icacls $ConfigPath /inheritance:r /grant:r "*${sid}:F" '*S-1-5-18:F' | Out-Null
if ($LASTEXITCODE) { throw 'Could not protect the client credentials.' }
$client = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
$client | Add-Member -NotePropertyName command -NotePropertyValue (Get-Command node).Source -Force
$client | Add-Member -NotePropertyName args -NotePropertyValue @((Join-Path $SourcePath 'server/build/index.js')) -Force
$client | Add-Member -NotePropertyName cwd -NotePropertyValue (Join-Path $SourcePath 'server') -Force
[IO.File]::WriteAllText($ConfigPath, ($client | ConvertTo-Json -Depth 8))
Push-Location (Join-Path $repo 'integrations/revit')
try { npm.cmd ci --omit=dev --ignore-scripts; if ($LASTEXITCODE) { throw 'Bridge dependencies failed.' } } finally { Pop-Location }
if ($RevitVersion -eq '2027') {
    & (Join-Path $PSScriptRoot 'install-revit-editing.ps1') -ConfigPath $ConfigPath -SourcePath $SourcePath
    if ($LASTEXITCODE) { throw 'Sonne commands installation failed.' }
    & (Join-Path $PSScriptRoot 'install-revit-full.ps1') -SourcePath $SourcePath
    if ($LASTEXITCODE) { throw 'Full guarded commands installation failed.' }
    & (Join-Path $PSScriptRoot 'install-revit-procedures.ps1') -SourcePath $SourcePath
    if ($LASTEXITCODE) { throw 'Parameter procedures installation failed.' }
    & (Join-Path $PSScriptRoot 'install-revit-publications.ps1') -SourcePath $SourcePath
    if ($LASTEXITCODE) { throw 'Room publication reader installation failed.' }
}
Write-Host "Revit $RevitVersion add-in installed. Open a test model and start scripts/start-revit-client.cmd."
