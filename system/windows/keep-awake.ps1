<#
.SYNOPSIS
  Keep a Windows machine awake while PM OS workers run, with no power-setting change.

.DESCRIPTION
  Machines with Modern Standby (S0 low power idle) start standby when the
  display turns off, so ES_SYSTEM_REQUIRED alone does not hold them: standby
  begins with the screen off and Windows later moves to its sleep phase even
  while a SYSTEM_REQUIRED loop runs (Kernel-Power events 506 and 507, on AC
  with the lid open). This script holds
  ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED, which keeps the display
  timer from expiring, so standby never starts. The screen stays on until the hold
  ends. It clears the request on exit; nothing in the power plan changes.

  One holder at a time: a new start stops the previous holder. State is in
  .state/keep-awake.json under the repo, and the log is .state/keep-awake.log.

  Dispatch drives it (system/scripts/keep_awake.py), so the machine stays awake
  while workers run: -Ensure starts a hold, or moves a live hold's end later in
  place with no gap and no second process; -Release ends the hold only when the
  given -Owner started it, so a hold the owner started by hand is left alone. The
  holder rereads its end time from the state file every minute.

.EXAMPLE
  pwsh -NoProfile -File system/windows/keep-awake.ps1 -Hours 10
  pwsh -NoProfile -File system/windows/keep-awake.ps1 -Status
  pwsh -NoProfile -File system/windows/keep-awake.ps1 -Stop
  pwsh -NoProfile -File system/windows/keep-awake.ps1 -Ensure -Hours 1 -Owner dispatch -Json
  pwsh -NoProfile -File system/windows/keep-awake.ps1 -Release -Owner dispatch -Json
  pwsh -NoProfile -File system/windows/keep-awake.ps1 -Status -Json
#>
param(
    [double]$Hours = 10,
    [switch]$Stop,
    [switch]$Status,
    # Start a hold, or move a live hold's end to at least now + Hours, in place.
    [switch]$Ensure,
    # End the hold only when -Owner started it.
    [switch]$Release,
    [string]$Owner = 'manual',
    [switch]$Json,
    # Internal: the hidden child that holds the request.
    [switch]$Hold,
    [string]$Until = ''
)
$ErrorActionPreference = 'Stop'

