param([Parameter(Mandatory=$true)][string]$Checkout, [switch]$KeepData)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $Checkout).ProviderPath
$state = Join-Path $root '.state'
$config = Join-Path $state 'revit-client/config.json'
if (!(Test-Path -LiteralPath $config -PathType Leaf)) { return }
if (!(Test-Path -LiteralPath (Join-Path $root 'scripts/install-revit-client.cmd') -PathType Leaf)) {
    throw 'Select the Windows Sonne client checkout.'
}
$addins = Join-Path $env:APPDATA 'Autodesk/Revit/Addins/2027'
$plugin = Join-Path $addins 'revit_mcp_plugin'
$dedicated = $false
if (Test-Path -LiteralPath (Join-Path $plugin 'Commands/SonneCommands')) {
    $answer = Read-Host 'Remove the installed Revit MCP add-in too? Type REMOVE if only Sonne uses it, or press Enter to keep it'
    $dedicated = $answer -ceq 'REMOVE'
}
if ($dedicated -and (Get-Process Revit -ErrorAction SilentlyContinue)) {
    throw 'Close Revit and retry to remove its add-in.'
}
$entry = [IO.Path]::GetFullPath((Join-Path $root 'integrations/revit/client.mjs'))
foreach ($node in (Get-CimInstance Win32_Process -Filter "Name='node.exe'")) {
    if ($node.CommandLine -and $node.CommandLine.Replace('/','\').IndexOf($entry, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        throw 'Press Ctrl+C in the Windows Sonne client terminal, then retry.'
    }
}

function Remove-CheckoutItem([string]$Base, [string]$Relative) {
    $basePath = [IO.Path]::GetFullPath($Base).TrimEnd('\')
    $target = [IO.Path]::GetFullPath((Join-Path $basePath $Relative))
    if (!$target.StartsWith($basePath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Unexpected path $target" }
    if (!(Test-Path -LiteralPath $target)) { return }
    # Windows PowerShell 5.1 can follow directory junctions during recursive removal.
    for ($parent = $target; $parent; $parent = [IO.Path]::GetDirectoryName($parent)) {
        if ((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Review the linked path before removing $target"
        }
    }
    $item = Get-Item -LiteralPath $target -Force
    if ($item.PSIsContainer) {
        $queue = New-Object 'System.Collections.Generic.Stack[string]'
        $queue.Push($target)
        while ($queue.Count) {
            foreach ($child in (Get-ChildItem -LiteralPath $queue.Pop() -Force)) {
                if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Review the linked item $($child.FullName)" }
                if ($child.PSIsContainer) { $queue.Push($child.FullName) }
            }
        }
    }
    Remove-Item -LiteralPath $target -Force -Recurse
}

if ($dedicated) {
    Remove-CheckoutItem $addins 'mcp-servers-for-revit.addin'
    Remove-CheckoutItem $addins 'revit_mcp_plugin'
    if (!$KeepData) { Remove-CheckoutItem $env:LOCALAPPDATA 'Sonne/RevitPublications' }
}
Remove-CheckoutItem $root '.state/revit-mcp-source'
if ($KeepData) { Write-Host 'Windows client build removed. Enrollment kept for reinstall.' }
else {
    Remove-CheckoutItem $root '.state/revit-client/config.json'
    Write-Host 'Windows client enrollment and its local MCP build removed.'
}
