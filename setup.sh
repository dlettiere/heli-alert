#!/usr/bin/env bash
# Quick local setup without Docker
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "Creating .env from template..."
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo "Please edit $SCRIPT_DIR/.env with your settings."
fi

echo "Creating Python virtual environment..."
python3 -m venv "$SCRIPT_DIR/venv"
"$SCRIPT_DIR/venv/bin/pip" install -U pip -q
"$SCRIPT_DIR/venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt" -q

echo ""
echo "Setup complete! To run:"
echo "  $SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/heli_alert.py"
