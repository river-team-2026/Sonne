param([switch]$AsJson, [switch]$Scan, [string[]]$SearchRoot)
$ErrorActionPreference = 'Stop'
$clients = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
function Test-Client([string]$Path) {
    if ((Test-Path -LiteralPath (Join-Path $Path '.state/revit-client/config.json') -PathType Leaf -ErrorAction SilentlyContinue) -and
        (Test-Path -LiteralPath (Join-Path $Path 'scripts/install-revit-client.cmd') -PathType Leaf -ErrorAction SilentlyContinue)) {
        [void]$clients.Add([IO.Path]::GetFullPath($Path))
    }
}
if (!$SearchRoot) { Test-Client (Split-Path $PSScriptRoot -Parent) }
if ($SearchRoot -or $Scan -or !$clients.Count) {
    $pending = New-Object 'System.Collections.Generic.Stack[System.IO.DirectoryInfo]'
    if ($SearchRoot) {
        foreach ($path in $SearchRoot) { $pending.Push((Get-Item -LiteralPath $path)) }
    } else {
        foreach ($drive in [IO.DriveInfo]::GetDrives()) {
            if ($drive.IsReady -and $drive.DriveType -eq 'Fixed') { $pending.Push($drive.RootDirectory) }
        }
    }
    $systemPaths = @($env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)})
    while ($pending.Count) {
        $current = $pending.Pop()
        try { $children = $current.GetDirectories() } catch [System.IO.IOException] { continue } catch [System.UnauthorizedAccessException] { continue }
        foreach ($child in $children) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $child.FullName -in $systemPaths -or
                $child.Name -in @('.git','node_modules','$Recycle.Bin','System Volume Information','WinSxS')) { continue }
            if ($child.Name -eq '.state') { Test-Client $current.FullName }
            else { $pending.Push($child) }
        }
    }
}
if ($AsJson) {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    ConvertTo-Json -Compress -InputObject @($clients | Sort-Object)
} else { $clients | Sort-Object }
