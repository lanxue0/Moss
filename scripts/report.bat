@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
"%PY%" report.py %*
echo.
echo 提示词已生成到 reports\ 目录，可以粘给任意模型出日报。
pause
