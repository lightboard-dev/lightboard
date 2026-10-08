@echo off
set "SCRIPT_DIR=%~dp0"
where py >nul 2>&1
if errorlevel 1 goto python
py -3 "%SCRIPT_DIR%lightboard.py" %*
exit /b %errorlevel%
:python
python "%SCRIPT_DIR%lightboard.py" %*
