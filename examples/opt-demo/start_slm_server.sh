#!/bin/bash
# Start the SLM inference server (continuous process, MPS GPU on Apple Silicon).
# Run this once before starting chat_ui.py when AXIS3_SLM_BACKEND=server.
#
# Usage:
#   ./start_slm_server.sh          # foreground (Ctrl-C to stop)
#   ./start_slm_server.sh &        # background

set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "[slm_server] ERROR: venv not found. Run: python -m venv venv && pip install -r requirements.txt"
    exit 1
fi

source venv/bin/activate

PORT="${AXIS3_SLM_SERVER_PORT:-8001}"
echo "[slm_server] Starting on port $PORT (device: MPS if available, else CPU)"
echo "[slm_server] Models loading at startup — wait for 'All models ready' before sending requests"

exec uvicorn slm_server:app --host 0.0.0.0 --port "$PORT" --workers 1
