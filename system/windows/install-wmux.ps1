<#
.SYNOPSIS
  Install wmux from its official source and give it the switchboard look.

.DESCRIPTION
  1. Install. When wmux is already installed this step only reports it. Else,
     with Scoop present, it uses wmux's own Scoop bucket
     (github.com/amirlehmam/wmux). Without Scoop it downloads the official
     installer from the wmux GitHub release, checks its SHA-256 against the
     digest GitHub publishes for that release asset, and runs it per user
     (no admin rights). Switchboard never ships wmux binaries; wmux is MIT
     licensed and installed from its author's release.
  2. Look. Runs system\windows\wmux_look.py apply, which merges the
     appearance and layout keys from system\windows\wmux-look.json into
     %APPDATA%\wmux\settings.json. When you already set one of those values
     to something else, it lists them, writes nothing and exits 3; -Force
     replaces them. Every changed file is backed up first. wmux must be
     closed for this step.

  On macOS and Linux the script prints that wmux is Windows only and exits 0.
  cmux on macOS keeps its own look.

.PARAMETER Version
  A wmux release such as 2.64.3, or "latest" (default). Scoop always installs
  the bucket's current version.

.PARAMETER Force
  Pass --force to the look step: replace settings you changed, after a backup.

.PARAMETER Instance
  wmux side-by-side instance name (WMUX_INSTANCE); the look goes to
  %APPDATA%\wmux-<name>. For trying the look without touching your wmux.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File system\windows\install-wmux.ps1
  powershell -ExecutionPolicy Bypass -File system\windows\install-wmux.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File system\windows\install-wmux.ps1 -SkipInstall -Force
#>
[CmdletBinding()]
param(
  [string]$Version = "latest",
  [switch]$Force,
  [switch]$DryRun,
  [switch]$SkipInstall,
  [switch]$SkipLook,
  [string]$Instance = $null
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Repo = "amirlehmam/wmux"
$RepoUrl = "https://github.com/$Repo"
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest on 5.1 crawls with the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
function Say([string]$msg) { Write-Host "[wmux] $msg" }

if ((Get-Variable IsWindows -ErrorAction SilentlyContinue) -and -not $IsWindows) {
  Say "wmux is Windows only; nothing to do here. On macOS cmux keeps its own look, and switchboard leaves cmux settings alone."
  exit 0
}

function Find-Wmux {
  $candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\wmux\wmux.exe"),
    $(if ($env:ProgramFiles) { Join-Path $env:ProgramFiles "wmux\wmux.exe" }),
    $(if ($env:SCOOP) { Join-Path $env:SCOOP "apps\wmux\current\wmux.exe" }),
    (Join-Path $HOME "scoop\apps\wmux\current\wmux.exe")
  ) | Where-Object { $_ }
  foreach ($c in $candidates) { if (Test-Path -LiteralPath $c) { return $c } }
  $cmd = Get-Command wmux.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

function Install-FromRelease([string]$Tag) {
  $api = if ($Tag -eq "latest") { "https://api.github.com/repos/$Repo/releases/latest" } else { "https://api.github.com/repos/$Repo/releases/tags/v$($Tag.TrimStart('v'))" }
  $release = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "switchboard-install" }
  $asset = $release.assets | Where-Object { $_.name -match '^wmux-[\d.]+-setup\.exe$' } | Select-Object -First 1
  if (-not $asset) { throw "release $($release.tag_name) has no setup.exe asset" }
  $digest = if ($asset.PSObject.Properties['digest']) { [string]$asset.digest } else { "" }
  if ($digest -notmatch '^sha256:([0-9a-f]{64})$') { throw "release $($release.tag_name) publishes no SHA-256 digest for $($asset.name); not installing an unchecked file" }
  $want = $Matches[1]
  $url = [string]$asset.browser_download_url
  if (-not $url.StartsWith("$RepoUrl/releases/download/")) { throw "download URL is not on the official wmux release page: $url" }
  Say "official release $($release.tag_name): $url"
  if ($DryRun) { Say "would download, check SHA-256 $want and run it for this user"; return }
  $dir = Join-Path ([IO.Path]::GetTempPath()) ("wmux-setup-" + [guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $dir | Out-Null
  try {
    $exe = Join-Path $dir $asset.name
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $exe).Hash.ToLower()
    if ($got -ne $want) { throw "SHA-256 mismatch for $($asset.name): got $got, want $want" }
    Say "SHA-256 matches; installing for this user"
    $p = Start-Process -FilePath $exe -ArgumentList "/S", "/currentuser" -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "wmux installer exited $($p.ExitCode)" }
  } finally {
    Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
  }
}

# -- 1. install ---------------------------------------------------------------
if ($SkipInstall) {
  Say "install: skipped (-SkipInstall)"
} else {
  $found = Find-Wmux
  if ($found) {
    Say "already installed: $found ($((Get-Item -LiteralPath $found).VersionInfo.ProductVersion))"
  } elseif (Get-Command scoop -ErrorAction SilentlyContinue) {
    Say "installing with Scoop from the wmux bucket ($RepoUrl)"
    $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
      $bucket = @(scoop bucket list 6>$null) | Where-Object { $_ -and $_.PSObject.Properties['Name'] -and $_.Name -eq 'wmux' } | Select-Object -First 1
      if ($bucket) {
        $src = ([string]$bucket.Source).TrimEnd('/') -replace '\.git$', ''
        if ($src -ne $RepoUrl) { throw "your Scoop bucket 'wmux' points at $src, not $RepoUrl; remove it or install wmux from $RepoUrl/releases" }
      }
      if ($DryRun) { Say ("would run: " + $(if (-not $bucket) { "scoop bucket add wmux $RepoUrl; " } else { "" }) + "scoop install wmux/wmux") }
      else {
        if (-not $bucket) { scoop bucket add wmux $RepoUrl }
        scoop install wmux/wmux
        if ($LASTEXITCODE -ne 0) { throw "scoop install wmux/wmux exited $LASTEXITCODE" }
      }
    } finally { $ErrorActionPreference = $eap }
  } else {
    Install-FromRelease $Version
  }
}

# -- 2. look ------------------------------------------------------------------
if ($SkipLook) { Say "look: skipped (-SkipLook)"; exit 0 }
$py = $null
foreach ($name in @("py", "python3", "python")) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notmatch 'WindowsApps') { $py = $cmd.Source; break }
}
if (-not $py) { throw "Python not found; install it (step 1 of docs/install-windows.md), then rerun with -SkipInstall" }
$lookArgs = @((Join-Path $PSScriptRoot "wmux_look.py"), "apply")
if ($Force) { $lookArgs += "--force" }
if ($DryRun) { $lookArgs += "--dry-run" }
if ($Instance) { $lookArgs += @("--instance", $Instance) }
& $py @lookArgs
exit $LASTEXITCODE
