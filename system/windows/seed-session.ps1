<#
.SYNOPSIS
  Apply the PM OS sidebar layout (pins, accent colors) to the wmux
  session file and the wmux sidebar prefs, so the next wmux start shows it.

.DESCRIPTION
  wmux 2.49.0 has no CLI or IPC call that pins, colors, or reorders a
  workspace, and its renderer lists workspaces in array order. The PM OS shim
  (system/scripts/mux/cmux-shim.py) records pin, color, and order in its
  sidecar (%USERPROFILE%\.pm-os\wmux-sidecar.json, or PM_OS_STATE_HOME). This
  script copies that state into %APPDATA%\wmux\sessions\session.json so the
  native wmux sidebar matches at the next start:

    1. The fixed surfaces (CoS, To Do, Launchpad, Comms Triage) are pinned
       and tinted with their accent colors. This works with no sidecar at all
       (first run).
       PM_OS_COMMS_TRIAGE=off in local/machines/<MACHINE>.env leaves Comms
       Triage out. PM_OS_MACHINE_ROLE=personal there means a closed Launchpad
       is expected (it opens on "load Launchpad"), so it is not reported
       missing.
    2. Every workspace with a sidecar entry takes the sidecar `pinned` flag
       and `color` (an empty or missing color leaves the wmux color alone).
    3. The row order is left as wmux has it. wmux cannot order tabs, and
       PM OS does not reorder them on wmux, so this script never sorts rows and ignores the sidecar `order_index`.
       The section order in system/scripts/sidebar_order.py is cmux only.
    4. %APPDATA%\wmux\settings.json gets `wmux-sidebar-prefs` merged with
       activeTabIndicator=leftRail, showNotificationMessage=true,
       hideAllDetails=false, and `wmux-workspace-prefs.defaultShell` set to
       bash.exe (Git Bash; other keys are kept). -NoPrefs skips this.
    5. The fixed-surface rows get `shell` = bash.exe too, because a restored
       row reopens with the shell frozen in it and wins over the default
       preference. PM_OS_WMUX_SHELL overrides the spec; "default" leaves
       wmux's own pwsh choice alone. Native runtime only.

  Unread counts and lane status are not persisted by wmux; the shim pushes
  them live with `set-status`, `report-agent`, and `notify`.

  The script never fabricates a workspace (the splitTree schema is
  wmux-internal). When a fixed surface is missing it says so and tells you to
  launch it first with `bash system/cmux-helpers.sh launch <surface>`.

  wmux reads session.json only at startup and rewrites it every 30 s and at
  quit, so it must be closed while this runs. Without -Restart the script
  refuses while wmux.exe is up (unless -Force). With -Restart it asks wmux to
  quit (main window close, then Stop-Process after 10 s), waits for the
  session file to settle (mtime advance or 15 s), seeds, then relaunches wmux.

.PARAMETER Runtime
  native (default; $env:PM_OS_RUNTIME honored) or wsl. Only affects where the
  sidecar is looked up.

.PARAMETER Sidecar
  Windows path to the shim sidecar. Default (native): $env:PM_OS_WMUX_SIDECAR,
  else <PM_OS_STATE_HOME or %USERPROFILE%\.pm-os>\wmux-sidecar.json, with
  PM_OS_STATE_HOME also read from %USERPROFILE%\.pm-os\env. Default (wsl):
  `wsl.exe` resolves the same path inside the distro. Pass an empty string to
  skip the sidecar and apply only the fixed surfaces.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File system\windows\seed-session.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File system\windows\seed-session.ps1 -Restart
  powershell -ExecutionPolicy Bypass -File system\windows\seed-session.ps1 -Sidecar ""
