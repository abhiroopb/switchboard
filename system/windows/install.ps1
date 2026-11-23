<#
.SYNOPSIS
  Install the switchboard Kit Windows layer: wmux as the cmux backend, Git for
  Windows bash plus Windows Python as the toolchain, Task Scheduler for the
  background loops, and the Obsidian review-pointer shim.

.DESCRIPTION
  Run after `py install.py --apply` has rendered the kit. Native Windows is the
  default runtime for every scope: the scripts run under Git Bash with the
  Windows Python and talk to wmux directly. -Runtime wsl (or SWITCHBOARD_RUNTIME=wsl)
  keeps the older WSL2 path, where the toolchain lives inside a distro.

  Steps, each idempotent:
    0. Toolchain (native): reload PATH from the registry, add the folders
       winget installs but does not put on PATH (Git\cmd, ripgrep, Bun), put
       the real Python ahead of the Microsoft Store alias and give it a
       python3.exe, and check Ruby and Bun. -PrereqsOnly runs this step alone,
       before install.py: powershell -ExecutionPolicy Bypass -File
       <kit>\system\windows\install.ps1 -PrereqsOnly
    1. Prerequisites. Native: git.exe, Git for Windows bash.exe, py or python;
       wmux is checked and only warned about. WSL: wsl.exe plus a distro.
    2. User environment: SWITCHBOARD_ROOT, SWITCHBOARD_MUX=wmux, SWITCHBOARD_RUNTIME,
       SWITCHBOARD_WORKTREES_DIR, SWITCHBOARD_STATE_HOME, AMP_EDITOR, SWITCHBOARD_BOARD_AUTOSTART=0;
       PATH gains system\windows (cmux.cmd) and, unless an existing CLI with the
       same name as the shim is on PATH (the shim itself does not count),
       system\connectors (the switchboard shim).
    3. %USERPROFILE%\.switchboard\env: created when missing, merged otherwise (keys
       install.py or the owner already set are never overwritten). It also
       records SWITCHBOARD_PYTHON and SWITCHBOARD_TOOL_PATH (python, git, rg, bun, ruby
       folders) for scheduled tasks, which keep the PATH from sign-in, and
       WMUX_BIN when the wmux CLI is found in its install folder.
    4. Shell hook: one `source .../system/windows/switchboard-env.sh` line in the
       Git Bash ~/.bashrc (or the distro's ~/.bashrc for wsl).
    5. The background loops in Task Scheduler (register-scheduled-tasks.ps1): six Amp loops, plus the claude-mem worker when claude-mem is the memory backend.
    6. wmux sidebar seed (seed-session.ps1) when a session exists.

.PARAMETER SwitchboardRoot
  Windows path of the repo: any folder or drive, a synced folder such as
  Dropbox included. Omitted: asked on a terminal with the same question and
  suggestion as install.py. The suggestion is $env:SWITCHBOARD_ROOT, else the
  SWITCHBOARD_ROOT recorded in %USERPROFILE%\.switchboard\env by install.py, else the
  installed repo this script sits in, else $HOME\Development\switchboard. A
  non-interactive run takes the suggestion and says so. Every later step (the
  env file, the user environment, the Git Bash hook, the scheduled tasks, the
  printed next steps) uses the chosen folder.
.PARAMETER Runtime
  native (default; $env:SWITCHBOARD_RUNTIME honored) or wsl.
.PARAMETER Distro
  WSL distribution (wsl runtime only; default: the WSL default distro).
.PARAMETER WslRoot
  The repo as seen from WSL (wsl runtime only; default: derived via wslpath).

.PARAMETER PrereqsOnly
  Run step 0 (toolchain and PATH) and stop. No install folder is asked.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File system\windows\install.ps1 -PrereqsOnly
  powershell -ExecutionPolicy Bypass -File system\windows\install.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File system\windows\install.ps1
  powershell -ExecutionPolicy Bypass -File system\windows\install.ps1 -Runtime wsl -Distro Ubuntu
#>
[CmdletBinding()]
param(
  [string]$SwitchboardRoot = "",
  [ValidateSet("native", "wsl")]
  [string]$Runtime = $(if ($env:SWITCHBOARD_RUNTIME -and $env:SWITCHBOARD_RUNTIME.ToLower() -eq "wsl") { "wsl" } else { "native" }),
  [string]$Distro = "",
  [string]$WslRoot = "",
  [switch]$SkipTasks,
  [switch]$SkipSeed,
  [switch]$PrereqsOnly,
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$userProfile = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }

# -- output helpers -----------------------------------------------------------
function Step([string]$msg) { Write-Host "[switchboard] ==> $msg" -ForegroundColor Cyan }
function Say([string]$msg)  { Write-Host "[switchboard]     $msg" }
function Ok([string]$msg)   { Write-Host "[switchboard]     ok: $msg" -ForegroundColor Green }
function Warn([string]$msg) { Write-Host "[switchboard]     warn: $msg" -ForegroundColor Yellow }

# -- small helpers ------------------------------------------------------------
function To-PosixPath([string]$win) {
  # C:\Dev\x -> /c/Dev/x (the MSYS form Git Bash understands).
  if ($win -match '^([A-Za-z]):[\\/](.*)$') {
    return "/" + $Matches[1].ToLower() + "/" + ($Matches[2] -replace '\\', '/')
  }
  return ($win -replace '\\', '/')
}

function To-WinPath([string]$p) {
  # /c/Dev/x (MSYS form, as Git Bash exports it) -> C:\Dev\x; anything else unchanged.
  if ($p -match '^/([A-Za-z])(/.*)?$') {
    $rest = if ($Matches[2]) { $Matches[2] -replace '/', '\' } else { "\" }
    return $Matches[1].ToUpper() + ":" + $rest
  }
  return $p
}

function To-WslPath([string]$win) {
  # Fallback when wslpath is unavailable: C:\x -> /mnt/c/x, \\wsl$\<distro>\x -> /x.
  if ($win -match '^([A-Za-z]):[\\/](.*)$') {
    return "/mnt/" + $Matches[1].ToLower() + "/" + ($Matches[2] -replace '\\', '/')
  }
  if ($win -match '^\\\\wsl(?:\$|\.localhost)\\[^\\]+\\(.*)$') {
    return "/" + ($Matches[1] -replace '\\', '/')
  }
  return $null
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

function Find-Wmux {
  # The wmux CLI, or $null. Same search order as system/scripts/lib/mux.py
  # (wmux_install_dirs, wmux_cli): WMUX_BIN, then the known install folders
  # (WMUX_HOME, %LOCALAPPDATA%\Programs\wmux, scoop, %ProgramFiles%\wmux), then PATH.
  # wmux puts resources\cli-bin on PATH only in terminals it started, so a plain
  # PowerShell window needs the folder search. A .cmd shim comes before the
  # extensionless one because PowerShell can run only the .cmd.
  if ($env:WMUX_BIN -and (Test-Path -LiteralPath $env:WMUX_BIN)) { return $env:WMUX_BIN }
  $scoop = if ($env:SCOOP) { $env:SCOOP } else { Join-Path $userProfile "scoop" }
  $roots = @()
  if ($env:WMUX_HOME) { $roots += $env:WMUX_HOME }
  if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA "Programs\wmux") }
  $roots += (Join-Path $scoop "apps\wmux\current")
  foreach ($base in @($env:ProgramFiles, $env:ProgramW6432)) { if ($base) { $roots += (Join-Path $base "wmux") } }
  foreach ($root in $roots) {
    foreach ($name in @("wmux.cmd", "wmux")) {
      $cand = Join-Path $root "resources\cli-bin\$name"
      if (Test-Path -LiteralPath $cand) { return $cand }
    }
  }
  foreach ($name in @("wmux.cmd", "wmux", "wmux.exe")) {
    $c = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
  }
  foreach ($name in @("wmux.cmd", "wmux.exe", "wmux")) {
    $cand = Join-Path $scoop "shims\$name"
    if (Test-Path -LiteralPath $cand) { return $cand }
  }
  return $null
}

function Get-WmuxState([string]$cli) {
  # "running" when `wmux ping` prints pong (exit 0), else "stopped" (wmux prints
  # "wmux is not running (could not connect to pipe)" and exits 1), or "unknown"
  # when PowerShell cannot run this file (the extensionless shim).
  if ($cli -notmatch '\.(cmd|exe|bat)$') { return "unknown" }
  $ErrorActionPreference = 'Continue'
  try {
    $out = (& $cli ping 2>$null) -join " "
    if ($LASTEXITCODE -eq 0 -and $out -match 'pong') { return "running" }
    return "stopped"
  } catch { return "stopped" }
}

function Report-Wmux([string]$cli) {
  if (-not $cli) {
    Warn "wmux not found (WMUX_HOME, %LOCALAPPDATA%\Programs\wmux, scoop, %ProgramFiles%\wmux, PATH). Install: scoop bucket add wmux https://github.com/amirlehmam/wmux; scoop install wmux/wmux (or the zip from https://wmux.org). Target version 2.49.0."
    return
  }
  switch (Get-WmuxState $cli) {
    "running" { Ok "wmux running (CLI: $cli)" }
    "stopped" { Ok "wmux installed, not running (CLI: $cli); start wmux before the sidebar seed" }
    default   { Ok "wmux installed (CLI: $cli); running state not checked" }
  }
}

function Get-SqKind {
  # "real", "shim" or "absent". One rule with system/connectors/company_cli.py: the
  # first switchboard on PATH counts as another program ("real") only when `switchboard --version`
  # does not say switchboard-shim and it is not under system\connectors. Uses company_cli.py when Python and the file exist.
  param([string]$Root, [string]$Python)
  $ErrorActionPreference = 'Continue'
  $script = if ($Root) { Join-Path $Root "system\connectors\company_cli.py" } else { "" }
  if ($Python -and $script -and (Test-Path -LiteralPath $script)) {
    $out = (& $Python -I $script 2>$null) -join " "
    if ($out -match '^(real|shim|absent)\b') { return $Matches[1] }
  }
  $switchboard = Get-Command switchboard -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $switchboard) { return "absent" }
  if ($switchboard.Source -match '[\\/]system[\\/]connectors[\\/]') { return "shim" }
  try {
    $v = (& $switchboard.Source --version 2>&1) -join " "
    if ($v -match 'switchboard-shim') { return "shim" }
  } catch { return "absent" }
  return "real"
}

