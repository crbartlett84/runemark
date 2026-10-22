@echo off
rem Runs Runemark from the source folder (sets it up the first time). Double-click to run.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 pause
