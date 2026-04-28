#!/usr/bin/env bash
# Paperclip launcher — macOS.
#
# Double-click to start. Requires Docker Desktop installed and
# running. Node.js is NOT required on this Mac — the React bundle
# is built inside Docker.

set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    osascript -e 'display dialog "Docker Desktop is not installed.\n\n1. Download from https://www.docker.com/products/docker-desktop/\n2. Open Docker Desktop and wait for it to say Running.\n3. Double-click this file again." buttons {"OK"} default button 1' >/dev/null
    exit 1
fi

export PAPERCLIP_PORT=${PAPERCLIP_PORT:-8080}

echo "Starting Paperclip via Docker Compose..."
echo "(First run takes a few minutes to download images and build.)"
echo

docker compose up -d --build

echo
echo "Paperclip is running at http://localhost:${PAPERCLIP_PORT}/"
echo
echo "To stop it: open Terminal in this folder and run"
echo "    docker compose down"
echo

open "http://localhost:${PAPERCLIP_PORT}/"
