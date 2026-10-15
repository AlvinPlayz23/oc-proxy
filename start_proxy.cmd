@echo off
REM Launch the OpenCode compat shim detached at logon.
REM start_proxy.ps1 picks a python that has aiohttp, is idempotent
REM (no-op if the port already answers), and waits for /health before exiting.
REM Registry autostart entry: HKCU\...\Run\OpenCodeCompatShim
REM
REM NOTE: no ">>" redirect here. cmd.exe opens a redirect target exclusively,
REM so two starters racing at logon collide. start_proxy.ps1 appends its own
REM diagnostics via Add-Content (open, write, close), which is concurrency-safe.
REM The proxy's stdout/stderr are handed to it by Start-Process inside that
REM script -- redirecting them here too gives two writers one file and the
REM proxy never starts.
REM
REM "%~dp0" keeps this portable: it resolves to the script's own folder.
REM start "" /b detaches, so logon never blocks on the PowerShell child.
start "" /b powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ^
  -File "%~dp0start_proxy.ps1" 18788
exit /b 0