$root = (& git -C (Get-Location) rev-parse --show-toplevel 2>$null)
if (-not $root) { $root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
# A task worktree's .state is a link to main's, so state is shared either way.
# PM_OS_KEEP_AWAKE_STATE_DIR points a test at its own folder.
$stateDir = if ($env:PM_OS_KEEP_AWAKE_STATE_DIR) { $env:PM_OS_KEEP_AWAKE_STATE_DIR } else { Join-Path $root '.state' }
New-Item -ItemType Directory -Force $stateDir | Out-Null
$statePath = Join-Path $stateDir 'keep-awake.json'
$logPath = Join-Path $stateDir 'keep-awake.log'

function Write-Log([string]$text) {
    Add-Content -Path $logPath -Value ("{0} {1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'), $text)
}

function Read-State {
    # -DateKind String: PowerShell 7 would turn the ISO strings into local dates.
    Get-Content $statePath -Raw | ConvertFrom-Json -DateKind String
}

function Get-Holder {
    if (-not (Test-Path $statePath)) { return $null }
    try { $s = Read-State } catch { return $null }
    $p = Get-Process -Id $s.pid -ErrorAction SilentlyContinue
    # The start time proves the pid was not reused by another process.
    if ($p -and [math]::Abs(($p.StartTime - [datetime]::Parse($s.process_start)).TotalSeconds) -lt 2) { return $s }
    return $null
}

function Get-OwnerOf($s) { if ($s.owner) { [string]$s.owner } else { 'manual' } }

function Write-State($s) {
    # Atomic replace: a holder reading mid-write never sees a half-written file.
    # The holder may have the file open for a moment, so the move retries.
    $tmp = "$statePath.$PID.tmp"
    $s | ConvertTo-Json | Set-Content -Path $tmp
    for ($i = 1; ; $i++) {
        try { Move-Item -Force -Path $tmp -Destination $statePath; return }
        catch {
            if ($i -ge 5) { Remove-Item $tmp -ErrorAction SilentlyContinue; throw }
            Start-Sleep -Milliseconds 200
        }
    }
}

function Lock-State {
    # One writer at a time across dispatch, a hand run, and a second dispatch run,
    # so two racing starts never both find no holder.
    $lockPath = Join-Path $stateDir 'keep-awake.ps1.lock'
    $deadline = (Get-Date).AddSeconds(30)
    while ($true) {
        try { return [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch {
            if ((Get-Date) -ge $deadline) { throw "keep-awake: state lock busy for 30 s ($lockPath)" }
            Start-Sleep -Milliseconds 200
        }
    }
}

function Start-Holder([datetime]$holdEnd, [string]$who) {
    $childArgs = @('-NoProfile', '-WindowStyle', 'Hidden', '-File', "`"$PSCommandPath`"", '-Hold', '-Until', $holdEnd.ToString('o'))
    $p = Start-Process pwsh -ArgumentList $childArgs -WindowStyle Hidden -PassThru -WorkingDirectory $root
    Start-Sleep -Milliseconds 500
    $p.Refresh()
    Write-State @{ pid = $p.Id; process_start = $p.StartTime.ToString('o'); started = (Get-Date).ToString('o'); until = $holdEnd.ToString('o'); owner = $who; flags = 'ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_DISPLAY_REQUIRED' }
    Write-Log "holding pid $($p.Id) until $($holdEnd.ToString('o')) (owner $who)"
    return $p.Id
}

function Stop-Holder([string]$why) {
    $s = Get-Holder
    if ($s) {
        Stop-Process -Id $s.pid -Force -ErrorAction SilentlyContinue
        Write-Log "stopped holder pid $($s.pid) ($why)"
    }
    Remove-Item $statePath -ErrorAction SilentlyContinue
    return [bool]$s
}

function Out-Result($obj, [string]$text) {
    if ($Json) { $obj | ConvertTo-Json -Compress } else { $text }
}

if ($Hold) {
    Add-Type -Namespace PmOs -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
    $flags = 0x80000003u  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    $end = [datetime]::Parse($Until)
    try {
        while ((Get-Date) -lt $end) {
            if ([PmOs.Power]::SetThreadExecutionState($flags) -eq 0) { Write-Log "SetThreadExecutionState failed (pid $PID)" }
            $left = ($end - (Get-Date)).TotalSeconds
            Start-Sleep -Seconds ([int][math]::Ceiling([math]::Max(1, [math]::Min(60, $left))))
            # One holder: exit once the state file is gone or names another holder,
            # so two racing starts never leave a hold that -Stop cannot see.
            if (-not (Test-Path $statePath)) { Write-Log "holder pid $PID has no state file; exiting"; break }
            # An unreadable file is a writer mid-replace, not a new holder: keep holding.
            try { $cur = Read-State } catch { continue }
            if ($cur.pid -ne $PID) { Write-Log "holder pid $PID is no longer the recorded holder; exiting"; break }
            # -Ensure moves the end later in place.
            try { $end = [datetime]::Parse($cur.until) } catch { }
        }
    } finally {
        [void][PmOs.Power]::SetThreadExecutionState(0x80000000u)
        Write-Log "hold ended (pid $PID)"
    }
    exit 0
}

if ($Status) {
    $s = Get-Holder
    if ($s) {
        Out-Result @{ holding = $true; pid = $s.pid; until = $s.until; owner = (Get-OwnerOf $s) } "holding: pid $($s.pid), until $($s.until), owner $(Get-OwnerOf $s), display on"
    } else {
        Out-Result @{ holding = $false } 'not holding'
    }
    exit 0
}

if (-not ($Stop -or $Release) -and ($Hours -le 0 -or $Hours -gt 24)) { throw '-Hours must be more than 0 and at most 24' }
$lock = Lock-State
try {

if ($Stop) {
    if (Stop-Holder 'requested') { 'stopped' } else { 'not holding' }
    exit 0
}

if ($Release) {
    $s = Get-Holder
    $manualEnd = $null
    if ($s -and $s.manual_until) { try { $manualEnd = [datetime]::Parse($s.manual_until) } catch { } }
    if ($s -and (Get-OwnerOf $s) -eq $Owner -and $manualEnd -and $manualEnd -gt (Get-Date)) {
        # This owner took over a hand-started hold: hand it back with its own end time.
        Write-State @{ pid = $s.pid; process_start = $s.process_start; started = $s.started; until = $s.manual_until; owner = 'manual'; flags = $s.flags }
        Write-Log "pid $($s.pid) returned to manual until $($s.manual_until) (released by $Owner)"
        $result = 'returned to manual'
    } elseif ($s -and (Get-OwnerOf $s) -eq $Owner) {
        [void](Stop-Holder "released by $Owner")
        $result = 'released'
    } elseif ($s) {
        $result = "left: held by $(Get-OwnerOf $s)"
    } else {
        Remove-Item $statePath -ErrorAction SilentlyContinue
        $result = 'not holding'
    }
    Out-Result @{ action = $result } $result
    exit 0
}

$holdEnd = (Get-Date).AddHours($Hours)

if ($Ensure) {
    $s = Get-Holder
    if ($s) {
        $curOwner = Get-OwnerOf $s
        if ([datetime]::Parse($s.until) -ge $holdEnd) {
            # A longer hold (a manual -Hours 10) already covers this window: leave it and its owner.
            $result = @{ action = 'kept'; pid = $s.pid; until = $s.until; owner = $curOwner }
        } else {
            # Move the end later in place. Taking over a shorter hand-started hold keeps its
            # end as manual_until, so -Release hands it back instead of ending it early.
            $new = @{ pid = $s.pid; process_start = $s.process_start; started = $s.started; until = $holdEnd.ToString('o'); owner = $Owner; flags = $s.flags }
            if ($curOwner -ne $Owner) { $new.manual_until = $s.until } elseif ($s.manual_until) { $new.manual_until = $s.manual_until }
            Write-State $new
            if ($curOwner -ne $Owner) { Write-Log "pid $($s.pid) taken over by $Owner from $curOwner (its end $($s.until) kept)" }
            $result = @{ action = 'extended'; pid = $s.pid; until = $holdEnd.ToString('o'); owner = $Owner }
        }
    } else {
        Remove-Item $statePath -ErrorAction SilentlyContinue
        $newPid = Start-Holder $holdEnd $Owner
        $result = @{ action = 'started'; pid = $newPid; until = $holdEnd.ToString('o'); owner = $Owner }
    }
    Out-Result $result "keep-awake $($result.action): pid $($result.pid), display on, until $($result.until)"
    exit 0
}

[void](Stop-Holder 'replaced by a new hold')
$newPid = Start-Holder $holdEnd $Owner
"keep-awake pid $newPid, display on, until $($holdEnd.ToString('ddd h:mm tt'))"

} finally {
    $lock.Dispose()
}
