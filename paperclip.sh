#!/usr/bin/env bash
# Paperclip launcher — Linux.
#
# Used by paperclip.desktop. Can also be double-clicked from the
# file manager if your desktop is configured to run shell scripts.

set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    notify-send "Paperclip" "Docker is not installed." 2>/dev/null || true
    echo "Docker is not installed or not on PATH." >&2
    echo "Install Docker (https://docs.docker.com/engine/install/) and re-run." >&2
    exit 1
fi

if [ ! -f "frontend/dist/index.html" ]; then
    if ! command -v npm >/dev/null 2>&1; then
        echo "The frontend has not been built and Node.js is not installed." >&2
        echo "Install Node.js, then re-run this script." >&2
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

if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:${PAPERCLIP_PORT}/" >/dev/null 2>&1 || true
fi