function Set-UserEnv([string]$name, [string]$value) {
  $current = [Environment]::GetEnvironmentVariable($name, "User")
  if ($current -eq $value) { Ok "$name already $value"; return }
  Say "$name = $value"
  if (-not $DryRun) { [Environment]::SetEnvironmentVariable($name, $value, "User") }
}

function Add-UserPath([string]$dir) {
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not $userPath) { $userPath = "" }
  $parts = @($userPath -split ';' | Where-Object { $_ })
  if ($parts -contains $dir) { Ok "PATH already has $dir"; return }
  Say "PATH += $dir"
  if (-not $DryRun) { [Environment]::SetEnvironmentVariable("Path", (($parts + $dir) -join ";"), "User") }
}

function Read-EnvFile([string]$path) {
  # Ordered so a rewrite keeps the owner's line order; comments are dropped.
  $map = [ordered]@{}
  if (-not (Test-Path -LiteralPath $path)) { return $map }
  foreach ($line in Get-Content -LiteralPath $path) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
    $idx = $t.IndexOf("=")
    $k = $t.Substring(0, $idx).Trim()
    $v = $t.Substring($idx + 1).Trim()
    if ($k) { $map[$k] = $v }
  }
  return $map
}

function Merge-EnvFile([string]$path, [System.Collections.IDictionary]$wanted, [string[]]$replace = @()) {
  # Add missing keys and never change a value install.py or the owner wrote,
  # except the keys in $replace (the install folder chosen in this run).
  $existing = Read-EnvFile $path
  $added = @()
  foreach ($k in $wanted.Keys) {
    if (-not $existing.Contains($k)) { $existing[$k] = $wanted[$k]; $added += $k }
    elseif (($replace -contains $k) -and $existing[$k] -ne $wanted[$k]) { $existing[$k] = $wanted[$k]; $added += $k }
  }
  if ($added.Count -eq 0) { Ok "$path already has every key"; return }
  foreach ($k in $added) { Say "$path set $k=$($existing[$k])" }
  if ($DryRun) { return }
  $dir = Split-Path -Parent $path
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
  $lines = @("# switchboard environment (KEY=VALUE). Written by install.py and install.ps1; edit by hand as needed.")
  foreach ($k in $existing.Keys) { $lines += "$k=$($existing[$k])" }
  $enc = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($path, (($lines -join "`n") + "`n"), $enc)
}

