@echo off
rem Builds Runemark (dist\Runemark\Runemark.exe). Double-click to run.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\build.ps1" %*
echo.
echo Finished. The full log is in build-log.txt. Press any key to close this window.
pause >nul
