#!/usr/bin/env bash
# ==============================================================================
# AI Gateway Portal - Stop Script
# ==============================================================================

PORT=8080

echo "[*] Stopping AI Gateway Portal on port ${PORT}..."

# Kill process by port
fuser -k ${PORT}/tcp 2>/dev/null

# Kill uvicorn running this portal app
pkill -f "uvicorn app.main:app" 2>/dev/null

sleep 1
echo "[✓] AI Gateway Portal stopped successfully."
