<#
.SYNOPSIS
  Register (or remove) the switchboard background loops as Windows Task Scheduler
  tasks. This is the Windows counterpart of system/scripts/install-launchd.sh:
  one task per shipped launchd agent, same command, same cadence.

.DESCRIPTION
  Runtime native (default): every task runs Git for Windows bash.exe, and
  switchboard-env.sh selects wmux and exports CMUX_BIN. The action is

    "<bash.exe>" -c "export MSYSTEM=<UCRT64 or MINGW64> LANG=en_US.UTF-8 PATH=<its bin>:/usr/bin:$HOME/bin:$PATH; source '<root posix>/system/windows/switchboard-env.sh'; cd '<root posix>' && bash <script> <args>"

  Not a login shell: -l reads /etc/profile and sources
  ~/.bashrc, about 58 processes before the task does anything, and a kernel leak on
  some Windows machines keeps every exited process until reboot. The exports above are the parts of
  the login profile the scripts use.

  hosted by `conhost.exe --headless` (no console window) as the interactive
  user; -NoHeadless registers bash.exe directly. Output is appended to the same
  log file the plist names. Missed runs start when the machine wakes
  (StartWhenAvailable), which is the launchd catch-up behavior the plists rely on.

  Runtime wsl: the same tasks run through wsl.exe -d <Distro> -- bash -lc.

  Tasks live in the "\switchboard\" folder:

    com.switchboard.command-center           every 10 min   command-center-loop.sh --once
    com.switchboard.background-tasks         :00 and :30    run-background-tasks.sh
    com.switchboard.cmux-closed-workspaces   at logon, kept alive   daemon/cmux-closed-workspaces.py
    com.switchboard.cos-autostart            once at logon  launch-cos.sh --at-logon (wmux, then CoS with Remote Control)
    com.switchboard.log-rotate               daily 03:30    rotate-logs.sh
    com.switchboard.local-backup             daily 02:30    local_backup.py run (missed runs start at wake)
    com.switchboard.amp-mem-weekly-distill   Sunday 09:00   daemon/amp-mem-weekly-distill.sh
    com.switchboard.claude-mem               at logon, kept alive   system/scripts/claude-mem-worker.sh
                                       (only when the owner chose claude-mem as the memory backend
                                       and claude is on PATH, or when named with -Labels)

  Each task registers on its own: one failure is listed and the rest still
  register. The script ends with "registered N of M tasks" and exits 1 when any
  task failed. Daemon triggers name the current user, so no admin is needed.

  Task Scheduler gives a task the PATH from sign-in, so the script also records
  SWITCHBOARD_PYTHON and SWITCHBOARD_TOOL_PATH in ~/.switchboard/env; switchboard-env.sh turns them into
  PATH entries for every task.

.PARAMETER SwitchboardRoot
  Repo root, wherever the owner installed it (any folder or drive, a synced
  folder such as Dropbox included). Native: a Windows path (default
  $env:SWITCHBOARD_ROOT, then the SWITCHBOARD_ROOT recorded in %USERPROFILE%\.switchboard\env,
  then the repo this script sits in). WSL: a path inside the distro (default
  $env:SWITCHBOARD_WSL_ROOT; otherwise pass it). install.ps1 always passes it.

.PARAMETER Runtime
  native (default; $env:SWITCHBOARD_RUNTIME honored) or wsl.

.PARAMETER Distro
  WSL distribution name (wsl runtime only). Defaults to the WSL default.

.PARAMETER Labels
  Subset of task labels to (un)register. Defaults to the six Amp loops, plus com.switchboard.claude-mem when claude is on PATH and the owner chose claude-mem (~/.claude-mem/settings.json exists, or SWITCHBOARD_MEMORY=claude-mem). Naming a label always registers it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File system\windows\register-scheduled-tasks.ps1
  powershell -ExecutionPolicy Bypass -File system\windows\register-scheduled-tasks.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File system\windows\register-scheduled-tasks.ps1 -Unregister
  powershell -ExecutionPolicy Bypass -File system\windows\register-scheduled-tasks.ps1 -Runtime wsl -SwitchboardRoot /home/me/Development/switchboard