#>
[CmdletBinding()]
param(
  [string]$SessionPath = (Join-Path $env:APPDATA "wmux\sessions\session.json"),
  [string]$SettingsPath = (Join-Path $env:APPDATA "wmux\settings.json"),
  [ValidateSet("native", "wsl")]
  [string]$Runtime = $(if ($env:PM_OS_RUNTIME -and $env:PM_OS_RUNTIME.ToLower() -eq "wsl") { "wsl" } else { "native" }),
  [string]$Sidecar = $null,
  [switch]$Restart,
  [switch]$NoPrefs,
  [switch]$DryRun,
  [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Say([string]$msg) { Write-Host "[pm-os] $msg" }
function Warn([string]$msg) { Write-Host "[pm-os] warn: $msg" -ForegroundColor Yellow }

# Accent per fixed surface (accents mirror the cmux-sidebar lane palette).
# Pattern matches the title's trailing name; the emoji prefix is ignored.
$Surfaces = @(
  @{ Match = "CoS";          Pattern = "CoS";                Color = "#5B8DEF" },
  @{ Match = "Todo";         Pattern = "(To ?-?Do|To-?dos?)"; Color = "#2BB673" },
  @{ Match = "Launchpad";    Pattern = "Launch ?pad";        Color = "#8E6CEF" },
  @{ Match = "Comms Triage"; Pattern = "Comms Triage";       Color = "#F2A93B" }
)

# Sidebar prefs the kit wants; everything else in the block is kept.
$WantedPrefs = @{
  activeTabIndicator      = "leftRail"
  showNotificationMessage = $true
  hideAllDetails          = $false
}

# -- small helpers ------------------------------------------------------------

function Get-Prop($obj, [string]$name, $default = $null) {
  # Strict-mode-safe property read on PSCustomObject or hashtable.
  if ($null -eq $obj) { return $default }
  if ($obj -is [System.Collections.IDictionary]) {
    if ($obj.Contains($name)) { return $obj[$name] } else { return $default }
  }
  $p = $obj.PSObject.Properties[$name]
  if ($null -ne $p) { return $p.Value }
  return $default
}

function Set-Prop($obj, [string]$name, $value) {
  $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
}

function Write-JsonFile([string]$path, $obj) {
  # UTF-8 without BOM (Electron's JSON.parse rejects a BOM), tmp + rename.
  $text = $obj | ConvertTo-Json -Depth 64
  $tmp = "$path.pm-os.tmp"
  $enc = New-Object System.Text.UTF8Encoding $false
  [System.IO.File]::WriteAllText($tmp, $text, $enc)
  Move-Item -Force $tmp $path
}

function Read-EnvFile {
  # KEY=VALUE lines written by install.py; returns a hashtable (may be empty).
  $map = @{}
  $file = Join-Path $env:USERPROFILE ".pm-os\env"
  if (-not (Test-Path $file)) { return $map }
  foreach ($line in Get-Content $file) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
    $idx = $t.IndexOf("=")
    $k = $t.Substring(0, $idx).Trim()
    $v = $t.Substring($idx + 1).Trim().Trim('"').Trim("'")
    if ($k) { $map[$k] = $v }
  }
  return $map
}

function Resolve-SidecarPath {
  # An explicit -Sidecar wins; an explicit empty string skips the sidecar.
  if ($script:SidecarGiven) {
    if ($Sidecar -eq "") { return $null }
    return $Sidecar
  }
  if ($env:PM_OS_WMUX_SIDECAR) { return $env:PM_OS_WMUX_SIDECAR }
  if ($Runtime -eq "wsl") {
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) { return $null }
    try {
      $posix = (& wsl.exe -- bash -lc 'printf %s "${PM_OS_WMUX_SIDECAR:-${PM_OS_STATE_HOME:-$HOME/.pm-os}/wmux-sidecar.json}"' 2>$null)
      if (-not $posix) { return $null }
      $win = (& wsl.exe -- wslpath -w $posix 2>$null)
      if ($win) { return $win.Trim() }
    } catch { }
    return $null
  }
  $stateHome = $env:PM_OS_STATE_HOME
  if (-not $stateHome) {
    $fromFile = Read-EnvFile
    if ($fromFile.ContainsKey("PM_OS_STATE_HOME")) { $stateHome = $fromFile["PM_OS_STATE_HOME"] }
  }
  if (-not $stateHome) { $stateHome = Join-Path $env:USERPROFILE ".pm-os" }
  return (Join-Path $stateHome "wmux-sidecar.json")
}

function Read-Sidecar([string]$Path) {
  if (-not $Path) { return @{} }
  if (-not (Test-Path $Path)) {
    Say "sidecar not found at $Path; applying fixed surfaces only"
    return @{}
  }
  try {
    $doc = Get-Content -Raw -Encoding UTF8 $Path | ConvertFrom-Json
  } catch {
    Warn "sidecar at $Path is not valid JSON; applying fixed surfaces only"
    return @{}
  }
  $map = @{}
  $workspaces = Get-Prop $doc "workspaces"
  if ($workspaces) {
    foreach ($prop in $workspaces.PSObject.Properties) { $map[$prop.Name] = $prop.Value }
  }
  Say ("sidecar: {0} ({1} workspaces)" -f $Path, $map.Count)
  return $map
}

