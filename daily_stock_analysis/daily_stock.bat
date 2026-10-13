@echo off
REM ============================================================
REM  Daily Stock Analysis - Startup Script
REM  Double-click to launch FastAPI backend in background
REM  and auto-open the Web UI in your browser.
REM ============================================================
REM
REM Why this script does NOT use `conda activate`:
REM   `conda activate` requires shell hooks (CONDA_EXE, CONDA_PREFIX, ...)
REM   that are loaded only by `conda init cmd.exe` into interactive
REM   cmd sessions. A .bat double-clicked from Windows Explorer runs
REM   in a fresh cmd.exe that DOES NOT have those hooks, so
REM   `call conda activate <env>` silently no-ops.
REM   We use `conda run -n <env> python ...` instead, which runs
REM   Python in the named env without needing shell activation.
REM   (This is conda's documented standard for batch/CI scripts.)
REM ============================================================

cd /d "%~dp0"

set "ROOT_DIR=%CD%"
set "WEBUI_PORT=8000"
set "CONDA_ENV=daily_stock_analysis"

echo.
echo ============================================================
echo   Daily Stock Analysis - Background Launcher
echo   Working dir : %ROOT_DIR%
echo   Web port    : %WEBUI_PORT%
echo   Conda env   : %CONDA_ENV%
echo ============================================================
echo.

REM --- 1. Locate conda.bat --------------------------------------------
echo [1/5] Locating conda ...
set "CONDA_BAT="

REM Try `where conda` first (works if conda init cmd.exe was run)
where conda >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%C in ('where conda') do (
        if not defined CONDA_BAT set "CONDA_BAT=%%C"
    )
)

REM Fall back to well-known install locations
if not defined CONDA_BAT if exist "C:\ProgramData\miniforge3\condabin\conda.bat"   set "CONDA_BAT=C:\ProgramData\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\miniforge3\condabin\conda.bat"    set "CONDA_BAT=%USERPROFILE%\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "D:\miniforge3\condabin\conda.bat"                set "CONDA_BAT=D:\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "C:\miniforge3\condabin\conda.bat"                set "CONDA_BAT=C:\miniforge3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\anaconda3\condabin\conda.bat"      set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\AppData\Local\Continuum\anaconda3\condabin\conda.bat" set "CONDA_BAT=%USERPROFILE%\AppData\Local\Continuum\anaconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "D:\anaconda3\condabin\conda.bat"                 set "CONDA_BAT=D:\anaconda3\condabin\conda.bat"

if not defined CONDA_BAT (
    echo [ERROR] conda.bat not found. Please either:
    echo   - Run "conda init cmd.exe" once in cmd, OR
    echo   - Install Miniforge/Anaconda and adjust the paths above.
    goto :error
)
echo       Using: %CONDA_BAT%

REM --- 2. Verify the conda env exists -------------------------------------
echo [2/5] Verifying conda env '%CONDA_ENV%' ...
"%CONDA_BAT%" env list | findstr /R /C:"%CONDA_ENV% " >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Conda env '%CONDA_ENV%' not found.
    echo         Available envs:
    "%CONDA_BAT%" env list
    goto :error
)
echo       OK.

REM --- 3. Build frontend --------------------------------------------------
echo [3/5] Building frontend ...
cd /d "%ROOT_DIR%\apps\dsa-web"
if errorlevel 1 (
    echo [ERROR] Cannot enter apps\dsa-web directory.
    goto :error
)

if not exist "node_modules" (
    echo [3/5] node_modules not found, running npm install - slow on first run ...
    call npm install
    if errorlevel 1 goto :error
) else (
    echo [3/5] node_modules exists, skipping npm install.
)

echo [3/5] Running npm run build ...
call npm run build
if errorlevel 1 goto :error

REM --- 4. Start backend in hidden window -------------------------------
echo [4/5] Starting backend in a hidden window (conda run -n %CONDA_ENV%) ...
cd /d "%ROOT_DIR%"

REM If port is already in use, kill the old process first
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort %WEBUI_PORT% -State Listen -ErrorAction SilentlyContinue; if ($c) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 1 }" >nul 2>&1

REM Launch via `conda run` (no shell activation required) in a hidden window.
REM Inner cmd /c wraps the conda command so it runs detached.
powershell -NoProfile -Command "Start-Process -FilePath 'cmd.exe' -ArgumentList '/c','\"%CONDA_BAT%\" run -n %CONDA_ENV% --no-capture-output python main.py --serve-only --port %WEBUI_PORT%' -WorkingDirectory '%ROOT_DIR%' -WindowStyle Hidden" >nul 2>&1

if errorlevel 1 (
    echo [ERROR] Failed to start backend process.
    goto :error
)

REM --- 5. Wait for server to be ready + open browser -------------------
echo [5/5] Waiting for server (http://localhost:%WEBUI_PORT%/) ...
set /a attempts=0
:wait_loop
set /a attempts=%attempts%+1
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -Uri 'http://localhost:%WEBUI_PORT%/' -UseBasicParsing -TimeoutSec 3 -Method Head; if ($r.StatusCode -ge 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :ready
if %attempts% GEQ 20 (
    echo [WARN] Server not ready within 60s, opening browser anyway.
    goto :open_browser
)
timeout /t 3 /nobreak >nul
goto :wait_loop

:ready
echo [5/5] Server is ready.

:open_browser
start "" "http://localhost:%WEBUI_PORT%/"

echo.
echo ============================================================
echo   Startup complete
echo   Web UI : http://localhost:%WEBUI_PORT%/
echo   To stop: double-click stop_daily_stock.bat
echo ============================================================
echo.

REM Auto-exit (background run)
exit /b 0

:error
echo.
echo [ERROR] Startup failed. See messages above.
pause
exit /b 1