@echo off
rem cmux.cmd: cmux-compatible entry point for Windows callers (PowerShell,
rem Task Scheduler, Amp running on Windows). It runs system\scripts\mux\cmux-shim.py
rem with the Windows Python and drives wmux through its CLI. Git Bash callers use
rem system/scripts/mux/cmux (bash) instead; both share the same sidecar state.
rem
rem SWITCHBOARD_RUNTIME=wsl keeps the old behavior: the shim runs inside the default
rem WSL distro (or SWITCHBOARD_WSL_DISTRO) through wsl.exe.
setlocal
set "HERE=%~dp0"
set "SHIM=%HERE%..\scripts\mux\cmux-shim.py"

if /I "%SWITCHBOARD_RUNTIME%"=="wsl" goto :wsl

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

:wsl
set "WSL_SHIM=%SWITCHBOARD_WSL_ROOT%/system/scripts/mux/cmux"
if not defined SWITCHBOARD_WSL_ROOT set "WSL_SHIM=$HOME/Development/switchboard/system/scripts/mux/cmux"
if defined SWITCHBOARD_WSL_DISTRO (
  wsl.exe -d "%SWITCHBOARD_WSL_DISTRO%" -- bash -lc "\"%WSL_SHIM%\" \"$@\"" cmux %*
) else (
  wsl.exe -- bash -lc "\"%WSL_SHIM%\" \"$@\"" cmux %*
)
exit /b %ERRORLEVEL%