# -- wmux lifecycle -----------------------------------------------------------

function Get-WmuxProcesses { return @(Get-Process -Name wmux -ErrorAction SilentlyContinue) }

function Stop-Wmux {
  # Returns the executable path of the main process (for the relaunch), or $null.
  $procs = Get-WmuxProcesses
  if ($procs.Count -eq 0) { Say "wmux is not running"; return $null }
  $main = $procs | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
  if (-not $main) { $main = $procs | Sort-Object StartTime | Select-Object -First 1 }
  $exe = $null
  try { $exe = $main.Path } catch { }
  if ($DryRun) { Say "dry run: would close wmux (pid $($main.Id)) and wait for session.json"; return $exe }
  Say "asking wmux (pid $($main.Id)) to quit"
  $asked = $false
  try { $asked = $main.CloseMainWindow() } catch { }
  if (-not $asked) { Warn "no main window to close; falling back to Stop-Process after the wait" }
  $deadline = (Get-Date).AddSeconds(10)
  while ((Get-WmuxProcesses).Count -gt 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 250 }
  $left = Get-WmuxProcesses
  if ($left.Count -gt 0) {
    Warn "wmux still running after 10 s; Stop-Process (last resort)"
    $left | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
  }
  return $exe
}

function Wait-SessionSettled([datetime]$before) {
  # wmux writes session.json at quit (atomic tmp+rename); wait for the mtime to
  # move past the pre-quit value, or give up after 15 s.
  if ($DryRun) { return }
  $deadline = (Get-Date).AddSeconds(15)
  while ((Get-Date) -lt $deadline) {
    if (Test-Path $SessionPath) {
      $now = (Get-Item $SessionPath).LastWriteTimeUtc
      if ($now -gt $before) { Say "session.json updated at $($now.ToString('o'))"; return }
    }
    Start-Sleep -Milliseconds 300
  }
  Warn "session.json mtime did not advance within 15 s; seeding the file as it is"
}

function Start-Wmux([string]$exe) {
  if ($DryRun) { Say "dry run: would relaunch wmux"; return }
  if ($exe -and (Test-Path $exe)) { Say "relaunching $exe"; Start-Process $exe; return }
  $cmd = Get-Command wmux.exe -ErrorAction SilentlyContinue
  if (-not $cmd) { $cmd = Get-Command wmux -ErrorAction SilentlyContinue }
  if ($cmd) { Say "relaunching $($cmd.Source)"; Start-Process $cmd.Source; return }
  Warn "wmux not found on PATH; start it by hand"
}

# -- sidebar prefs ------------------------------------------------------------

function Set-SidebarPrefs {
  # Merges the kit's keys into the `wmux-sidebar-prefs` and
  # `wmux-workspace-prefs` blocks of settings.json; other keys are kept.
  if ($NoPrefs) { Say "sidebar prefs: skipped (-NoPrefs)"; return }
  $settings = $null
  if (Test-Path $SettingsPath) {
    try { $settings = Get-Content -Raw -Encoding UTF8 $SettingsPath | ConvertFrom-Json } catch { Warn "settings.json is not valid JSON; leaving it alone"; return }
  }
  if ($null -eq $settings) { $settings = [pscustomobject]@{} }
  $delta = @()
  $blocks = @(
    @{ Key = "wmux-sidebar-prefs";   Wanted = $WantedPrefs },
    @{ Key = "wmux-workspace-prefs"; Wanted = $WantedWorkspacePrefs }
  )
  foreach ($b in $blocks) {
    if ($b.Wanted.Count -eq 0) { continue }
    $prefs = Get-Prop $settings $b.Key
    if ($null -eq $prefs) { $prefs = [pscustomobject]@{} }
    $touched = $false
    foreach ($k in $b.Wanted.Keys) {
      $cur = Get-Prop $prefs $k
      if ($cur -ne $b.Wanted[$k]) { Set-Prop $prefs $k $b.Wanted[$k]; $delta += "$($b.Key).$k=$($b.Wanted[$k])"; $touched = $true }
    }
    if ($touched) { Set-Prop $settings $b.Key $prefs }
  }
  if ($delta.Count -eq 0) { Say "sidebar and workspace prefs already match"; return }
  Say ("prefs: {0} -> {1}" -f ($delta -join ", "), $SettingsPath)
  if ($DryRun) { return }
  $dir = Split-Path -Parent $SettingsPath
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
  if (Test-Path $SettingsPath) { Copy-Item $SettingsPath "$SettingsPath.bak" -Force }
  Write-JsonFile $SettingsPath $settings
}

