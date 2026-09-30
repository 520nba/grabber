@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYBASE=C:\Users\DT\.workbuddy\binaries\python\versions\3.13.12\python.exe
set VENV=C:\Users\DT\.workbuddy\binaries\python\envs\grabber
if not exist "%VENV%\Scripts\python.exe" "%PYBASE%" -m venv "%VENV%"
"%VENV%\Scripts\python.exe" -m pip install -r requirements.txt
echo 安装完成
pause
