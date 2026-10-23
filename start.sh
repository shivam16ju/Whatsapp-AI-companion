#!/bin/bash
# ==============================================================================
# WhatsApp AI Companion ("Baby I Need Space") - One-Click Start Script
# Starts Python FastAPI backend (port 8000) and Node.js Baileys Bridge (port 3001)
# ==============================================================================

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

mkdir -p logs media/generated

echo "=================================================="
echo "🚀 Starting WhatsApp AI Companion..."
echo "=================================================="

# 1. Clean up existing processes on ports 8000 and 3001
echo "[1/4] Checking and freeing ports 8000 & 3001..."
lsof -ti :8000 | xargs kill -9 2>/dev/null || true
lsof -ti :3001 | xargs kill -9 2>/dev/null || true
sleep 1

# 2. Check virtualenv & dependencies
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

if [ ! -d "bridge/node_modules" ]; then
    echo "Installing bridge node dependencies..."
    (cd bridge && npm install)
fi

# 3. Start Python FastAPI backend in background
echo "[2/4] Starting FastAPI backend on port 8000..."
nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 < /dev/null > logs/backend.log 2>&1 &
BACKEND_PID=$!
disown $BACKEND_PID 2>/dev/null || true
echo "      Backend PID: $BACKEND_PID (logs: logs/backend.log)"

# 4. Start Node.js Baileys bridge in background
echo "[3/4] Starting WhatsApp Web Bridge on port 3001..."
nohup node bridge/server.js < /dev/null > logs/bridge.log 2>&1 &
BRIDGE_PID=$!
disown $BRIDGE_PID 2>/dev/null || true
echo "      Bridge PID: $BRIDGE_PID (logs: logs/bridge.log)"

# 5. Wait for initialization & check health
echo "[4/4] Verifying health..."
sleep 3

BACKEND_OK=false
BRIDGE_OK=false

for i in {1..6}; do
    if curl -s http://127.0.0.1:8000/health | grep -q "ok"; then
        BACKEND_OK=true
        break
    fi
    sleep 1
done

for i in {1..6}; do
    if curl -s http://127.0.0.1:3001/status | grep -q "status"; then
        BRIDGE_OK=true
        break
    fi
    sleep 1
done

PUBLIC_URL=""
if [ "$1" == "--public" ] || [ "$1" == "--share" ]; then
    echo "🌐 Starting Public Tunnel for remote sharing..."
    pkill -f "cloudflared" 2>/dev/null || true
    nohup ./bin/cloudflared tunnel --url http://127.0.0.1:8000 < /dev/null > logs/tunnel.log 2>&1 &
    TUNNEL_PID=$!
    disown $TUNNEL_PID 2>/dev/null || true
    echo "      Tunnel PID: $TUNNEL_PID (logs: logs/tunnel.log)"
    for i in {1..10}; do
        PUBLIC_URL=$(grep -a -o 'https://[-a-zA-Z0-9@:%._\+~#=]*\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | head -n 1 || true)
        if [ -n "$PUBLIC_URL" ]; then
            break
        fi
        sleep 1
    done
fi

echo ""
echo "=================================================="
if [ "$BACKEND_OK" = true ] && [ "$BRIDGE_OK" = true ]; then
    echo "✅ WhatsApp Companion is RUNNING!"
    echo "--------------------------------------------------"
    echo "📱 Local Dashboard:  http://localhost:8000 (Token: shiavm@1)"
    if [ -n "$PUBLIC_URL" ]; then
        echo "🌍 Public Share URL:  $PUBLIC_URL (Token: shiavm@1)"
        echo "   👉 Share this link with your friend on any network!"
    else
        echo "💡 To share with a friend on another network, run: ./share.sh"
    fi
    echo "🔗 WhatsApp Status:   http://localhost:3001/status"
    echo "📷 Link WhatsApp QR:  http://localhost:8000/qr"
    echo ""
    echo "📋 Logs:"
    echo "   Backend: tail -f logs/backend.log"
    echo "   Bridge:  tail -f logs/bridge.log"
    [ -n "$PUBLIC_URL" ] && echo "   Tunnel:  tail -f logs/tunnel.log"
    echo ""
    echo "🛑 To stop: ./stop.sh"
else
    echo "⚠️ Started with warnings. Check logs for details:"
    echo "   Backend log: cat logs/backend.log"
    echo "   Bridge log:  cat logs/bridge.log"
fi
echo "=================================================="