#>
[CmdletBinding()]
param(
  [string]$SwitchboardRoot = "",
  [ValidateSet("native", "wsl")]
  [string]$Runtime = $(if ($env:SWITCHBOARD_RUNTIME -and $env:SWITCHBOARD_RUNTIME.ToLower() -eq "wsl") { "wsl" } else { "native" }),
  [string]$Distro = "",
  [string[]]$Labels = @(),
  [switch]$Unregister,
  [Alias("Uninstall")]
  [switch]$Remove,
  [switch]$NoHeadless,
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$TaskPath = "\switchboard\"

function Say([string]$msg) { Write-Host "[switchboard] $msg" }
function Warn([string]$msg) { Write-Host "[switchboard] warn: $msg" -ForegroundColor Yellow }

if ($Remove) { $Unregister = $true }

if (-not $SwitchboardRoot) {
  if ($Runtime -eq "wsl") {
    if (-not $env:SWITCHBOARD_WSL_ROOT) { throw "pass -SwitchboardRoot <repo path inside the distro> (or set SWITCHBOARD_WSL_ROOT)" }
    $SwitchboardRoot = $env:SWITCHBOARD_WSL_ROOT
  } else {
    $SwitchboardRoot = $env:SWITCHBOARD_ROOT
    if (-not $SwitchboardRoot) {
      $stateDir = if ($env:SWITCHBOARD_STATE_HOME) { $env:SWITCHBOARD_STATE_HOME } else { Join-Path $(if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }) ".switchboard" }
      $envFile = Join-Path $stateDir "env"
      if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile) {
          if ($line -match '^\s*SWITCHBOARD_ROOT\s*=\s*(.+?)\s*$') { $SwitchboardRoot = $Matches[1] }
        }
      }
    }
    if (-not $SwitchboardRoot) { $SwitchboardRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) }
    # Git Bash exports the MSYS form (/c/Dev/switchboard); Task Scheduler needs C:\Dev\switchboard.
    if ($SwitchboardRoot -match '^/([A-Za-z])(/.*)?$') { $SwitchboardRoot = $Matches[1].ToUpper() + ":" + $(if ($Matches[2]) { $Matches[2] -replace '/', '\' } else { "\" }) }
  }
}

# label -> script (relative to the repo root), args, log (relative), schedule
$Jobs = @(
  # 10 min, same as macOS: a tick starts hundreds of processes and each can stay until
  # reboot on Windows.
  @{ Label = "com.switchboard.command-center";         Cmd = "bash system/scripts/command-center-loop.sh --once"; Log = "system/state/logs/command-center.out.log";  Kind = "every";   Minutes = 10 },
  @{ Label = "com.switchboard.background-tasks";       Cmd = "bash system/scripts/run-background-tasks.sh";       Log = "system/logs/background-tasks-stdout.log";   Kind = "every";   Minutes = 30 },
  @{ Label = "com.switchboard.cmux-closed-workspaces"; Cmd = "python3 daemon/cmux-closed-workspaces.py";          Log = "system/logs/cmux-closed-workspaces.log";    Kind = "daemon" },
  @{ Label = "com.switchboard.log-rotate";             Cmd = "bash system/scripts/rotate-logs.sh";                Log = "system/logs/rotate-logs-launchd.log";       Kind = "daily";   At = "03:30" },
  @{ Label = "com.switchboard.local-backup";           Cmd = "python3 system/scripts/local_backup.py run";        Log = "system/logs/local-backup.log";              Kind = "daily";   At = "02:30" },
  @{ Label = "com.switchboard.amp-mem-weekly-distill"; Cmd = "bash daemon/amp-mem-weekly-distill.sh";             Log = ".state/amp-mem-weekly-distill.out.log";     Kind = "weekly";  At = "09:00"; Day = "Sunday" },
  # wmux opens only http(s) links, so note links in CoS reports go through this
  # 127.0.0.1 server (review_surface note_link_mode "http"). `ensure` starts it
  # when it is down and is a no-op otherwise.
  @{ Label = "com.switchboard.note-links";             Cmd = "python3 system/scripts/mux/note-links.py ensure";  Log = "system/state/logs/note-links.out.log";       Kind = "every";   Minutes = 10 },
  # CoS on the phone after a reboot. Once per sign-in: starts
  # wmux, lets it restore (a resumed CoS keeps its Remote Control), and opens a CoS with
  # --remote-control only when no live CoS came back. Kill switch SWITCHBOARD_COS_AUTOSTART=off.
  @{ Label = "com.switchboard.cos-autostart";          Cmd = "bash system/scripts/launch-cos.sh --at-logon";     Log = "system/state/logs/cos-autostart.log";        Kind = "logon" },
  # Claude Code memory worker (claude-mem). Registered by default only when the
  # owner chose claude-mem (RequiresMemory) and Claude Code is installed, or when
  # asked for by label (install-claude-code.sh --memory claude-mem does that).
  @{ Label = "com.switchboard.claude-mem";             Cmd = "bash system/scripts/claude-mem-worker.sh";          Log = "system/state/logs/claude-mem.out.log";      Kind = "daemon"; RequiresCommand = "claude"; RequiresMemory = "claude-mem" }
)

function Test-MemoryBackend([string]$backend) {
  # install-claude-code.sh --memory claude-mem writes ~/.claude-mem/settings.json;
  # the default (builtin) leaves it absent. SWITCHBOARD_MEMORY overrides either way.
  if ($env:SWITCHBOARD_MEMORY) { return ($env:SWITCHBOARD_MEMORY.ToLower() -eq $backend) }
  if ($backend -eq "claude-mem") {
    $userHome = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
    return (Test-Path -LiteralPath (Join-Path $userHome ".claude-mem\settings.json"))
  }
  return $false
}

if ($Labels.Count -gt 0) {
  $Jobs = @($Jobs | Where-Object { $Labels -contains $_.Label })
  if (-not $Jobs) { throw "no task matches: $($Labels -join ', ')" }
} else {
  $Jobs = @($Jobs | Where-Object {
    # ContainsKey, not $_.RequiresCommand: StrictMode in PowerShell 7 throws on a missing key.
    ((-not $_.ContainsKey("RequiresCommand")) -or (Get-Command $_.RequiresCommand -ErrorAction SilentlyContinue)) -and
    ((-not $_.ContainsKey("RequiresMemory")) -or (Test-MemoryBackend $_.RequiresMemory))
  })
}

function Find-GitBash {
  # Git for Windows bash, never System32\bash.exe (the WSL launcher).
  $cands = @(Get-Command bash.exe -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch '\\System32\\' })
  if ($cands.Count -gt 0) { return $cands[0].Source }
  $git = Get-Command git.exe -ErrorAction SilentlyContinue
  if ($git) {
    $gitRoot = Split-Path -Parent (Split-Path -Parent $git.Source)
    foreach ($rel in @("bin\bash.exe", "usr\bin\bash.exe")) {
      $p = Join-Path $gitRoot $rel
      if (Test-Path $p) { return $p }
    }
  }
  foreach ($p in @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe", "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe")) {
    if ($p -and (Test-Path $p)) { return $p }
  }
  return $null
}

