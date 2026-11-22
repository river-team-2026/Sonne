param([Parameter(Mandatory=$true)][string]$Name, [string]$Distro, [string]$LinuxUser,
      [string]$Runtime, [switch]$Remove)
$ErrorActionPreference = 'Stop'
if ($Name -notmatch '^Sonne-ACC-[0-9a-f]{16}$') { throw 'Invalid Sonne task name.' }
if ($Remove) {
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
    $log = Join-Path $env:LOCALAPPDATA "$Name.log"
    if (Test-Path -LiteralPath $log) { Remove-Item -LiteralPath $log -Force }
    return
}
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
foreach ($value in @($Distro,$LinuxUser,$Runtime)) {
    if (!$value -or $value -match '["\r\n]') { throw 'Unsupported WSL launch argument.' }
}
$script = "$Runtime/.state/acc-startup/setup-revit-acc.py"
$arguments = @('-d',$Distro,'-u',$LinuxUser,'--cd','/','--exec','python3',$script,'--runtime',$Runtime,'--resume-folder')
$literals = ($arguments | ForEach-Object { "'" + $_.Replace("'","''") + "'" }) -join ','
$launch = @"
`$env:WSL_UTF8 = '1'
`$arguments = @($literals)
& "`$env:SystemRoot\System32\wsl.exe" @arguments 1> `$null 2> "`$env:LOCALAPPDATA\$Name.log"
exit `$LASTEXITCODE
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($launch))
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand $encoded"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings -User $account -RunLevel Limited -Force | Out-Null
