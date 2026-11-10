@echo off
rem Thin wrapper: all logic lives in run.ps1 (the run.sh counterpart).
rem ExecutionPolicy bypass for this process only -- does not change the system.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