function To-PosixPath([string]$win) {
  # C:\Dev\x -> /c/Dev/x (the MSYS form Git Bash understands).
  if ($win -match '^([A-Za-z]):[\\/](.*)$') {
    return "/" + $Matches[1].ToLower() + "/" + ($Matches[2] -replace '\\', '/')
  }
  return ($win -replace '\\', '/')
}

function Native-Action([string]$bashExe, [string]$rootPosix, [string]$cmd, [string]$logPosix) {
  # switchboard-env.sh reads ~/.switchboard/env line by line (a raw `source` of that file
  # would strip the backslashes in Windows paths), exports SWITCHBOARD_PYTHON and runs
  # mux_init, so a task sees the same environment as an interactive Git Bash.
  # An interactive-logon console task would flash a window, so conhost
  # --headless hosts bash.exe.
  # Under conhost --headless the process has no stdin handle, and a child that
  # inherits it fails (Python raises WinError 6), so the command reads /dev/null.
  # A nonzero exit is also written to the log, because conhost may hide it from
  # Task Scheduler's LastTaskResult.
  $logDir = $logPosix.Substring(0, $logPosix.LastIndexOf('/'))
  # Recent Git for Windows ships ucrt64, older ones mingw64; bash.exe sits in <root>\usr\bin or <root>\bin.
  $gitRoot = Split-Path -Parent (Split-Path -Parent $bashExe)
  if ((Split-Path -Leaf $gitRoot) -eq "usr") { $gitRoot = Split-Path -Parent $gitRoot }
  $msys = if (Test-Path -LiteralPath (Join-Path $gitRoot "ucrt64\bin\git.exe")) { "UCRT64" } else { "MINGW64" }
  $msysBin = "/" + $msys.ToLower() + "/bin"
  $inner = "export MSYSTEM=$msys LANG=en_US.UTF-8 PATH=${msysBin}:/usr/bin:`$HOME/bin:`$PATH; source '$rootPosix/system/windows/switchboard-env.sh' 2>/dev/null; [ -d '$logDir' ] || mkdir -p '$logDir'; cd '$rootPosix' && $cmd < /dev/null >> '$logPosix' 2>&1"
  $inner += '; rc=$?; if [ "$rc" -ne 0 ]; then echo "[switchboard task] exit $rc" >> ' + "'$logPosix'" + '; fi; exit "$rc"'
  $escaped = $inner -replace '"', '\"'
  $bashArgs = "`"$bashExe`" -c `"$escaped`""
  if ($NoHeadless) { return @{ Execute = $bashExe; Argument = "-c `"$escaped`"" } }
  return @{ Execute = "conhost.exe"; Argument = "--headless $bashArgs" }
}

function Wsl-Action([string]$cmd, [string]$log) {
  # `bash -lc` so ~/.bashrc (switchboard-env.sh) selects wmux and exports CMUX_BIN.
  $inner = "mkdir -p `$(dirname '$log') && cd '$SwitchboardRoot' && $cmd >> '$log' 2>&1"
  $argv = @()
  if ($Distro) { $argv += @("-d", $Distro) }
  $argv += @("--", "bash", "-lc", $inner)
  $argString = ($argv | ForEach-Object { if ($_ -match '\s') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join " "
  return @{ Execute = "wsl.exe"; Argument = $argString }
}

function Task-Trigger($job) {
  switch ($job.Kind) {
    "every" {
      # Daily at 00:00 with a repetition interval: the Task Scheduler equivalent of
      # launchd StartInterval / StartCalendarInterval(Minute 0,30).
      $t = New-ScheduledTaskTrigger -Daily -At "00:00"
      $t.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Minutes $job.Minutes) -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
      return $t
    }
    "daily"   { return New-ScheduledTaskTrigger -Daily -At $job.At }
    "weekly"  { return New-ScheduledTaskTrigger -Weekly -DaysOfWeek $job.Day -At $job.At }
    # -User: without it the trigger covers every user's logon and Windows asks for admin.
    "daemon"  { return New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) }
    # Once per sign-in, 30 s after it, so the desktop is up before wmux starts.
    "logon"   {
      $t = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
      $t.Delay = "PT30S"
      return $t
    }
    "monthly" {
      # New-ScheduledTaskTrigger has no -Monthly, and Register-ScheduledTask rejects a
      # hand-built MSFT_TaskMonthlyTrigger. Register daily here; the task XML gets the
      # monthly schedule right after (Set-MonthlyTrigger).
      return New-ScheduledTaskTrigger -Daily -At $job.At
    }
  }
}

function Task-Settings($job) {
  if ($job.Kind -eq "daemon") {
    # KeepAlive: restart on exit, no run-time limit, one instance.
    return New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
      -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
      -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
  }
  return New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew -Hidden
}
# "logon" jobs take the default settings above: one run, no restart, 2 h limit.

# -- resolve the launcher once ------------------------------------------------
$bashExe = $null
$rootPosix = $null
if (-not $Unregister) {
  if ($Runtime -eq "native") {
    $bashExe = Find-GitBash
    if (-not $bashExe) {
      if ($DryRun) { Warn "Git for Windows bash.exe not found; showing the command with a placeholder"; $bashExe = "C:\Program Files\Git\bin\bash.exe" }
      else { throw "Git for Windows bash.exe not found; install with: winget install --id Git.Git -e" }
    }
    $rootPosix = To-PosixPath $SwitchboardRoot
    Say "runtime native: $bashExe, repo $SwitchboardRoot ($rootPosix)"
  } else {
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl -and -not $DryRun) { throw "wsl.exe not found; install WSL2 first (wsl --install) or use -Runtime native" }
    Say "runtime wsl: distro '$Distro', repo $SwitchboardRoot"
  }
}

function Find-RealPython([string]$Preferred = "") {
  # sys.executable of a working Python 3.11+ that is not the Microsoft Store stub, or $null.
  # A $Preferred path (the one ~/.switchboard/env already names) wins while it still works.
  $cands = @()
  if ($Preferred -and (Test-Path -LiteralPath $Preferred)) { $cands += , @($Preferred) }
  $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($launcher) { $cands += , @($launcher.Source, "-3") }
  foreach ($name in @("python.exe", "python3.exe")) {
    foreach ($c in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
      if ($c.Source -notmatch '\\Microsoft\\WindowsApps\\') { $cands += , @($c.Source) }
    }
  }
  foreach ($c in $cands) {
    $rest = @()
    if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
    try {
      $out = & $c[0] @rest -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
      if ($LASTEXITCODE -eq 0 -and $out -and "$out" -notmatch '\\Microsoft\\WindowsApps\\') { return "$out".Trim() }
    } catch { }
  }
  return $null
}

function Record-ToolEnv {
  # A scheduled task keeps the PATH from sign-in, so tools installed or put on PATH
  # since then are invisible to it. Record the real Python and the tool folders in
  # ~/.switchboard/env; switchboard-env.sh reads them for every task. Forward slashes, ";" between folders.
  $userProfile = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
  $stateDir = if ($env:SWITCHBOARD_STATE_HOME) { $env:SWITCHBOARD_STATE_HOME } else { Join-Path $userProfile ".switchboard" }
  $envFile = Join-Path $stateDir "env"
  $lines = @()
  if (Test-Path -LiteralPath $envFile) { $lines = @(Get-Content -LiteralPath $envFile) }
  $current = @{}
  foreach ($line in $lines) { if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') { $current[$Matches[1]] = $Matches[2] } }

  $py = Find-RealPython $current["SWITCHBOARD_PYTHON"]
  if (-not $py) { Warn "no working Python 3.11+ found; SWITCHBOARD_PYTHON not recorded (tasks that need python3 will fail)"; return }
  $dirs = @((Split-Path -Parent $py))
  foreach ($exe in @("git.exe", "rg.exe", "bun.exe", "ruby.exe")) {
    $c = Get-Command $exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { $dirs += (Split-Path -Parent $c.Source) }
  }
  $dirs = @($dirs | Where-Object { $_ } | ForEach-Object { $_ -replace '\\', '/' } | Select-Object -Unique)
  $wanted = [ordered]@{ SWITCHBOARD_PYTHON = ($py -replace '\\', '/'); SWITCHBOARD_TOOL_PATH = ($dirs -join ";") }
  $changed = $false
  foreach ($k in $wanted.Keys) {
    if ($current[$k] -ne $wanted[$k]) { Say "$envFile set $k=$($wanted[$k])"; $changed = $true }
  }
  if (-not $changed) { Say "$envFile already has SWITCHBOARD_PYTHON and SWITCHBOARD_TOOL_PATH"; return }
  if ($DryRun) { return }
  $kept = @($lines | Where-Object { $_ -notmatch '^\s*(SWITCHBOARD_PYTHON|SWITCHBOARD_TOOL_PATH)\s*=' })
  if ($kept.Count -eq 0) { $kept = @("# switchboard environment (KEY=VALUE). Written by install.py and install.ps1; edit by hand as needed.") }
  $out = $kept + @("SWITCHBOARD_PYTHON=$($wanted["SWITCHBOARD_PYTHON"])", "SWITCHBOARD_TOOL_PATH=$($wanted["SWITCHBOARD_TOOL_PATH"])")
  if (-not (Test-Path -LiteralPath $stateDir)) { New-Item -ItemType Directory -Path $stateDir | Out-Null }
  [System.IO.File]::WriteAllText($envFile, (($out -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding $false))
}

if (-not $Unregister -and $Runtime -eq "native") {
  try { Record-ToolEnv } catch { Warn "could not record SWITCHBOARD_PYTHON and SWITCHBOARD_TOOL_PATH: $($_.Exception.Message)" }
}

$failed = @()
$registered = 0
foreach ($job in $Jobs) {
  $name = $job.Label
  if ($Unregister) {
    Say "unregister $name"
    if (-not $DryRun) { Unregister-ScheduledTask -TaskName $name -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue }
    continue
  }
  try {
    if ($Runtime -eq "native") {
      $act = Native-Action $bashExe $rootPosix $job.Cmd ("$rootPosix/" + $job.Log)
    } else {
      $act = Wsl-Action $job.Cmd ("$SwitchboardRoot/" + $job.Log)
    }
    Say ("register {0}  ->  `"{1}`" {2}" -f $name, $act.Execute, $act.Argument)
    if ($DryRun) { $registered++; continue }
    $action = New-ScheduledTaskAction -Execute $act.Execute -Argument $act.Argument
    $trigger = Task-Trigger $job
    $settings = Task-Settings $job
    # Interactive user, limited token, runs only while logged on. Hidden window.
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Unregister-ScheduledTask -TaskName $name -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $name -TaskPath $TaskPath -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
      -Description "switchboard: $($job.Cmd) (mirrors system/launchd/$name.plist)" | Out-Null
    if ($job.Kind -eq "monthly") {
      $months = (("January","February","March","April","May","June","July","August","September","October","November","December") | ForEach-Object { "<$_ />" }) -join ""
      $start = (Get-Date -Format "yyyy-MM-dd") + "T" + $job.At + ":00"
      $monthly = "<Triggers><CalendarTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled><ScheduleByMonth><DaysOfMonth><Day>$($job.DayOfMonth)</Day></DaysOfMonth><Months>$months</Months></ScheduleByMonth></CalendarTrigger></Triggers>"
      $xml = (Export-ScheduledTask -TaskName $name -TaskPath $TaskPath) -replace '(?s)<Triggers>.*</Triggers>', $monthly
      Register-ScheduledTask -TaskName $name -TaskPath $TaskPath -Xml $xml -Force | Out-Null
    }
    if ($job.Kind -eq "daemon") { Start-ScheduledTask -TaskName $name -TaskPath $TaskPath }
    $registered++
  } catch {
    $failed += ("{0}: {1}" -f $name, $_.Exception.Message.Trim())
    Warn "could not register ${name}: $($_.Exception.Message.Trim())"
  }
}

if (-not $Unregister) {
  if (-not $DryRun) { Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue | Select-Object TaskName, State | Format-Table -AutoSize }
  Say ("registered {0} of {1} tasks{2}" -f $registered, @($Jobs).Count, $(if ($DryRun) { " (dry run)" } else { "" }))
  if ($failed.Count -gt 0) {
    foreach ($f in $failed) { Warn "failed: $f" }
    exit 1
  }
}
