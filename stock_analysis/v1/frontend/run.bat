@echo off
setlocal
cd /d "%~dp0"
echo [frontend] 启动 Vite 开发服务器...
echo.
if not exist "node_modules" (
    echo [frontend] 安装依赖(首次启动较慢, 可能需要几分钟)...
    call npm install
    if errorlevel 1 (
        echo [ERROR] npm install 失败
        pause
        exit /b 1
    )
)
echo.
call npm run dev
pause