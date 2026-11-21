# Windows entry point. Probe WSL read-only, then uninstall as the discovered owner.
$ErrorActionPreference = 'Stop'
$mode = @($args)
foreach ($arg in $mode) {
    if ($arg -notin @('--list','--keep-data','--purge','--yes')) { throw "Unknown option: $arg" }
}
if ('--purge' -in $mode -and '--keep-data' -in $mode) { throw 'Choose one uninstall mode.' }
$root = Split-Path $PSScriptRoot -Parent
$env:WSL_UTF8 = '1'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$installs = @()
$failures = @()
if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {
    $names = @($null | wsl.exe --list --quiet)
    if ($LASTEXITCODE -eq 0) {
        foreach ($line in $names) {
            $distro = ($line -replace [char]0,'').Trim()
            if (!$distro -or $distro -in @('docker-desktop','docker-desktop-data')) { continue }
            # Supported Sonne distributions have python3. Do not run the uninstall as root.
            $null | wsl.exe -d $distro -u root --exec sh -c 'command -v python3' | Out-Null
            if ($LASTEXITCODE -ne 0) { continue }
            $json = $null | wsl.exe -d $distro -u root --cd $root --exec python3 scripts/uninstall-sonne.py --discover-json
            if ($LASTEXITCODE -ne 0) { $failures += $distro; continue }
            foreach ($item in ($json | ConvertFrom-Json)) {
                $installs += [pscustomobject]@{Distro=$distro; User=$item.user; Path=$item.path}
            }
        }
    }
}
if ($failures.Count) { Write-Warning "Could not inspect: $($failures -join ', ')." }
function Select-Installation($choices) {
    if ($choices.Count -eq 1) { return $choices[0] }
    $index = 0
    if (![int]::TryParse((Read-Host 'Choose an installation number'), [ref]$index) -or
        $index -lt 1 -or $index -gt $choices.Count) { throw 'Invalid selection.' }
    return $choices[$index-1]
}
if ($installs.Count) {
    for ($i=0; $i -lt $installs.Count; $i++) {
        $item = $installs[$i]
        Write-Host "$($i+1). $($item.Distro) / $($item.User) / $($item.Path)"
    }
    if ('--list' -in $mode) { return }
    $item = Select-Installation $installs
    wsl.exe -d $item.Distro -u $item.User --cd $root --exec python3 scripts/uninstall-sonne.py --dir $item.Path @mode
    exit $LASTEXITCODE
}

# A Revit workstation can have a Windows client without a local Linux server.
$clients = @(& (Join-Path $PSScriptRoot 'find-revit-clients.ps1'))
if (!$clients.Count) {
    Write-Host 'No Sonne installation found.'
    if ($failures.Count) { exit 1 }
    return
}
for ($i=0; $i -lt $clients.Count; $i++) { Write-Host "$($i+1). Windows Revit client / $($clients[$i])" }
if ('--list' -in $mode) { return }
$client = Select-Installation $clients
$keep = '--keep-data' -in $mode
if (!$keep -and '--purge' -notin $mode) {
    $choice = Read-Host '1. Keep enrollment [default]   2. Erase enrollment. Choose [1/2]'
    if ($choice -notin @('','1','2')) { throw 'Invalid choice.' }
    $keep = $choice -ne '2'
}
if (!$keep -and '--yes' -notin $mode -and (Read-Host 'Type DELETE to erase the local client data') -cne 'DELETE') { return }
& (Join-Path $PSScriptRoot 'uninstall-revit-client.ps1') -Checkout $client -KeepData:$keep
