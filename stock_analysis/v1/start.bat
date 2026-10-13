@echo off
setlocal
echo ========================================
echo   个股 K线 + 波峰波谷分析 - 启动脚本
echo ========================================
echo.

:: 启动后端 (新窗口)
echo [1/2] 启动后端 (端口 8100, 避开参考项目的 8000)...
start "Backend - FastAPI (8100)" cmd /k "cd /d %~dp0backend && call run.bat"

:: 等待几秒再启动前端
timeout /t 3 /nobreak >nul

:: 启动前端
echo [2/2] 启动前端 (端口 5179, 避开参考项目的 5173)...
start "Frontend - Vite (5179)" cmd /k "cd /d %~dp0frontend && call run.bat"

echo.
echo 两个窗口已打开,关闭任一窗口将停止对应服务。
echo 后端: http://127.0.0.1:8100
echo 前端: http://127.0.0.1:5179
echo.
pause