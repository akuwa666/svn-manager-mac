@echo off
chcp 65001 >nul 2>nul
setlocal
set "PS1=%~dp0svnmgr.ps1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
if errorlevel 1 (
    echo.
    echo 启动失败。请查看上面的提示，或手动运行：
    echo   python "%~dp0..\svnmanager.py"
    pause
)
