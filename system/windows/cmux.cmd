@echo off
rem cmux.cmd: cmux-compatible entry point for Windows callers (PowerShell,
rem Task Scheduler, Amp running on Windows). It runs system\scripts\mux\cmux-shim.py
rem with the Windows Python and drives wmux through its CLI. Git Bash callers use
rem system/scripts/mux/cmux (bash) instead; both share the same sidecar state.
rem
rem PM_OS_RUNTIME=wsl keeps the old behavior: the shim runs inside the default
rem WSL distro (or PM_OS_WSL_DISTRO) through wsl.exe.
setlocal
set "HERE=%~dp0"
set "SHIM=%HERE%..\scripts\mux\cmux-shim.py"

if /I "%PM_OS_RUNTIME%"=="wsl" goto :wsl

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

:wsl
set "WSL_SHIM=%PM_OS_WSL_ROOT%/system/scripts/mux/cmux"
if not defined PM_OS_WSL_ROOT set "WSL_SHIM=$HOME/Development/pm-os/system/scripts/mux/cmux"
if defined PM_OS_WSL_DISTRO (
  wsl.exe -d "%PM_OS_WSL_DISTRO%" -- bash -lc "\"%WSL_SHIM%\" \"$@\"" cmux %*
) else (
  wsl.exe -- bash -lc "\"%WSL_SHIM%\" \"$@\"" cmux %*
)
exit /b %ERRORLEVEL%
