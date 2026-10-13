@echo off
REM Simplified stop script - minimal commands
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "FRONTEND_PORT=5173"
set "BACKEND_PORT=8000"
set "BACKEND_MARKER=%CD%\.run_state\backend.pid"

echo.
echo === Hot-Analysis Stop Script ===
echo.

REM --- Step 1: Kill Vite (ALWAYS) ---
echo [1] Killing Vite on port %FRONTEND_PORT% ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%FRONTEND_PORT% "') do (
    if not "%%P"=="" (
        echo     Killing PID %%P ...
        taskkill /F /PID %%P
    )
)
echo     Done.
echo.

REM --- Step 2: Check ownership ---
echo [2] Backend ownership check ...
set "OWN_BACKEND=0"
if exist "%BACKEND_MARKER%" (
    echo     Marker exists - we own the backend.
    set "OWN_BACKEND=1"
) else (
    echo     No marker - we don't own the backend.
)
echo.

REM --- Step 3: Kill backend if owned ---
echo [3] Backend action (OWN_BACKEND=!OWN_BACKEND!) ...
if "!OWN_BACKEND!"=="1" (
    echo     Killing backend on port %BACKEND_PORT% ...
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%BACKEND_PORT% "') do (
        if not "%%P"=="" (
            echo     Killing PID %%P ...
            taskkill /F /PID %%P
        )
    )
    del "%BACKEND_MARKER%" 2>nul
    echo     Done.
) else (
    echo     Skipping backend.
)
echo.

echo === Stop complete ===
endlocal