# -- session seed -------------------------------------------------------------

function Seed-Session([hashtable]$side) {
  if (-not (Test-Path $SessionPath)) {
    throw "wmux session not found at $SessionPath. Start wmux once, launch the surfaces, quit wmux, then rerun."
  }
  $json = Get-Content -Raw -Encoding UTF8 $SessionPath | ConvertFrom-Json
  $version = Get-Prop $json "version"
  if ($version -ne 1) { Warn "session.json version $version (expected 1); proceeding" }
  $windows = Get-Prop $json "windows" @()
  if (-not $windows) { Warn "session.json has no windows; nothing to seed"; return $false }

  $changed = $false
  foreach ($win in @($windows)) {
    $ws = @(Get-Prop $win "workspaces" @())
    if (-not $ws) { continue }
    $missing = @()

    # 1. Fixed surfaces: pin and color. Titles carry an emoji
    #    prefix ("🧭 CoS"); match on the trailing name.
    foreach ($s in $Surfaces) {
      $hit = $ws | Where-Object { (Get-Prop $_ "title" "") -match ("(?i)(^|\s)" + $s.Pattern + "$") } | Select-Object -First 1
      if (-not $hit) {
        if ($s.Match -notin $script:OnDemandMatch) { $missing += $s.Match }
        continue
      }
      if (-not (Get-Prop $hit "pinned" $false)) { Set-Prop $hit pinned $true; $changed = $true }
      if ((Get-Prop $hit "customColor" "") -ne $s.Color) { Set-Prop $hit customColor $s.Color; $changed = $true }
      # A restored row reopens with the shell frozen in it. A surface first
      # opened in pwsh keeps reopening in pwsh unless the row is repointed.
      if ($DefaultShell -and $Runtime -eq "native" -and (Get-Prop $hit "shell" "") -ne $DefaultShell) {
        Set-Prop $hit shell $DefaultShell; $changed = $true
      }
    }

    # 2. Sidecar pin and color per workspace id (wins over the fixed defaults).
    $applied = 0
    foreach ($w in $ws) {
      $wid = [string](Get-Prop $w "id" "")
      if (-not $wid -or -not $side.ContainsKey($wid)) { continue }
      $entry = $side[$wid]
      $applied++
      $pinVal = Get-Prop $entry "pinned"
      if ($null -ne $pinVal) {
        $pin = [bool]$pinVal
        if ([bool](Get-Prop $w "pinned" $false) -ne $pin) { Set-Prop $w pinned $pin; $changed = $true }
      }
      $color = [string](Get-Prop $entry "color" "")
      if ($color -and (Get-Prop $w "customColor" "") -ne $color) { Set-Prop $w customColor $color; $changed = $true }
    }

    $pinnedTitles = ($ws | Where-Object { Get-Prop $_ "pinned" $false } | ForEach-Object { Get-Prop $_ "title" "" }) -join " | "
    Say ("window {0}: {1} rows, {2} with sidecar state; pinned -> {3}" -f (Get-Prop $win "id" "?"), $ws.Count, $applied, $pinnedTitles)
    if ($missing) {
      Warn ("missing surfaces: {0}. In a wmux pane run: bash system/cmux-helpers.sh launch <surface>" -f ($missing -join ", "))
    }
  }

  if (-not $changed) { Say "session.json already matches; nothing to do"; return $false }
  if ($DryRun) { Say "dry run: not writing $SessionPath"; return $true }
  Copy-Item $SessionPath "$SessionPath.bak" -Force
  Write-JsonFile $SessionPath $json
  Say "wrote $SessionPath (backup at $SessionPath.bak)"
  return $true
}

# -- main ---------------------------------------------------------------------

