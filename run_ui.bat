@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=.venv\Scripts\python.exe"
)

echo Starting Math Quiz Workbench...
echo Project: %CD%
echo Python: %PYTHON_EXE%
echo.

%PYTHON_EXE% codes\ui_server.py

echo.
echo UI server stopped. If the browser showed "refused to connect", check the error above.
pause
