#!/bin/bash
set -e

# Clean up any stale Xvfb lock from a previous run
rm -f /tmp/.X99-lock

echo "Starting Xvfb virtual display..."
Xvfb :99 -screen 0 1280x800x24 &
export DISPLAY=:99
sleep 2

echo "Starting Ticket Tracker..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info
