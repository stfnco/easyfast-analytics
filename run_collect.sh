#!/bin/bash
# Easyfast Analytics — автозапуск
# Этот файл запускается каждый день в 7:00 утра

PYTHON=$(which python3 2>/dev/null || echo "/usr/bin/python3")
SCRIPT="/Users/nick/Documents/Claude/Template Analytics/collect.py"
LOG="/Users/nick/Documents/Claude/Template Analytics/collect.log"

echo "── $(date '+%Y-%m-%d %H:%M:%S') ──" >> "$LOG"
"$PYTHON" "$SCRIPT" >> "$LOG" 2>&1
echo "" >> "$LOG"
