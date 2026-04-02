#!/bin/bash
# Easyfast Dashboard — локальный просмотр
# Запускает мини-сервер и открывает дашборд в браузере

PORT=8420
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 Serving dashboard at http://localhost:$PORT/easyfast_dashboard.html"
echo "   Press Ctrl+C to stop"
echo ""

# Open in browser (macOS)
open "http://localhost:$PORT/easyfast_dashboard.html" 2>/dev/null

# Start server
cd "$DIR" && python3 -m http.server $PORT
