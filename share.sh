#!/bin/bash
# ==============================================================================
# WhatsApp AI Companion - Remote Share Script
# Exposes the companion dashboard & WhatsApp linking to the internet via Cloudflare
# ==============================================================================

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

mkdir -p logs

echo "=================================================="
echo "🌍 Starting Remote Public Share Link..."
echo "=================================================="

# Check if backend is running
if ! curl -s http://127.0.0.1:8000/health | grep -q "ok"; then
    echo "⚠️ Backend is not running on port 8000. Starting services first..."
    ./start.sh
fi

echo "Connecting secure tunnel to http://127.0.0.1:8000..."
pkill -f "cloudflared" 2>/dev/null || true
nohup ./bin/cloudflared tunnel --url http://127.0.0.1:8000 < /dev/null > logs/tunnel.log 2>&1 &
TUNNEL_PID=$!

echo "Waiting for tunnel to be assigned..."
PUBLIC_URL=""
for i in {1..15}; do
    PUBLIC_URL=$(grep -a -o 'https://[-a-zA-Z0-9@:%._\+~#=]*\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | head -n 1 || true)
    if [ -n "$PUBLIC_URL" ]; then
        break
    fi
    sleep 1
done

if [ -n "$PUBLIC_URL" ]; then
    echo ""
    echo "=================================================="
    echo "🎉 SUCCESS! Your companion is available online:"
    echo "--------------------------------------------------"
    echo "🌐 Public URL:   $PUBLIC_URL"
    echo "🔑 Access Token: shiavm@1"
    echo "--------------------------------------------------"
    echo "Send this URL and Token to your friend!"
    echo "They can open it on their phone, laptop, or any browser."
    echo ""
    echo "🛑 To stop sharing: ./stop.sh"
    echo "=================================================="
else
    echo "❌ Failed to obtain tunnel URL. Check logs/tunnel.log:"
    tail -n 20 logs/tunnel.log
fi
