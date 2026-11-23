# Open the switchboard routine tabs (CoS, To Do, Launchpad, Comms Triage) from any wmux pane,
# PowerShell included. wmux panes open PowerShell by default, where `bash` is not on PATH,
# so this calls Git for Windows bash explicitly. ensure-routine-workspaces.sh skips Comms Triage
# while SWITCHBOARD_COMMS_TRIAGE=off on this machine, and on a personal machine
# (SWITCHBOARD_MACHINE_ROLE=personal) skips Launchpad until it is loaded:
#   bash system/scripts/load-surface.sh Launchpad    (or say "load Launchpad" in CoS)
#
#   powershell -ExecutionPolicy Bypass -File system\windows\open-routines.ps1
#   powershell -ExecutionPolicy Bypass -File system\windows\open-routines.ps1 -Routines todo,launchpad
param([string[]]$Routines = @("switchboard", "todo", "launchpad", "comms-triage"))
$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$bash = @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe", "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe") |
  Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $bash) { throw "Git for Windows bash not found. Install it: winget install Git.Git" }
$posix = ($root -replace '\\', '/') -replace '^([A-Za-z]):', { "/" + $_.Groups[1].Value.ToLower() }
& $bash -lc "cd '$posix' && source system/windows/switchboard-env.sh >/dev/null 2>&1; bash system/scripts/ensure-routine-workspaces.sh $($Routines -join ' ')"
exit $LASTEXITCODE
