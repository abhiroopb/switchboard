<#
.SYNOPSIS
  Make a Markdown note opened from Windows (Explorer double-click, a file://
  link handed to the shell, os.startfile) open in the right Obsidian vault.

.DESCRIPTION
  Obsidian's installer maps .md to Obsidian.exe "%1", but Obsidian ignores a
  bare file argument for a note that already sits in a vault (verified on
  Obsidian 1.15.0: nothing opens). PM OS hands review notes over as file://
  links on Windows, so without this those clicks do nothing.

  This points the open verb of whatever ProgId opens .md (Obsidian's own
  "Markdown" on a stock install, named by the hash-protected UserChoiceLatest)
  at system\windows\amp-obsidian-editor.ps1, run headless (conhost --headless,
  no console flash), as a per-user HKCU override; HKLM and the protected choice
  stay untouched. With no .md handler at all it maps .md to its own ProgId,
  PMOS.ObsidianNote. The shim turns a note inside a registered vault into
  obsidian://open?vault=<vault>&file=<note> (the most specific vault wins, so
  pm-os notes open in the pm-os vault even though it sits inside another
  vault) and hands any other .md to Obsidian.exe directly.

  Only HKCU is written; no admin. It changes a handler only when it is
  Obsidian or missing; a .md mapped to another editor (VS Code, Notepad++) is
  left alone with one line saying so (-Force overrides). The original command
  is kept in PmOsOriginalCommand, and -Unregister puts it back.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File system\windows\register-obsidian-notes.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File system\windows\register-obsidian-notes.ps1 -Check
  powershell -NoProfile -ExecutionPolicy Bypass -File system\windows\register-obsidian-notes.ps1 -Unregister
