@echo off
rem pmos.cmd: entry point for the PM OS connector CLI for native Windows callers (PowerShell,
rem Task Scheduler, Amp running on Windows). It runs system/connectors/pmos-shim.py
rem with the Windows Python and serves the mail, calendar, drive
rem and slack verbs from the REST APIs. WSL2 callers use
rem system/connectors/pmos (bash) instead; both share ~/.pm-os/connectors.
setlocal
set "SHIM=%~dp0pmos-shim.py"
if defined PM_OS_PYTHON (
  "%PM_OS_PYTHON%" "%SHIM%" %*
  exit /b %ERRORLEVEL%
)
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
  py -3 "%SHIM%" %*
  exit /b %ERRORLEVEL%
)
python "%SHIM%" %*
exit /b %ERRORLEVEL%
