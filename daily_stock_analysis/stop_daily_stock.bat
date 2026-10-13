@echo off
REM ============================================================
REM  Daily Stock Analysis - Stop Script
REM  Double-click to stop the FastAPI backend (and any
REM  related python/conda wrapper processes) running on port 8000.
REM ============================================================
REM
REM The backend is launched via:
REM   cmd.exe /c "conda.bat run -n daily_stock_analysis python main.py --serve-only --port 8000"
REM This produces a process tree:
REM   cmd.exe (wrapper) -> conda.exe -> python.exe (uvicorn, listens on :8000)
REM We use `taskkill /F /T` to kill the entire tree from the python.exe root.
REM ============================================================

setlocal EnableExtensions

set "WEBUI_PORT=8000"

echo.
echo ============================================================
echo   Daily Stock Analysis - Stop Script
echo   Target port : %WEBUI_PORT%
echo ============================================================
echo.

REM --- 1. Find and kill processes listening on the port (with tree) -------
echo [1/4] Looking for processes listening on port %WEBUI_PORT% ...
set "FOUND_PIDS="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%WEBUI_PORT% " ^| findstr /R /C:"LISTENING"') do (
    if not "%%P"=="" (
        echo       Found PID %%P, killing process tree ...
        REM /T kills the process and all its descendants (conda wrapper, cmd wrapper, etc.)
        taskkill /F /T /PID %%P >nul 2>&1
    )
)

REM --- 2. Fallback: kill python main.py processes (with tree) ---------
echo [2/4] Cleaning up orphaned python main.py processes ...
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*main.py*' -or $_.CommandLine -like '*--serve-only*' }; foreach ($p in $procs) { Write-Output ('Kill PID ' + $p.ProcessId + ' (tree)'); $null = cmd /c 'taskkill /F /T /PID ' + $p.ProcessId + ' 2^>nul' }" 2>nul

REM --- 3. Fallback: kill any conda run wrappers still around -------------
echo [3/4] Cleaning up lingering conda run / cmd wrappers ...
powershell -NoProfile -Command "$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'cmd.exe' -and $_.CommandLine -like '*conda*run*') -or ($_.Name -eq 'conda.exe') }; foreach ($p in $procs) { Write-Output ('Kill PID ' + $p.ProcessId + ' (' + $p.Name + ')'); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }" 2>nul

REM --- 4. Verify port is released -----------------------------------------
echo [4/4] Verifying port %WEBUI_PORT% is released ...
timeout /t 1 /nobreak >nul
netstat -ano | findstr /R /C:":%WEBUI_PORT% " | findstr /R /C:"LISTENING" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ============================================================
    echo   Service stopped. Port %WEBUI_PORT% is free.
    echo ============================================================
    echo.
    endlocal
    exit /b 0
) else (
    echo.
    echo [WARN] Port %WEBUI_PORT% is still occupied. Check manually.
    echo.
    endlocal
    pause
    exit /b 1
)