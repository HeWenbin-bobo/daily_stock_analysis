@echo off
REM ============================================================
REM  三榜交集分析 - 一键启动脚本
REM  Double-click to launch:
REM    - FastAPI backend (port 8000, shared with main project)
REM    - Vue 3 + Vite dev server (port 5173)
REM    - Auto-open browser to the hot-analysis UI
REM
REM  与主项目 daily_stock.bat 的关键差异：
REM    1. 同时启动后端 + 前端 dev server（主项目是 build + 后端）
REM    2. 端口 8000 已启动时**不**重复启动（共享主项目后端）
REM    3. 自动写入 .run_state/backend.pid 标记，便于 stop 识别
REM ============================================================
REM
REM Why this script does NOT use `conda activate`:
REM   Same rationale as daily_stock.bat. We use `conda run -n <env>`
REM   which does not require shell hooks absent in a double-clicked
REM   .bat session.
REM ============================================================

cd /d "%~dp0"

set "ROOT_DIR=%CD%"
set "PROJECT_ROOT=%ROOT_DIR%\.."
set "FRONTEND_DIR=%ROOT_DIR%\frontend"
set "BACKEND_PORT=8000"
set "FRONTEND_PORT=5173"
set "CONDA_ENV=daily_stock_analysis"
set "STATE_DIR=%ROOT_DIR%\.run_state"

echo.
echo ============================================================
echo   Hot-Analysis Launcher
echo   Working dir   : %ROOT_DIR%
echo   Project root  : %PROJECT_ROOT%
echo   Frontend dir  : %FRONTEND_DIR%
echo   Backend port  : %BACKEND_PORT%  (shared with main project)
echo   Frontend port : %FRONTEND_PORT%
echo   Conda env     : %CONDA_ENV%
echo ============================================================
echo.

REM --- 1. Locate conda.bat --------------------------------------------
echo [1/6] Locating conda ...
set "CONDA_BAT="

where conda >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%C in ('where conda') do (
        if not defined CONDA_BAT set "CONDA_BAT=%%C"
    )
)

REM Fall back to well-known install locations
if not defined CONDA_BAT if exist "C:\ProgramData\miniforge3\condabin\conda.bat" set "CONDA_BAT=C:\ProgramData\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\miniforge3\condabin\conda.bat"  set "CONDA_BAT=%USERPROFILE%\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "D:\miniforge3\condabin\conda.bat"              set "CONDA_BAT=D:\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "C:\miniforge3\condabin\conda.bat"              set "CONDA_BAT=C:\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\anaconda3\condabin\conda.bat"    set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\AppData\Local\Continuum\anaconda3\condabin\conda.bat" set "CONDA_BAT=%USERPROFILE%\AppData\Local\Continuum\anaconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "D:\anaconda3\condabin\conda.bat"               set "CONDA_BAT=D:\anaconda3\condabin\conda.bat"

if not defined CONDA_BAT (
    echo [ERROR] conda.bat not found. Please run "conda init cmd.exe" or
    echo         install Miniforge/Anaconda.
    goto :error
)
echo       Using: %CONDA_BAT%

REM --- 2. Verify the conda env exists --------------------------------
echo [2/6] Verifying conda env '%CONDA_ENV%' ...
"%CONDA_BAT%" env list | findstr /R /C:"%CONDA_ENV% " >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Conda env '%CONDA_ENV%' not found.
    echo         Available envs:
    "%CONDA_BAT%" env list
    goto :error
)
echo       OK.

REM --- 3. Prepare frontend (node_modules) ----------------------------
echo [3/6] Preparing frontend ...
cd /d "%FRONTEND_DIR%"
if errorlevel 1 (
    echo [ERROR] Cannot enter %FRONTEND_DIR%
    goto :error
)
if not exist "node_modules" (
    echo       node_modules not found, running npm install - first run may take minutes ...
    call npm install
    if errorlevel 1 goto :error
) else (
    echo       node_modules exists, skipping npm install.
)
echo       OK.

REM --- 4. Start backend (only if port 8000 is free) ------------------
echo [4/6] Checking backend port %BACKEND_PORT% ...
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1

set "STARTED_BACKEND=0"
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %BACKEND_PORT% -State Listen -ErrorAction SilentlyContinue; if ($c) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo       Port %BACKEND_PORT% is occupied - reusing existing backend.
    echo       (If main project is patched, /api/v1/hot-analysis/* is already loaded.)
    if exist "%STATE_DIR%\backend.pid" del "%STATE_DIR%\backend.pid" >nul 2>&1
) else (
    echo       Port %BACKEND_PORT% is free - starting backend ...
    cd /d "%PROJECT_ROOT%"
    powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','\"%CONDA_BAT%\" run -n %CONDA_ENV% --no-capture-output python main.py --serve-only --port %BACKEND_PORT%' -WorkingDirectory '%PROJECT_ROOT%' -WindowStyle Hidden" >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to start backend process.
        goto :error
    )
    set "STARTED_BACKEND=1"
    REM Mark that this launcher owns the backend process (used by stop script)
    echo 1 > "%STATE_DIR%\backend.pid"
)

REM --- 5. Start Vite dev server --------------------------------------
echo [5/6] Starting Vite dev server on port %FRONTEND_PORT% ...
cd /d "%FRONTEND_DIR%"
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %FRONTEND_PORT% -State Listen -ErrorAction SilentlyContinue; if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }" >nul 2>&1
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','cd /d %FRONTEND_DIR% && npm run dev' -WindowStyle Hidden" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to start Vite dev server.
    goto :error
)

REM --- 6. Wait for frontend to be ready + open browser ---------------
echo [6/6] Waiting for Vite dev server (http://localhost:%FRONTEND_PORT%/) ...
set /a attempts=0
:wait_loop
set /a attempts=%attempts%+1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:%FRONTEND_PORT%/' -UseBasicParsing -TimeoutSec 3 -Method Head; if ($r.StatusCode -ge 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :ready
if %attempts% GEQ 15 (
    echo [WARN] Vite not ready within 45s, opening browser anyway.
    goto :open_browser
)
timeout /t 3 /nobreak >nul
goto :wait_loop

:ready
echo [6/6] Vite dev server is ready.

:open_browser
start "" "http://localhost:%FRONTEND_PORT%/"

echo.
echo ============================================================
echo   Startup complete
echo   - Frontend  : http://localhost:%FRONTEND_PORT%/
echo   - Backend   : http://localhost:%BACKEND_PORT%/docs
echo   - API       : POST http://localhost:%BACKEND_PORT%/api/v1/hot-analysis/run
echo   - Health    : GET  http://localhost:%BACKEND_PORT%/api/v1/hot-analysis/health
echo   - To stop   : double-click stop_hot_analysis.bat
if "%STARTED_BACKEND%"=="1" (
    echo   - Note     : We started the backend, stop script will kill it.
) else (
    echo   - Note     : Backend was already running (shared), stop script will NOT kill it.
)
echo ============================================================
echo.

REM Auto-exit (background run)
exit /b 0

:error
echo.
echo [ERROR] Startup failed. See messages above.
pause
exit /b 1