function Add-BashrcLine([string]$bashrc, [string]$line, [string]$marker) {
  if ((Test-Path $bashrc) -and (Select-String -Path $bashrc -SimpleMatch $marker -Quiet)) {
    Ok "$bashrc already sources switchboard-env.sh"; return
  }
  Say "append to ${bashrc}: $line"
  if ($DryRun) { return }
  $dir = Split-Path -Parent $bashrc
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
  $enc = New-Object System.Text.UTF8Encoding $false
  $prefix = ""
  if (Test-Path $bashrc) {
    $cur = [System.IO.File]::ReadAllText($bashrc)
    if ($cur.Length -gt 0 -and -not $cur.EndsWith("`n")) { $prefix = "`n" }
  }
  [System.IO.File]::AppendAllText($bashrc, "$prefix`n# switchboard Kit (wmux backend)`n$line`n", $enc)
}

function Invoke-Child([string]$script, [string[]]$argv) {
  & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $here $script) @argv
  if ($LASTEXITCODE -ne 0) { Warn "$script exited $LASTEXITCODE" }
}

# -- owner choice 1: the install folder (docs/install-decisions.md) -----------
# Same question, same suggestion order, and the same synced-folder caveat as
# install.py, so both installers agree on where switchboard lives.
$DestinationPrompt = @"
Where should switchboard live? This folder holds the repo, your notes, people profiles and
workstreams. Suggested: {0}. Type another path to choose a different
folder or drive, or press Enter to accept. A synced folder such as Dropbox works; run switchboard
from one device at a time there, because git locks and the background jobs clash across devices.
"@
# Path segments that mark a sync client's folder (install.py SYNCED_SEGMENTS).
$SyncedSegments = @("dropbox", "onedrive", "icloud", "com~apple~clouddocs", "mobile documents",
  "google drive", "googledrive", "cloudstorage", "box sync", "box-box")

function Test-Interactive {
  # A console with a person at it: no redirected stdin, no -NonInteractive host.
  if ($env:SWITCHBOARD_INSTALL_INTERACTIVE -eq "1") { return $true }
  if ($env:SWITCHBOARD_INSTALL_INTERACTIVE -eq "0") { return $false }
  if (-not [Environment]::UserInteractive) { return $false }
  try { if ([Console]::IsInputRedirected) { return $false } } catch { }
  foreach ($a in [Environment]::GetCommandLineArgs()) { if ($a -match '^-noni') { return $false } }
  return $true
}

