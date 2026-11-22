<#
.SYNOPSIS
  AMP_EDITOR shim for native-Windows Amp: a clicked file:// link to a vault
  note opens in Obsidian; anything else opens with the default handler.
  Windows counterpart of system/scripts/amp-obsidian-editor.sh.

.DESCRIPTION
  Amp hands $AMP_EDITOR the clicked target (a file:// URI or a path).
  When the target is a .md file under PM_OS_ROOT (the vault root), this
  rewrites it to obsidian://open?vault=<PM_OS_VAULT_NAME>&file=<relative path
  without .md> and opens it. Otherwise it opens the target as-is.

  Set in the user environment (install.ps1 does this):
    AMP_EDITOR = powershell -NoProfile -ExecutionPolicy Bypass -File <root>\system\windows\amp-obsidian-editor.ps1
    PM_OS_ROOT = C:\Users\<you>\Development\pm-os   (or \\wsl$\<Distro>\home\<you>\Development\pm-os)
    PM_OS_VAULT_NAME = pm-os                          (optional; default pm-os)
#>
param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]]$Target
)

$ErrorActionPreference = "Stop"
if (-not $Target) { exit 0 }
$raw = ($Target -join " ")

$vaultRoot = if ($env:PM_OS_VAULT_ROOT) { $env:PM_OS_VAULT_ROOT } else { $env:PM_OS_ROOT }
$vaultName = if ($env:PM_OS_VAULT_NAME) { $env:PM_OS_VAULT_NAME } else { "pm-os" }

function To-LocalPath([string]$s) {
  if ($s -match '^file://') {
    $u = [Uri]$s
    $p = [Uri]::UnescapeDataString($u.AbsolutePath)
    # WSL paths arrive as file:///home/... ; map through \\wsl$ when a distro is known.
    if ($p -match '^/[^/]' -and $p -notmatch '^/[A-Za-z]:') {
      $distro = if ($env:PM_OS_WSL_DISTRO) { $env:PM_OS_WSL_DISTRO } else { (wsl.exe -l -q 2>$null | Select-Object -First 1) }
      if ($distro) { return ("\\wsl$\" + $distro.Trim() + ($p -replace '/', '\')) }
      return $p
    }
    return ($p.TrimStart('/') -replace '/', '\')
  }
  return $s
}

$path = To-LocalPath $raw
# Strip a trailing :line[:col] that Amp may append.
$path = $path -replace ':\d+(:\d+)?$', ''

if ($vaultRoot -and $path.ToLower().EndsWith(".md")) {
  $rootNorm = ($vaultRoot.TrimEnd('\', '/') -replace '/', '\')
  $pathNorm = ($path -replace '/', '\')
  if ($pathNorm.ToLower().StartsWith($rootNorm.ToLower() + '\')) {
    $rel = $pathNorm.Substring($rootNorm.Length + 1)
    $rel = $rel.Substring(0, $rel.Length - 3) -replace '\\', '/'
    $uri = "obsidian://open?vault=" + [Uri]::EscapeDataString($vaultName) + "&file=" + [Uri]::EscapeDataString($rel)
    Start-Process $uri
    exit 0
  }
}

# Any other note: open it in the most specific registered Obsidian vault that
# holds it (obsidian.json), so a note in a nested vault opens in that vault.
# Obsidian.exe ignores a bare file argument for a note already in a vault
# (verified on Obsidian 1.15.0), which is why the .md association
# (register-obsidian-notes.ps1) points here instead of at Obsidian.exe.
# Only .md is a note to Obsidian; .markdown goes to Obsidian.exe below.
$isMd = $path.ToLower().EndsWith(".md")
$isNote = $isMd -or $path.ToLower().EndsWith(".markdown")
if ($isNote) {
  $pathNorm = ($path -replace '/', '\')
  $cfg = Join-Path $env:APPDATA "obsidian\obsidian.json"
  $best = $null
  if ($isMd -and (Test-Path -LiteralPath $cfg)) {
    try {
      $vaults = (Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json).vaults
      foreach ($p in $vaults.PSObject.Properties) {
        $vp = ([string]$p.Value.path).TrimEnd('\', '/') -replace '/', '\'
        if ($vp -and $pathNorm.ToLower().StartsWith($vp.ToLower() + '\') -and (-not $best -or $vp.Length -gt $best.Length)) { $best = $vp }
      }
    } catch { $best = $null }
  }
  if ($best) {
    $rel = $pathNorm.Substring($best.Length + 1) -replace '\\', '/'
    $rel = $rel -replace '\.md$', ''
    $uri = "obsidian://open?vault=" + [Uri]::EscapeDataString((Split-Path -Leaf $best)) + "&file=" + [Uri]::EscapeDataString($rel)
    Start-Process $uri
    exit 0
  }
  # Outside every vault: hand it to Obsidian directly (never back through the
  # .md association, which may point at this script).
  $obsidian = @("$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe", "$env:ProgramFiles\Obsidian\Obsidian.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
  if ($obsidian) { Start-Process -FilePath $obsidian -ArgumentList ('"' + $path + '"'); exit 0 }
  Start-Process notepad.exe -ArgumentList ('"' + $path + '"')
  exit 0
}

Start-Process $path
