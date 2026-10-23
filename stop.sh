#!/bin/bash
# ==============================================================================
# WhatsApp AI Companion - Stop Script
# Stops FastAPI backend (port 8000) and Node.js Baileys Bridge (port 3001)
# ==============================================================================

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "🛑 Stopping WhatsApp AI Companion services..."

# Kill by port
PIDS_8000=$(lsof -ti :8000 2>/dev/null || true)
PIDS_3001=$(lsof -ti :3001 2>/dev/null || true)

if [ -n "$PIDS_8000" ]; then
    echo "Stopping FastAPI on port 8000 (PIDs: $PIDS_8000)..."
    kill -9 $PIDS_8000 2>/dev/null || true
fi

if [ -n "$PIDS_3001" ]; then
    echo "Stopping Bridge on port 3001 (PIDs: $PIDS_3001)..."
    kill -9 $PIDS_3001 2>/dev/null || true
fi

# Also kill any leftover uvicorn, node bridge/server.js, or cloudflared processes
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "node bridge/server.js" 2>/dev/null || true
pkill -f "cloudflared" 2>/dev/null || true

sleep 1
echo "✅ All WhatsApp Companion services stopped successfully."
