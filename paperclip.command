#!/usr/bin/env bash
# Paperclip launcher — macOS.
#
# Double-click to start the system. Requires Docker Desktop installed.

set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    osascript -e 'display dialog "Docker is not installed. Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and try again." buttons {"OK"} default button 1' >/dev/null
    exit 1
fi

if [ ! -f "frontend/dist/index.html" ]; then
    if ! command -v npm >/dev/null 2>&1; then
        osascript -e 'display dialog "The Paperclip frontend has not been built and Node.js is not installed. Install Node.js from https://nodejs.org/ and re-run, or ask IT for a pre-built copy under frontend/dist." buttons {"OK"} default button 1' >/dev/null
        exit 1
    fi
    echo "Building Paperclip frontend (one-time)..."
    (cd frontend && npm install && npm run build)
fi

export PAPERCLIP_PORT=${PAPERCLIP_PORT:-8080}

echo "Starting Paperclip via Docker Compose..."
docker compose up -d --build

echo
echo "Paperclip is running at http://localhost:${PAPERCLIP_PORT}/"
echo "Stop it later with: docker compose down"
echo

open "http://localhost:${PAPERCLIP_PORT}/"
