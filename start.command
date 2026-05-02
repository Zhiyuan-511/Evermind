#!/bin/bash
# ═══════════════════════════════════════════
# Evermind — One-Click Launcher
# Double-click this file to start the frontend + backend.
# ═══════════════════════════════════════════

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     Evermind is starting...               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Check Python ──
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python3 not found. Please install Python 3.10+."
    exit 1
fi

# ── Check Node.js ──
if ! command -v node &>/dev/null; then
    echo "ERROR: Node.js not found. Please install Node.js 20+."
    exit 1
fi

# ── Kill zombie processes on our ports ──
echo "Cleaning ports..."
lsof -ti :8765 | xargs kill -9 2>/dev/null || true
lsof -ti :3000 | xargs kill -9 2>/dev/null || true
sleep 0.5

# ── Install Backend Dependencies ──
echo "Checking backend dependencies..."
cd "$SCRIPT_DIR/backend"
if [ ! -d "__pycache__" ] || ! python3 -c "import fastapi" 2>/dev/null; then
    echo "   Installing Python dependencies..."
    pip3 install -r requirements.txt -q
fi

# ── Install Frontend Dependencies ──
echo "Checking frontend dependencies..."
cd "$SCRIPT_DIR/frontend"
if [ ! -d "node_modules" ]; then
    echo "   Installing Node dependencies..."
    npm install
fi

# ── Start Backend ──
echo "Starting backend (port 8765)..."
cd "$SCRIPT_DIR/backend"
python3 server.py &
BACKEND_PID=$!

# Wait for backend to be ready
echo "   Waiting for backend..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8765/api/health >/dev/null 2>&1; then
        echo "   Backend is up."
        break
    fi
    sleep 1
done

# ── Start Frontend ──
echo "Starting frontend (port 3000)..."
cd "$SCRIPT_DIR/frontend"
npx next dev &
FRONTEND_PID=$!

# Wait for frontend
sleep 3
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  Evermind is running.                    ║"
echo "║                                          ║"
echo "║  Editor:    http://127.0.0.1:3000/editor ║"
echo "║  Backend:   http://127.0.0.1:8765        ║"
echo "║  WebSocket: ws://127.0.0.1:8765/ws       ║"
echo "║                                          ║"
echo "║  Press Ctrl+C to stop all services.      ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Open browser ──
if command -v open &>/dev/null; then
    open "http://127.0.0.1:3000/editor"
elif command -v xdg-open &>/dev/null; then
    xdg-open "http://127.0.0.1:3000/editor"
fi

# ── Wait and cleanup ──
cleanup() {
    echo ""
    echo "Stopping Evermind..."
    kill $BACKEND_PID 2>/dev/null
    kill $FRONTEND_PID 2>/dev/null
    echo "Stopped."
    exit 0
}
trap cleanup SIGINT SIGTERM
wait
