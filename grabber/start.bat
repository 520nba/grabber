@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\DT\.workbuddy\binaries\python\envs\grabber\Scripts\python.exe
if not exist "%PY%" (
  echo 虚拟环境不存在，请先运行 install.bat
  pause
  exit /b 1
)
echo 启动服务，局域网/隧道地址 http://0.0.0.0:8765
"%PY%" server.py
pause
