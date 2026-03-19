#!/bin/bash
# ═══════════════════════════════════════════
# Evermind — 一键启动 (One-Click Launcher)
# 双击此文件即可启动前端 + 后端
# ═══════════════════════════════════════════

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     🧠 Evermind 正在启动...               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Check Python ──
if ! command -v python3 &>/dev/null; then
    echo "❌ 未找到 Python3，请先安装 Python 3.10+"
    exit 1
fi

# ── Check Node.js ──
if ! command -v node &>/dev/null; then
    echo "❌ 未找到 Node.js，请先安装 Node.js 20+"
    exit 1
fi

# ── Kill zombie processes on our ports ──
echo "🧹 清理端口..."
lsof -ti :8765 | xargs kill -9 2>/dev/null || true
lsof -ti :3000 | xargs kill -9 2>/dev/null || true
sleep 0.5

# ── Install Backend Dependencies ──
echo "📦 检查后端依赖..."
cd "$SCRIPT_DIR/backend"
if [ ! -d "__pycache__" ] || ! python3 -c "import fastapi" 2>/dev/null; then
    echo "   安装 Python 依赖..."
    pip3 install -r requirements.txt -q
fi

# ── Install Frontend Dependencies ──
echo "📦 检查前端依赖..."
cd "$SCRIPT_DIR/frontend"
if [ ! -d "node_modules" ]; then
    echo "   安装 Node 依赖..."
    npm install
fi

# ── Start Backend ──
echo "🚀 启动后端 (port 8765)..."
cd "$SCRIPT_DIR/backend"
python3 server.py &
BACKEND_PID=$!

# Wait for backend to be ready
echo "   等待后端启动..."
for i in $(seq 1 30); do
    if curl -sf http://localhost:8765/api/health >/dev/null 2>&1; then
        echo "   ✅ 后端已启动"
        break
    fi
    sleep 1
done

# ── Start Frontend ──
echo "🚀 启动前端 (port 3000)..."
cd "$SCRIPT_DIR/frontend"
npx next dev &
FRONTEND_PID=$!

# Wait for frontend
sleep 3
echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  ✅ Evermind 已启动!                      ║"
echo "║                                          ║"
echo "║  🌐 前端编辑器: http://localhost:3000/editor ║"
echo "║  ⚙️  后端 API:  http://localhost:8765      ║"
echo "║  📡 WebSocket:  ws://localhost:8765/ws    ║"
echo "║                                          ║"
echo "║  按 Ctrl+C 停止所有服务                    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Open browser ──
if command -v open &>/dev/null; then
    open "http://localhost:3000/editor"
elif command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:3000/editor"
fi

# ── Wait and cleanup ──
cleanup() {
    echo ""
    echo "🛑 正在停止 Evermind..."
    kill $BACKEND_PID 2>/dev/null
    kill $FRONTEND_PID 2>/dev/null
    echo "✅ 已停止"
    exit 0
}
trap cleanup SIGINT SIGTERM
wait
