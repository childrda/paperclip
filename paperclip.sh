#!/usr/bin/env bash
# Paperclip launcher — Linux.
#
# Used by paperclip.desktop. Can also be invoked directly from a
# terminal: ./paperclip.sh
#
# Requires Docker installed and running. Node.js is NOT required —
# the React bundle is built inside Docker.

set -e
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "Docker is not installed or not on PATH." >&2
    echo "Install Docker:  https://docs.docker.com/engine/install/" >&2
    echo "Then re-run this script." >&2
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
echo "To stop it: run 'docker compose down' from this folder."
echo

if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:${PAPERCLIP_PORT}/" >/dev/null 2>&1 || true
fi
