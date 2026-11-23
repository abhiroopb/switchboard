@echo off
rem switchboard.cmd: entry point for the switchboard connector CLI for native Windows callers (PowerShell,
rem Task Scheduler, Amp running on Windows). It runs system/connectors/switchboard-shim.py
rem with the Windows Python and serves the mail, calendar, drive
rem and slack verbs from the REST APIs. WSL2 callers use
rem system/connectors/switchboard (bash) instead; both share ~/.switchboard/connectors.
setlocal
set "SHIM=%~dp0switchboard-shim.py"
if defined SWITCHBOARD_PYTHON (
  "%SWITCHBOARD_PYTHON%" "%SHIM%" %*
  exit /b %ERRORLEVEL%
)
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
  py -3 "%SHIM%" %*
  exit /b %ERRORLEVEL%
)
python "%SHIM%" %*
exit /b %ERRORLEVEL%
