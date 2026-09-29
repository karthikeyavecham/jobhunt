#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Kill any existing JobHunt server
pkill -f "run\.py" 2>/dev/null || true
sleep 1

# Create venv if it doesn't exist
if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Install dependencies (fast if up to date)
./venv/bin/pip install -q -r requirements.txt 2>/dev/null || true

echo "Starting JobHunt on http://127.0.0.1:5002"
./venv/bin/python run.py