function Resolve-Folder([string]$p) {
  # Trim, drop one pair of surrounding quotes ("Copy as path" adds them), expand
  # ~ and %VARS%, convert the MSYS form, and make the path absolute.
  $p = $p.Trim()
  if ($p.Length -ge 2 -and $p[0] -eq $p[-1] -and ($p[0] -eq '"' -or $p[0] -eq "'")) { $p = $p.Substring(1, $p.Length - 2).Trim() }
  if ($p -eq "~") { $p = $HOME }
  elseif ($p -match '^~[\\/]') { $p = Join-Path $HOME $p.Substring(2) }
  $p = To-WinPath ([Environment]::ExpandEnvironmentVariables($p))
  $p = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p)
  if ($p.Length -gt 3) { $p = $p.TrimEnd('\', '/') }
  return $p
}

function Get-SyncedSegment([string]$path) {
  foreach ($part in ($path -split '[\\/]')) {
    $low = $part.ToLower()
    foreach ($m in $SyncedSegments) { if ($low.Contains($m)) { return $part } }
  }
  return $null
}

function Get-FolderSuggestion([string]$envFilePath) {
  # Returns @(path, source). Order: SWITCHBOARD_ROOT, the env file record, the
  # installed repo around this script, then $HOME\Development\switchboard.
  $fromEnv = $env:SWITCHBOARD_ROOT
  if (-not $fromEnv) { $fromEnv = [Environment]::GetEnvironmentVariable("SWITCHBOARD_ROOT", "User") }
  if ($fromEnv) { return @((Resolve-Folder $fromEnv), "SWITCHBOARD_ROOT") }
  $rec = Read-EnvFile $envFilePath
  if ($rec.Contains("SWITCHBOARD_ROOT") -and $rec["SWITCHBOARD_ROOT"]) {
    return @((Resolve-Folder $rec["SWITCHBOARD_ROOT"]), "the folder recorded in $envFilePath")
  }
  $around = Split-Path -Parent (Split-Path -Parent $here)
  if (Test-Path -LiteralPath (Join-Path $around ".switchboard-kit\install.json")) { return @($around, "the installed repo this script sits in") }
  return @((Join-Path $HOME "Development\switchboard"), "the default")
}

# -- step 0: toolchain and PATH (native) ---------------------------------------
# winget installs Python, Git, ripgrep and Bun, but a terminal opened before the
# install keeps the old PATH, ripgrep and Bun often get no PATH entry, Git's cmd
# folder can be missing, and the Microsoft Store alias python3.exe in
# WindowsApps answers "Python was not found". Each fix below is idempotent.
function Update-SessionPath {
  # Reload PATH from the registry (machine, then user), keeping entries this session added.
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $parts = @()
  foreach ($src in @($machine, $user, $env:Path)) {
    if (-not $src) { continue }
    foreach ($d in ($src -split ';')) { if ($d -and -not ($parts -contains $d)) { $parts += $d } }
  }
  $env:Path = $parts -join ';'
}

function Add-ToolDir([string]$dir) {
  # User PATH and this session, so later steps and child scripts see the tool.
  Add-UserPath $dir
  if (-not (($env:Path -split ';') -contains $dir)) { $env:Path = "$dir;$env:Path" }
}

function Find-WingetTool([string]$exe, [string[]]$packagePrefixes, [string[]]$extraDirs) {
  # Returns the folder holding $exe from winget's Links or Packages folders, or $null.
  $winget = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet"
  $links = Join-Path $winget "Links"
  if (Test-Path -LiteralPath (Join-Path $links $exe)) { return $links }
  foreach ($d in $extraDirs) { if ($d -and (Test-Path -LiteralPath (Join-Path $d $exe))) { return $d } }
  $pkgs = Join-Path $winget "Packages"
  if (-not (Test-Path -LiteralPath $pkgs)) { return $null }
  foreach ($prefix in $packagePrefixes) {
    foreach ($pkg in @(Get-ChildItem -LiteralPath $pkgs -Directory -Filter "$prefix*" -ErrorAction SilentlyContinue)) {
      $hit = Get-ChildItem -LiteralPath $pkg.FullName -Recurse -Filter $exe -File -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($hit) { return $hit.DirectoryName }
    }
  }
  return $null
}

function Test-StoreAlias([string]$path) {
  return ($path -and $path -match '\\Microsoft\\WindowsApps\\')
}

function Get-PathBeforeWindowsApps([string[]]$parts, [string[]]$dirs) {
  # $parts with $dirs moved just ahead of the first WindowsApps entry (or to the front).
  $new = @()
  $placed = $false
  foreach ($d in $parts) {
    if (-not $d -or ($dirs -contains $d)) { continue }
    if (-not $placed -and (Test-StoreAlias ($d + "\"))) { $new += $dirs; $placed = $true }
    $new += $d
  }
  if (-not $placed) { $new = @($dirs) + $new }
  return , $new
}

function Move-UserPathBeforeWindowsApps([string[]]$dirs) {
  # The Store alias folder sits in the user PATH; the real Python must come first.
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if (-not $userPath) { $userPath = "" }
  $parts = @($userPath -split ';' | Where-Object { $_ })
  $new = Get-PathBeforeWindowsApps $parts $dirs
  if (($new -join ';') -eq ($parts -join ';')) { Ok "PATH: $($dirs -join ', ') already ahead of the Store alias"; return }
  Say "PATH: put $($dirs -join ', ') ahead of the Microsoft Store alias folder"
  if (-not $DryRun) { [Environment]::SetEnvironmentVariable("Path", ($new -join ';'), "User") }
  $env:Path = (@($dirs) + @($env:Path -split ';' | Where-Object { $_ -and -not ($dirs -contains $_) })) -join ';'
}

function Find-RealPython {
  # sys.executable of a working Python 3 that is not the Store alias, or $null.
  $cands = @()
  $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($launcher) { $cands += , @($launcher.Source, "-3") }
  foreach ($name in @("python.exe", "python3.exe")) {
    foreach ($c in @(Get-Command $name -All -ErrorAction SilentlyContinue)) {
      if (-not (Test-StoreAlias $c.Source)) { $cands += , @($c.Source) }
    }
  }
  foreach ($root in @((Join-Path $env:LOCALAPPDATA "Programs\Python"), $env:ProgramFiles)) {
    if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
    foreach ($d in @(Get-ChildItem -LiteralPath $root -Directory -Filter "Python3*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
      $exe = Join-Path $d.FullName "python.exe"
      if (Test-Path -LiteralPath $exe) { $cands += , @($exe) }
    }
  }
  foreach ($c in $cands) {
    $exe = $c[0]
    $rest = @()
    if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
    try {
      $out = & $exe @rest -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" 2>$null
      if ($LASTEXITCODE -eq 0 -and $out -and -not (Test-StoreAlias "$out")) { return "$out".Trim() }
    } catch { }
  }
  return $null
}

$script:RealPython = $null

function Install-Toolchain {
  Step "0. toolchain and PATH (native)"
  Update-SessionPath
  Ok "PATH reloaded from the registry (tools winget installed after this terminal opened are visible)"

  # Git: install.py and every repo script call git.exe by name.
  if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    $gitCmd = $null
    foreach ($base in @($env:ProgramFiles, $env:ProgramW6432, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"))) {
      if ($base -and (Test-Path -LiteralPath (Join-Path $base "Git\cmd\git.exe"))) { $gitCmd = Join-Path $base "Git\cmd"; break }
    }
    if ($gitCmd) { Add-ToolDir $gitCmd } else { Warn "git.exe not found. Install: winget install --id Git.Git -e, then rerun" }
  } else { Ok "git.exe on PATH" }

  # ripgrep and Bun: winget often leaves them off PATH.
  $tools = @(
    @{ exe = "rg.exe"; prefixes = @("BurntSushi.ripgrep"); extra = @(); hint = "winget install --id BurntSushi.ripgrep.MSVC -e" },
    @{ exe = "bun.exe"; prefixes = @("Oven-sh.Bun"); extra = @((Join-Path $userProfile ".bun\bin")); hint = "winget install --id Oven-sh.Bun -e (the switchboard Claude Code plugin hooks need it)" },
    # jq: start-day.sh and cmux-helpers.sh still need it (the tick-path scripts use Python).
    @{ exe = "jq.exe"; prefixes = @("jqlang.jq"); extra = @((Join-Path $userProfile "bin")); hint = "winget install --id jqlang.jq -e, or put the jqlang release jq-windows-amd64.exe in ~\bin as jq.exe (start-day.sh needs it)" }
  )
  foreach ($tool in $tools) {
    if (Get-Command $tool.exe -ErrorAction SilentlyContinue) { Ok "$($tool.exe) on PATH"; continue }
    $dir = Find-WingetTool $tool.exe $tool.prefixes $tool.extra
    if ($dir) { Add-ToolDir $dir } else { Warn "$($tool.exe) not found. Install: $($tool.hint)" }
  }

  # Python: a real python3 for bash, xargs, Task Scheduler and Python subprocesses alike.
  $real = Find-RealPython
  if (-not $real) {
    Warn "no working Python 3.11+ found. Install: winget install --id Python.Python.3.12 -e, open a new terminal, rerun"
  } else {
    Ok "python: $real"
    $script:RealPython = $real
    $pyDir = Split-Path -Parent $real
    $dirs = @($pyDir)
    $scripts = Join-Path $pyDir "Scripts"
    if (Test-Path -LiteralPath $scripts) { $dirs += $scripts }
    $py3 = Join-Path $pyDir "python3.exe"
    if (-not (Test-Path -LiteralPath $py3)) {
      Say "copy $real -> $py3 (Windows Python ships no python3.exe; scripts call python3)"
      if (-not $DryRun) {
        try { Copy-Item -LiteralPath $real -Destination $py3 }
        catch { Warn "could not write $py3 ($($_.Exception.Message)). Turn off the python.exe and python3.exe App Execution Aliases (Settings, Apps, Advanced app settings) instead." }
      }
    } else { Ok "$py3 present" }
    Move-UserPathBeforeWindowsApps $dirs
    if (-not $DryRun) {
      $first = Get-Command python3.exe -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($first -and -not (Test-StoreAlias $first.Source)) { Ok "python3 resolves to $($first.Source)" }
      else { Warn "python3 still resolves to the Store alias. Turn off the python.exe and python3.exe App Execution Aliases (Settings, Apps, Advanced app settings)." }
    }
  }

  # Ruby: only the full loop needs it (run-background-tasks.sh).
  if (Get-Command ruby.exe -ErrorAction SilentlyContinue) { Ok "ruby on PATH" }
  else { Say "ruby not on PATH; only the full loop needs it. Install for the full loop: winget install --id RubyInstallerTeam.Ruby.3.3 -e" }
  Say "open a new terminal (and restart wmux) so every pane sees the updated PATH"
}

if ($PrereqsOnly) {
  if ($Runtime -ne "native") { throw "-PrereqsOnly covers the native runtime; the wsl runtime keeps its toolchain inside the distro." }
  Install-Toolchain
  Write-Host "[switchboard] Toolchain ready. Next, from a new terminal: py <kit>\install.py --apply   (docs/install-windows.md)"
  exit 0
}

$stateDir = if ($env:SWITCHBOARD_STATE_HOME) { Resolve-Folder $env:SWITCHBOARD_STATE_HOME } else { Join-Path $userProfile ".switchboard" }
$envFile = Join-Path $stateDir "env"

if ($SwitchboardRoot) {
  $SwitchboardRoot = Resolve-Folder $SwitchboardRoot
} else {
  $suggestion = Get-FolderSuggestion $envFile
  if (Test-Interactive) {
    Write-Host ($DestinationPrompt -f $suggestion[0])
    $answer = Read-Host "Install folder [$($suggestion[0])]"
    $SwitchboardRoot = if ($answer -and $answer.Trim()) { Resolve-Folder $answer } else { $suggestion[0] }
  } else {
    $SwitchboardRoot = $suggestion[0]
    Write-Host "[switchboard] no -SwitchboardRoot and no terminal: using the suggestion $SwitchboardRoot (from $($suggestion[1]); pass -SwitchboardRoot to choose)"
  }
}
$syncedSegment = Get-SyncedSegment $SwitchboardRoot
Write-Host "[switchboard] install folder: $SwitchboardRoot"
if ($syncedSegment) {
  Write-Host "[switchboard] synced folder '$syncedSegment': allowed. Run switchboard from one device at a time there, because git locks and the background jobs clash across devices."
}

# =============================================================================
if ($Runtime -eq "native") {
  # --------------------------------------------------------------- native path
  Install-Toolchain
  Step "1. prerequisites (native)"
  if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
    throw "git.exe not found. Install Git for Windows: winget install --id Git.Git -e"
  }
  Ok "git.exe"
  $bashExe = Find-GitBash
  if (-not $bashExe) { throw "Git for Windows bash.exe not found (System32\bash.exe is the WSL launcher and does not count). Reinstall Git for Windows." }
  Ok "bash: $bashExe"
  $py = Get-Command py -ErrorAction SilentlyContinue
  if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
  if (-not $py) { throw "Windows Python not found. Install: winget install --id Python.Python.3.12 -e" }
  Ok "python: $($py.Source)"
  $wmuxCli = Find-Wmux
  Report-Wmux $wmuxCli
  if (-not (Test-Path -LiteralPath $SwitchboardRoot)) {
    $msg = "$SwitchboardRoot does not exist. Install there first: py <kit>\install.py --destination `"$SwitchboardRoot`" --apply"
    if ($DryRun) { Warn $msg } else { throw $msg }
  } elseif (-not (Test-Path -LiteralPath (Join-Path $SwitchboardRoot "system\windows\switchboard-env.sh"))) {
    Warn "$SwitchboardRoot does not look like a rendered switchboard repo (system\windows\switchboard-env.sh missing); pass -SwitchboardRoot"
  }
  $rootPosix = To-PosixPath $SwitchboardRoot
  Ok "repo: $SwitchboardRoot  ($rootPosix)"

  # Values the owner or install.py already chose win over the defaults.
  $fromFile = Read-EnvFile $envFile
  function Pick([string]$key, [string]$fallback) {
    if ($fromFile.Contains($key) -and $fromFile[$key]) { return $fromFile[$key] }
    $u = [Environment]::GetEnvironmentVariable($key, "User")
    if ($u) { return $u }
    return $fallback
  }
  $worktrees = Pick "SWITCHBOARD_WORKTREES_DIR" (Join-Path $env:LOCALAPPDATA "switchboard\worktrees")
  $stateHome = Pick "SWITCHBOARD_STATE_HOME" $stateDir
  $ampEditor = "powershell -NoProfile -ExecutionPolicy Bypass -File `"" + (Join-Path $SwitchboardRoot "system\windows\amp-obsidian-editor.ps1") + "`""

  Step "2. user environment"
  Set-UserEnv "SWITCHBOARD_ROOT" $SwitchboardRoot
  Set-UserEnv "SWITCHBOARD_MUX" "wmux"
  Set-UserEnv "SWITCHBOARD_RUNTIME" "native"
  Set-UserEnv "SWITCHBOARD_WORKTREES_DIR" $worktrees
  Set-UserEnv "SWITCHBOARD_STATE_HOME" $stateHome
  Set-UserEnv "AMP_EDITOR" $ampEditor
  Set-UserEnv "SWITCHBOARD_BOARD_AUTOSTART" "0"
  Add-UserPath (Join-Path $SwitchboardRoot "system\windows")
  # The shim on PATH does not count: only another CLI with the same name keeps the shim off PATH.
  switch (Get-SqKind $SwitchboardRoot $script:RealPython) {
    "real"   { Ok "another CLI named switchboard is on PATH; connector shim not added" }
    "shim"   { Ok "switchboard on PATH is the switchboard connector shim"; Add-UserPath (Join-Path $SwitchboardRoot "system\connectors") }
    default  { Say "no switchboard on PATH; adding the connector shim"; Add-UserPath (Join-Path $SwitchboardRoot "system\connectors") }
  }
  foreach ($d in @($worktrees, $stateHome)) {
    if (-not (Test-Path $d)) { Say "mkdir $d"; if (-not $DryRun) { New-Item -ItemType Directory -Path $d | Out-Null } }
  }

  Step "3. $envFile"
  $envWanted = [ordered]@{
    SWITCHBOARD_ROOT            = $SwitchboardRoot
    SWITCHBOARD_WORKTREES_DIR   = $worktrees
    SWITCHBOARD_STATE_HOME      = $stateHome
    SWITCHBOARD_MUX             = "wmux"
    SWITCHBOARD_RUNTIME         = "native"
    SWITCHBOARD_SYNCED_SEGMENT  = $(if ($syncedSegment) { $syncedSegment } else { "" })
    SWITCHBOARD_BOARD_AUTOSTART = "0"
  }
  $envReplace = @("SWITCHBOARD_ROOT", "SWITCHBOARD_SYNCED_SEGMENT")
  $existingEnv = Read-EnvFile $envFile
  function Test-StaleEnvPath([string]$key) {
    # True when the key is unset, or points at a file that is gone or at the Store stub.
    if (-not $existingEnv.Contains($key) -or -not $existingEnv[$key]) { return $true }
    $v = To-WinPath $existingEnv[$key]
    return (-not (Test-Path -LiteralPath $v -PathType Leaf)) -or (Test-StoreAlias $v)
  }
  # Task Scheduler keeps the PATH from sign-in, so a scheduled task cannot see
  # tools installed or put on PATH since. Record the real Python and the tool
  # folders here; switchboard-env.sh turns them into PATH entries and SWITCHBOARD_PYTHON.
  $realPy = if ($script:RealPython) { $script:RealPython } else { Find-RealPython }
  if ($realPy) {
    $toolDirs = @((Split-Path -Parent $realPy))
    foreach ($exe in @("git.exe", "rg.exe", "bun.exe", "ruby.exe")) {
      $c = Get-Command $exe -ErrorAction SilentlyContinue | Select-Object -First 1
      if ($c) { $toolDirs += (Split-Path -Parent $c.Source) }
    }
    $envWanted["SWITCHBOARD_TOOL_PATH"] = (@($toolDirs | ForEach-Object { $_ -replace '\\', '/' } | Select-Object -Unique) -join ";")
    $envReplace += "SWITCHBOARD_TOOL_PATH"
    $envWanted["SWITCHBOARD_PYTHON"] = ($realPy -replace '\\', '/')
    if (Test-StaleEnvPath "SWITCHBOARD_PYTHON") { $envReplace += "SWITCHBOARD_PYTHON" }
  } else {
    Warn "no real Python found; SWITCHBOARD_PYTHON and SWITCHBOARD_TOOL_PATH not recorded (scheduled tasks may not find python3)"
  }
  $rubyCmd = Get-Command ruby.exe -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($rubyCmd) {
    $envWanted["SWITCHBOARD_RUBY"] = ($rubyCmd.Source -replace '\\', '/')
    if (Test-StaleEnvPath "SWITCHBOARD_RUBY") { $envReplace += "SWITCHBOARD_RUBY" }
  }
  if ($wmuxCli) {
    $envWanted["WMUX_BIN"] = ($wmuxCli -replace '\\', '/')
    if (Test-StaleEnvPath "WMUX_BIN") { $envReplace += "WMUX_BIN" }
  }
  Merge-EnvFile $envFile $envWanted $envReplace

  Step "4. Git Bash shell hook"
  # Git Bash uses %HOME% when set, else %USERPROFILE%.
  $bashHome = if ($env:HOME) { $env:HOME } else { $userProfile }
  $bashrc = Join-Path $bashHome ".bashrc"
  Add-BashrcLine $bashrc "source `"$rootPosix/system/windows/switchboard-env.sh`"" "system/windows/switchboard-env.sh"

  Step "5. background loops (Task Scheduler)"
  if ($SkipTasks) { Warn "skipped (-SkipTasks)" }
  else {
    $argv = @("-Runtime", "native", "-SwitchboardRoot", $SwitchboardRoot)
    if ($DryRun) { $argv += "-DryRun" }
    Invoke-Child "register-scheduled-tasks.ps1" $argv
  }

  Step "6. wmux sidebar seed"
  $session = Join-Path $env:APPDATA "wmux\sessions\session.json"
  if ($SkipSeed) { Warn "skipped (-SkipSeed)" }
  elseif (-not (Test-Path $session)) {
    Warn "no wmux session yet. Open wmux, run 'bash system/scripts/ensure-routine-workspaces.sh switchboard launchpad todo comms-triage' in a pane, then: powershell -File system\windows\seed-session.ps1 -Restart"
  } elseif ($DryRun) {
    # seed-session.ps1 refuses a running wmux even for a preview; do not call it here.
    Say "dry run: would seed the sidebar with seed-session.ps1 (preview it with: powershell -File system\windows\seed-session.ps1 -DryRun)"
  } elseif (Get-Process -Name wmux -ErrorAction SilentlyContinue) {
    Warn "wmux is running; seed later with: powershell -File system\windows\seed-session.ps1 -Restart"
  } else {
    Invoke-Child "seed-session.ps1" @("-Runtime", "native")
  }

  Step "done"
  Write-Host @"
[switchboard] Next (open a new terminal first so the user environment is loaded). switchboard lives in: $SwitchboardRoot
  1. Open wmux. In its pane run:  cd "$rootPosix" && bash system/scripts/ensure-routine-workspaces.sh switchboard launchpad todo comms-triage
     (opens CoS, Launchpad, Todo and Comms Triage; echo `$CMUX_BIN should print .../system/scripts/mux/cmux)
  2. From PowerShell:  powershell -ExecutionPolicy Bypass -File "$(Join-Path $here 'seed-session.ps1')" -Restart
     (quits wmux, writes pins, accent rails and order into session.json, applies sidebar prefs, relaunches wmux)
  3. Verify in a Git Bash or wmux pane:  cd "$rootPosix" && bash system/scripts/verify-kit-install.sh
     First-run checks: cmux --version ; seed-session.ps1 -DryRun ; CMUX_SHIM_TRACE=1 cmux list-workspaces
  4. Limits and the procedure for each: $(Join-Path $here 'README.md')
"@
  exit 0
}

# ------------------------------------------------------------------- wsl path
Step "1. prerequisites (wsl)"
Report-Wmux (Find-Wmux)
$wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
if (-not $wsl) { throw "WSL2 is required for -Runtime wsl (wsl --install). The default native runtime needs no WSL: rerun without -Runtime wsl." }
if (-not $Distro) {
  $Distro = (wsl.exe -l -q 2>$null | Where-Object { $_ -and $_.Trim() } | Select-Object -First 1)
  if ($Distro) { $Distro = $Distro.Trim() -replace "`0", "" }
}
if (-not $Distro) { throw "no WSL distribution found; run: wsl --install -d Ubuntu" }
Ok "WSL distro $Distro"
foreach ($tool in @("git", "python")) {
  if (Get-Command $tool -ErrorAction SilentlyContinue) { Ok "$tool on Windows PATH" } else { Warn "$tool not on Windows PATH (only needed for native-Windows callers; WSL has its own)" }
}
if (-not $WslRoot) {
  $WslRoot = (wsl.exe -d $Distro -- wslpath -a ($SwitchboardRoot -replace '\\', '/') 2>$null)
  if ($WslRoot) { $WslRoot = $WslRoot.Trim() }
}
if (-not $WslRoot) { $WslRoot = To-WslPath $SwitchboardRoot }
if (-not $WslRoot) { throw "could not derive the WSL path of $SwitchboardRoot; pass -WslRoot <path inside the distro>" }
Ok "repo: $SwitchboardRoot  (WSL: $WslRoot)"

Step "2. user environment"
Set-UserEnv "SWITCHBOARD_MUX" "wmux"
Set-UserEnv "SWITCHBOARD_ROOT" $SwitchboardRoot
Set-UserEnv "SWITCHBOARD_RUNTIME" "wsl"
Set-UserEnv "SWITCHBOARD_WSL_ROOT" $WslRoot
Set-UserEnv "SWITCHBOARD_WSL_DISTRO" $Distro
Set-UserEnv "SWITCHBOARD_BOARD_AUTOSTART" "0"
Set-UserEnv "AMP_EDITOR" ("powershell -NoProfile -ExecutionPolicy Bypass -File `"" + (Join-Path $here "amp-obsidian-editor.ps1") + "`"")
Add-UserPath $here

Step "3. WSL shell hook"
$line = "source `"$WslRoot/system/windows/switchboard-env.sh`""
$check = "grep -qsF 'system/windows/switchboard-env.sh' ~/.bashrc && echo present || echo absent"
$state = (wsl.exe -d $Distro -- bash -lc $check 2>$null)
if ("$state".Trim() -eq "present") { Ok "~/.bashrc already sources switchboard-env.sh" }
else {
  Say "append to ~/.bashrc: $line"
  if (-not $DryRun) {
    $append = "printf '\n# switchboard Kit (wmux backend)\n%s\n' '$line' >> ~/.bashrc && chmod +x '$WslRoot/system/windows/switchboard-env.sh' '$WslRoot/system/scripts/mux/cmux' 2>/dev/null; true"
    wsl.exe -d $Distro -- bash -lc $append | Out-Null
  }
}

Step "4. background loops (Task Scheduler)"
if ($SkipTasks) { Warn "skipped (-SkipTasks)" }
else {
  $argv = @("-Runtime", "wsl", "-SwitchboardRoot", $WslRoot, "-Distro", $Distro)
  if ($DryRun) { $argv += "-DryRun" }
  Invoke-Child "register-scheduled-tasks.ps1" $argv
}

Step "5. wmux sidebar seed"
$session = Join-Path $env:APPDATA "wmux\sessions\session.json"
if ($SkipSeed) { Warn "skipped (-SkipSeed)" }
elseif (-not (Test-Path $session)) {
  Warn "no wmux session yet. Open wmux, in a WSL pane run 'bash system/scripts/ensure-routine-workspaces.sh switchboard launchpad todo comms-triage', then: powershell -File system\windows\seed-session.ps1 -Runtime wsl -Restart"
} elseif ($DryRun) {
  Say "dry run: would seed the sidebar with seed-session.ps1 -Runtime wsl (preview it with: powershell -File system\windows\seed-session.ps1 -Runtime wsl -DryRun)"
} elseif (Get-Process -Name wmux -ErrorAction SilentlyContinue) {
  Warn "wmux is running; seed later with: powershell -File system\windows\seed-session.ps1 -Runtime wsl -Restart"
} else {
  Invoke-Child "seed-session.ps1" @("-Runtime", "wsl")
}

Step "done"
Write-Host @"
[switchboard] Next (open a new wmux window first so the user environment is loaded):
  1. In a WSL pane run:  bash system/scripts/ensure-routine-workspaces.sh switchboard launchpad todo comms-triage
  2. From PowerShell:  powershell -ExecutionPolicy Bypass -File system\windows\seed-session.ps1 -Runtime wsl -Restart
  3. Verify in a WSL pane:  cmux identify   and   cd "$WslRoot" && bash system/scripts/verify-kit-install.sh
  4. Limits and the procedure for each: system\windows\README.md
"@
