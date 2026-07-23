#!/bin/bash
# ============================================================
# start_telemetry.sh - Analyzer Agent Telemetry Server (macOS/Linux)
# Zero dependencies (uses Node built-in node:sqlite).
# Requires Node.js v22.5+ (v24 recommended).
# ============================================================

set -e
cd "$(dirname "$0")/telemetry-server"

echo "═══════════════════════════════════════════════"
echo "  Analyzer Agent Telemetry Server"
echo "═══════════════════════════════════════════════"
echo

# --- Check Node.js ---
if ! command -v node &>/dev/null; then
    echo "[ERROR] Node.js not found. Install v22.5+ from https://nodejs.org/"
    exit 1
fi
NODE_VER=$(node -v)
echo "[OK] Node.js: $NODE_VER"

# --- Check version >= 22 ---
NODE_MAJOR=$(echo "$NODE_VER" | sed 's/v\([0-9]*\)\..*/\1/')
if [ "$NODE_MAJOR" -lt 22 ]; then
    echo "[ERROR] Node.js $NODE_VER is too old. Need v22.5+ for built-in sqlite."
    echo "Download from https://nodejs.org/"
    exit 1
fi
echo

# --- Start server (no npm install needed - zero dependencies) ---
echo "Starting server on port 3000..."
echo
echo "  Dashboard: http://localhost:3000/"
echo "  Endpoint:  http://YOUR_IP:3000/api/usage"
echo "  Stats:     http://localhost:3000/api/stats"
echo
echo "  Press Ctrl+C to stop."
echo "═══════════════════════════════════════════════"
echo

# Open browser after 3s (background)
(sleep 3 && (open http://localhost:3000/ 2>/dev/null || xdg-open http://localhost:3000/ 2>/dev/null)) &

node server.js
