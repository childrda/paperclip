@echo off
REM Paperclip launcher — Windows.
REM
REM Double-click to start. Requires Docker Desktop installed and
REM running. Node.js is NOT required on this machine — the React
REM bundle is built inside Docker.

setlocal
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
    echo.
    echo Docker is not installed or not on PATH.
    echo.
    echo 1. Install Docker Desktop from
    echo    https://www.docker.com/products/docker-desktop/
    echo 2. Start Docker Desktop and wait for "Engine running".
    echo 3. Double-click this file again.
    echo.
    pause
    exit /b 1
)

if "%PAPERCLIP_PORT%"=="" set "PAPERCLIP_PORT=8080"

echo Starting Paperclip via Docker Compose...
echo (First run takes a few minutes to download images and build.)
echo.

docker compose up -d --build
if errorlevel 1 (
    echo.
    echo Could not start Paperclip. See output above.
    pause
    exit /b 1
)

echo.
echo Paperclip is running at http://localhost:%PAPERCLIP_PORT%/
echo.
echo To stop it: open a Command Prompt in this folder and run
echo     docker compose down
echo.

start "" "http://localhost:%PAPERCLIP_PORT%/"

endlocal
