<#
.SYNOPSIS
  Log which apps hold the laptop's screen or system awake, every 10 minutes, so an
  overnight hold can be named the next morning.

.DESCRIPTION
  `powercfg /requests` names the app behind a display or system hold, but only from an
  admin prompt and only while the hold is live, so a hold that ends before anyone looks
  goes unnamed. This script closes that gap without changing any power
  setting: -Install copies this script to an admin-only folder,
  C:\ProgramData\switchboard\power-log (Administrators and SYSTEM write, Users read), and
  registers a scheduled task that runs that copy as SYSTEM every 10 minutes while the
  laptop is awake, appending one line per run to power-requests.log in the same folder.
  The task never runs the repo copy: the repo sits in a user-writable folder, and
  a SYSTEM task running a user-writable script would let any user program run as SYSTEM.
  It never wakes the laptop and never holds it awake. Rerun -Install after editing this
  script so the installed copy matches.

  Each line: time, then one entry per held request, for example
    2000-01-01 05:10:02 +00:00 DISPLAY=browser.exe (Video Wake Lock) | SYSTEM=pwsh.exe
  and "none" when nothing holds. switchboard's own holder shows as pwsh.exe while workers run.

  Install, status, and removal need an admin PowerShell opened in the switchboard folder:
    cd <your switchboard folder>
    pwsh -NoProfile -File system\windows\log-power-requests.ps1 -Install
    pwsh -NoProfile -File system\windows\log-power-requests.ps1 -Status
    pwsh -NoProfile -File system\windows\log-power-requests.ps1 -Uninstall

  Read the log from any prompt (no admin):
    Get-Content C:\ProgramData\switchboard\power-log\power-requests.log -Tail 50

.PARAMETER ParseFile
  Test hook: parse a saved `powercfg /requests` output instead of running powercfg.
#>
param(
    [switch]$Install,
    [switch]$Uninstall,
    [switch]$Status,
    [switch]$Log,
    [string]$LogPath = '',
    [string]$ParseFile = ''
)
$ErrorActionPreference = 'Stop'
$TaskName = 'switchboard power requests log'
$MaxBytes = 1MB
$InstallDir = Join-Path $env:ProgramData 'switchboard\power-log'

function Test-Reparse([string]$path) {
    (Test-Path $path) -and [bool]((Get-Item -Force $path).Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Format-Requests([string[]]$lines) {
    # powercfg prints SECTION: headers, then "None." or entries: "[PROCESS] \Device\...\app.exe"
    # followed by an optional reason line.
    $entries = @()
    $section = ''
    $last = $null
    foreach ($raw in $lines) {
        $line = $raw.Trim()
        if (-not $line) { $last = $null; continue }
        if ($line -match '^[A-Z]+:$') { $section = $line.TrimEnd(':'); $last = $null; continue }
        if ($line -eq 'None.') { continue }
        if ($line -match '^\[(?<kind>[A-Z]+)\]\s*(?<what>.*)$') {
            $what = $Matches.what
            $name = if ($Matches.kind -eq 'PROCESS' -and $what) { Split-Path -Leaf $what } else { "$($Matches.kind) $what".Trim() }
            $last = [pscustomobject]@{ section = $section; name = $name; reason = '' }
            $entries += $last
            continue
        }
        if ($last -and -not $last.reason) { $last.reason = $line }
    }
    if (-not $entries) { return 'none' }
    ($entries | ForEach-Object { if ($_.reason) { "$($_.section)=$($_.name) ($($_.reason))" } else { "$($_.section)=$($_.name)" } }) -join ' | '
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

if ($ParseFile) {
    Format-Requests (Get-Content -Path $ParseFile)
    exit 0
}

if ($Log) {
    if (-not $LogPath) { $LogPath = Join-Path $InstallDir 'power-requests.log' }
    New-Item -ItemType Directory -Force (Split-Path -Parent $LogPath) | Out-Null
    # Never follow a link someone planted in place of the log, its folder, or its rotated copy.
    foreach ($p in @((Split-Path -Parent $LogPath), $LogPath, "$LogPath.1")) {
        if (Test-Reparse $p) { throw "refusing to write through a link: $p" }
    }
    if ((Test-Path $LogPath) -and (Get-Item $LogPath).Length -gt $MaxBytes) {
        Move-Item -Force $LogPath "$LogPath.1"
    }
    # Windows PowerShell 5.1 turns a native command's stderr into a terminating error under
    # 'Stop', so run powercfg under 'Continue' and log any failure as a line.
    $ErrorActionPreference = 'Continue'
    try {
        $out = & powercfg /requests 2>&1
        $summary = if ($LASTEXITCODE -ne 0) { "error: powercfg /requests exit $LASTEXITCODE (needs admin)" } else { Format-Requests ($out | ForEach-Object { "$_" }) }
    } catch {
        $summary = "error: $($_.Exception.Message)"
    }
    Add-Content -Path $LogPath -Value ("{0} {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'), $summary)
    exit 0
}

if ($Status) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = $task | Get-ScheduledTaskInfo
        "installed: state $($task.State), last run $($info.LastRunTime), result $($info.LastTaskResult)"
    } else { 'not installed' }
    exit 0
}

if ($Install -or $Uninstall) {
    if (-not (Test-Admin)) { throw 'Run this from an admin PowerShell (right-click Terminal, Run as administrator).' }
}

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        'removed'
    } else { 'not installed' }
    exit 0
}

if ($Install) {
    # Admin-only copy: Administrators and SYSTEM may write, Users may only read.
    New-Item -ItemType Directory -Force $InstallDir | Out-Null
    if (Test-Reparse $InstallDir) { throw "refusing to install into a link: $InstallDir" }
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $none = [System.Security.AccessControl.PropagationFlags]::None
    foreach ($rule in @(@('S-1-5-32-544', 'FullControl'), @('S-1-5-18', 'FullControl'), @('S-1-5-32-545', 'ReadAndExecute'))) {
        $sid = New-Object System.Security.Principal.SecurityIdentifier($rule[0])
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $rule[1], $inherit, $none, 'Allow')))
    }
    $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')))
    Set-Acl -Path $InstallDir -AclObject $acl
    $script = Join-Path $InstallDir 'log-power-requests.ps1'
    Copy-Item -Force -Path $PSCommandPath -Destination $script
    $logFile = Join-Path $InstallDir 'power-requests.log'
    # The system's own PowerShell: a user-installed pwsh can sit in a user-writable folder.
    $exe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $action = New-ScheduledTaskAction -Execute $exe -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Log -LogPath `"$logFile`""
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    $before = if (Test-Path $logFile) { (Get-Content $logFile).Count } else { 0 }
    Start-ScheduledTask -TaskName $TaskName
    "installed: every 10 minutes while awake, script $script, log $logFile"
    # Prove the first run: wait up to 60 seconds for its line.
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        if ((Test-Path $logFile) -and (Get-Content $logFile).Count -gt $before) { break }
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    "task: last run $($info.LastRunTime), result $($info.LastTaskResult), next run $($info.NextRunTime)"
    if ((Test-Path $logFile) -and (Get-Content $logFile).Count -gt $before) { "first line: $(Get-Content $logFile -Tail 1)" } else { 'first line: none yet' }
    exit 0
}

'Usage: -Install | -Status | -Uninstall (admin), -Log (run by the task). See the header.'