#>
[CmdletBinding()]
param(
  [string]$PmOsRoot = "",
  [switch]$Check,
  [switch]$Unregister,
  [switch]$Force,
  [switch]$DryRun
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Say([string]$m) { Write-Host "[pm-os] $m" }

$ProgId = "PMOS.ObsidianNote"
$Classes = "HKCU:\Software\Classes"
if (-not $PmOsRoot) { $PmOsRoot = if ($env:PM_OS_ROOT) { $env:PM_OS_ROOT } else { Split-Path -Parent (Split-Path -Parent $PSScriptRoot) } }
if ($PmOsRoot -match '^/([A-Za-z])(/.*)?$') { $PmOsRoot = $Matches[1].ToUpper() + ":" + $(if ($Matches[2]) { $Matches[2] -replace '/', '\' } else { "\" }) }
# The handler must outlive any task worktree: resolve a worktree to its main
# checkout through the shared git dir.
try {
  $common = (& git -C $PmOsRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
  if ($LASTEXITCODE -eq 0 -and $common -and ((Split-Path -Leaf $common) -eq ".git")) { $PmOsRoot = (Split-Path -Parent $common) -replace '/', '\' }
} catch { }
$shim = Join-Path $PmOsRoot "system\windows\amp-obsidian-editor.ps1"
$obsidian = @("$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe", "$env:ProgramFiles\Obsidian\Obsidian.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
$conhost = Join-Path $env:SystemRoot "System32\conhost.exe"
$command = "`"$conhost`" --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$shim`" `"%1`""

function Effective-ProgId([string]$ext) {
  # Windows 11 UserChoiceLatest, then UserChoice ("Always use this app"), then the
  # Classes default. The first two are hash-protected, so they are never rewritten.
  $fe = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\$ext"
  foreach ($k in @("$fe\UserChoiceLatest\ProgId", "$fe\UserChoice")) {
    if (Test-Path $k) { $v = (Get-Item $k).GetValue("ProgId"); if ($v) { return $v } }
  }
  foreach ($root in @("HKCU:\Software\Classes", "Registry::HKEY_LOCAL_MACHINE\Software\Classes")) {
    $k = Join-Path $root $ext
    if (Test-Path $k) { $v = (Get-Item $k).GetValue(""); if ($v) { return $v } }
  }
  return $null
}
function Open-Command([string]$progId) {
  foreach ($root in @("HKCU:\Software\Classes", "Registry::HKEY_LOCAL_MACHINE\Software\Classes")) {
    $k = Join-Path $root "$progId\shell\open\command"
    if (Test-Path $k) { return [string](Get-Item $k).GetValue("") }
  }
  return ""
}
function Is-Ours([string]$cmd) { return $cmd -match '(?i)amp-obsidian-editor\.ps1' }
function Shim-Of([string]$cmd) { if ($cmd -match '-File "([^"]+amp-obsidian-editor\.ps1)"') { return $Matches[1] }; return "" }

$exts = @(".md", ".markdown")
if ($Check) {
  $bad = 0
  foreach ($e in $exts) {
    $pid_ = Effective-ProgId $e
    $cmd = if ($pid_) { Open-Command $pid_ } else { "" }
    $target = Shim-Of $cmd
    if ((Is-Ours $cmd) -and $target -and -not (Test-Path -LiteralPath $target)) { Say "note  $e opener points at a missing script ($target); rerun system\windows\register-obsidian-notes.ps1 from the main checkout"; $bad++ }
    elseif (Is-Ours $cmd) { Say "ok    $e opens through PM OS (Obsidian vault-aware, ProgId $pid_)" }
    elseif (-not $pid_ -and $e -eq ".markdown") { Say "ok    $e has no handler (nothing to fix)" }
    else { Say "note  $e opens with '$pid_' ($cmd); run system\windows\register-obsidian-notes.ps1"; $bad++ }
  }
  exit $(if ($bad) { 1 } else { 0 })
}

if ($Unregister) {
  foreach ($e in $exts) {
    $pid_ = Effective-ProgId $e
    if (-not $pid_) { continue }
    $k = Join-Path $Classes "$pid_\shell\open\command"
    if ((Test-Path $k) -and (Is-Ours ([string](Get-Item $k).GetValue("")))) {
      $orig = (Get-Item $k).GetValue("PmOsOriginalCommand")
      if ($DryRun) { Say "would restore $pid_ open command" }
      elseif ($pid_ -eq $ProgId) {
        Remove-Item -Path (Join-Path $Classes $ProgId) -Recurse -Force
        $ek = Join-Path $Classes $e
        if ((Test-Path $ek) -and ((Get-Item $ek).GetValue("") -eq $ProgId)) { & reg.exe delete ("HKCU\Software\Classes\" + $e) /ve /f | Out-Null }
        Say "removed $ProgId and the $e mapping"
      }
      elseif ($orig -eq "<none>" -or -not $orig) { Remove-Item -Path $k -Force; Say "removed the HKCU open-command override of $pid_" }
      else { Set-Item -Path $k -Value $orig; Remove-ItemProperty -Path $k -Name PmOsOriginalCommand; Say "restored $pid_ -> $orig" }
    }
  }
  exit 0
}

if (-not $obsidian) { Say "skip: Obsidian is not installed (winget install Obsidian.Obsidian), so .md is left as it is"; exit 0 }
if (-not (Test-Path -LiteralPath $shim)) { Say "error: $shim missing"; exit 1 }

$changed = 0
foreach ($e in $exts) {
  $pid_ = Effective-ProgId $e
  if (-not $pid_) {
    if ($e -ne ".md") { continue }   # .markdown with no handler stays unmapped
    $pid_ = $ProgId
    if ($DryRun) { Say "would map $e -> $ProgId" }
    else {
      $k = Join-Path $Classes $e
      if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
      Set-Item -Path $k -Value $ProgId
      Set-Item -Path (New-Item -Path (Join-Path $Classes $ProgId) -Force).PSPath -Value "Markdown note (opens in Obsidian)"
    }
  }
  $cmd = Open-Command $pid_
  if (Is-Ours $cmd) {
    $k = Join-Path $Classes "$pid_\shell\open\command"
    if ((Test-Path $k) -and ([string](Get-Item $k).GetValue("") -ne $command)) {
      if ($DryRun) { Say "would update $pid_ open command to $shim" }
      else { Set-Item -Path $k -Value $command; Say "updated $pid_ open command to $shim" }
    }
    else { Say "ok    $e already opens through PM OS ($pid_)" }
    continue
  }
  # An existing ProgId with no open command belongs to an app that opens files
  # another way (packaged apps such as Notepad); leave it alone.
  if ($pid_ -ne $ProgId -and -not $cmd -and -not $Force) { Say "skip  $e opens with '$pid_', which has no open command (packaged app?); left alone (-Force to take it over)"; continue }
  if ($cmd -and $cmd -notmatch '(?i)obsidian\.exe' -and -not $Force) { Say "skip  $e opens with '$pid_' ($cmd), not Obsidian; left alone (-Force to take it over)"; continue }
  if ($DryRun) { Say "would point $pid_ (the $e handler) at the PM OS note opener; was: $cmd"; continue }
  # Per-user override of that ProgId's open verb; HKLM and the protected choice stay untouched.
  $k = Join-Path $Classes "$pid_\shell\open\command"
  New-Item -Path $k -Force | Out-Null
  Set-Item -Path $k -Value $command
  New-ItemProperty -Path $k -Name PmOsOriginalCommand -Value $(if ($cmd) { $cmd } else { "<none>" }) -Force | Out-Null
  Say "set   $e ($pid_) -> PM OS note opener (was: $cmd)"
  $changed++
}
if ($changed -and -not $DryRun) {
  Add-Type -Namespace PmOs -Name Shell -MemberDefinition '[DllImport("shell32.dll")] public static extern void SHChangeNotify(int e, uint f, IntPtr a, IntPtr b);'
  [PmOs.Shell]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
}
exit 0
