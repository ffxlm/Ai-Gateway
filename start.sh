#!/usr/bin/env bash
# ==============================================================================
# AI Gateway Portal - Startup Script
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1
umask 000

PORT=8080

# Check virtualenv
if [ ! -d "venv" ]; then
    echo "[!] Virtualenv not found. Please run setup first."
    exit 1
fi

# Check if already running on port 8080
if lsof -Pi :${PORT} -sTCP:LISTEN -t >/dev/null 2>&1 || ss -tuln | grep -q ":${PORT} "; then
    echo "[*] AI Gateway Portal is already running on port ${PORT}."
else
    echo "[*] Starting AI Gateway Portal on http://0.0.0.0:${PORT} ..."
    nohup ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port ${PORT} > portal.log 2>&1 &
    sleep 2
fi

# Open in Browser if GUI available
if command -v xdg-open &> /dev/null; then
    nohup xdg-open "http://localhost:${PORT}" >/dev/null 2>&1 &
elif command -v google-chrome &> /dev/null; then
    nohup google-chrome "http://localhost:${PORT}" >/dev/null 2>&1 &
fi

echo "[✓] Portal ready: http://localhost:${PORT}"
echo "    - Dashboard: http://localhost:${PORT}/"
echo "    - Admin:     http://localhost:${PORT}/admin"
echo "    - API Base:  http://localhost:${PORT}/v1"
echo "    - Logs:      $SCRIPT_DIR/portal.log"
