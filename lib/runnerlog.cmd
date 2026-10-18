@echo off
rem runnerlog for cmd.exe and PowerShell (Windows). Runs lib/runnerlog with
rem the Python runner uses: RUNNER_PYTHON (set in every run), else the one
rem install.ps1 recorded in python.path, else python.
setlocal
set "RL_PY=%RUNNER_PYTHON%"
if not defined RL_PY if exist "%~dp0..\python.path" set /p RL_PY=<"%~dp0..\python.path"
if not defined RL_PY set "RL_PY=python"
"%RL_PY%" "%~dp0runnerlog" %*
exit /b %ERRORLEVEL%
