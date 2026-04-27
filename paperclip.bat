@echo off
REM Paperclip launcher — Windows.
REM
REM Double-click to start the system. Requires Docker Desktop installed.
REM IT staff should run this once (or set it to run on login) so the
REM officer can just open http://localhost:8080.

setlocal
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
    echo.
    echo Docker is not installed or not on PATH. Install Docker Desktop
    echo from https://www.docker.com/products/docker-desktop/ and run
    echo this script again.
    echo.
    pause
    exit /b 1
)

REM Build the frontend bundle if it isn't there yet.
if not exist "frontend\dist\index.html" (
    where npm >nul 2>nul
    if errorlevel 1 (
        echo.
        echo The frontend has not been built and Node.js is not installed.
        echo Install Node.js from https://nodejs.org/ then re-run this
        echo script, OR ask IT to provide a pre-built copy under
        echo frontend\dist.
        echo.
        pause
        exit /b 1
    )
    echo Building Paperclip frontend (one-time)...
    pushd frontend
    call npm install
    call npm run build
    popd
)

set "PAPERCLIP_PORT=8080"

echo Starting Paperclip via Docker Compose...
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo Could not start Paperclip. See output above.
    pause
    exit /b 1
)

echo.
echo Paperclip is running at http://localhost:%PAPERCLIP_PORT%/
echo (To stop it later, run "docker compose down" from this folder.)
echo.

REM Open the user's default browser at the app.
start "" "http://localhost:%PAPERCLIP_PORT%/"

endlocal
