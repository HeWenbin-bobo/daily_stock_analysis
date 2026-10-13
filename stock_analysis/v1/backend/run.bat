@echo off
setlocal
cd /d "%~dp0"
echo [backend] 启动 FastAPI 服务...
echo.
if not exist ".env" (
    echo [WARN] .env 不存在, 使用 .env.example 模板
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        echo [INFO] 已复制 .env.example → .env
    )
)
echo [backend] 安装依赖(首次启动较慢)...
python -m pip install -q -r requirements.txt
echo.
python app.py