# Default shell for every new wmux pane. wmux picks pwsh, then powershell,
# then cmd on its own; PM OS scripts, the seeded surfaces, and the .bashrc
# that sources pm-os-env.sh all assume Git Bash. The bare name "bash.exe" is
# the alias wmux resolves at Git's install root (never the WSL launcher in
# System32) and starts with --login -i. PM_OS_WMUX_SHELL overrides; "default"
# leaves wmux's own choice in place.
$DefaultShell = $null
if ($null -ne $env:PM_OS_WMUX_SHELL) {
  $DefaultShell = $env:PM_OS_WMUX_SHELL.Trim()
} else {
  $envFile = Read-EnvFile
  if ($envFile.ContainsKey("PM_OS_WMUX_SHELL")) { $DefaultShell = $envFile["PM_OS_WMUX_SHELL"].Trim() }
}
if ($null -eq $DefaultShell) { $DefaultShell = "bash.exe" }
if ($DefaultShell -in @("default", "wmux")) { $DefaultShell = "" }
$WantedWorkspacePrefs = @{}
if ($DefaultShell -and $Runtime -eq "native") { $WantedWorkspacePrefs.defaultShell = $DefaultShell }

# Per-machine keys live in local/machines/<MACHINE>.env (system/scripts/comms_triage_setting.py
# owns the path rule); a value in the process environment wins over the file.
function Read-MachineKey([string]$Key) {
  $val = [Environment]::GetEnvironmentVariable($Key)
  if ($val) { return $val }
  $dir = $env:PM_OS_LOCAL_DIR
  if (-not $dir) {
    $root = $env:PM_OS_ROOT
    if (-not $root) { $root = (Read-EnvFile)["PM_OS_ROOT"] }
    if (-not $root) { $root = Split-Path (Split-Path $PSScriptRoot) }
    $dir = Join-Path $root "local"
  }
  $machine = $env:PM_OS_MACHINE
  if (-not $machine) { $machine = [System.Net.Dns]::GetHostName() }
  $file = Join-Path $dir ("machines\" + $machine.Split(".")[0].ToUpper() + ".env")
  $found = ""
  if (Test-Path $file) {
    foreach ($line in Get-Content $file) {
      if ($line -match ('^\s*' + $Key + '\s*=(.*)$')) { $found = $Matches[1].Trim().Trim('"').Trim("'") }
    }
  }
  return $found
}

# Off switch: PM_OS_COMMS_TRIAGE=off leaves Comms Triage out of the fixed surfaces.
# No file or no key means on, as before.
$commsTriage = Read-MachineKey "PM_OS_COMMS_TRIAGE"
if ($commsTriage -and $commsTriage.Trim().ToLower() -in @("off", "0", "false", "no", "disabled")) {
  $Surfaces = @($Surfaces | Where-Object { $_.Match -ne "Comms Triage" })
}

# Machine role (system/scripts/machine_role.py): on a personal machine Launchpad opens only
# on "load Launchpad", so a closed one is expected, not missing. When it is open it still
# gets its pin, color, and place under To Do. Scheduled Jobs and Meetings never warn.
$script:OnDemandMatch = @()
if ((Read-MachineKey "PM_OS_MACHINE_ROLE").Trim().ToLower() -eq "personal" -and
    (Read-MachineKey "PM_OS_KEEP_SURFACES") -notmatch '(?i)launch ?pad') {
  $script:OnDemandMatch = @("Launchpad")
}

$script:SidecarGiven = $PSBoundParameters.ContainsKey("Sidecar")
$sidecarPath = Resolve-SidecarPath
$side = Read-Sidecar $sidecarPath

$running = (Get-WmuxProcesses).Count -gt 0
if ($running -and -not $Restart -and -not $Force -and $DryRun) {
  Say "dry run: wmux is running, so a real run needs -Restart (or -Force); previewing the seed only"
} elseif ($running -and -not $Restart -and -not $Force) {
  throw "wmux is running. Quit it first (it rewrites session.json on exit), pass -Restart to let this script quit and relaunch it, or pass -Force."
}

$exe = $null
if ($Restart -and $running) {
  $before = if (Test-Path $SessionPath) { (Get-Item $SessionPath).LastWriteTimeUtc } else { [datetime]::MinValue }
  $exe = Stop-Wmux
  Wait-SessionSettled $before
}

[void](Seed-Session $side)
Set-SidebarPrefs

if ($Restart) { Start-Wmux $exe }
