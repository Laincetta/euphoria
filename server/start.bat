@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
:run
%PY% server.py
if %errorlevel%==3 (echo Restarting after update... & goto run)
pause
