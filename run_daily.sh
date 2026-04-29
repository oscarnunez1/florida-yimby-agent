#!/bin/bash
# Daily pipeline wrapper — activates .venv and runs run_daily.py,
# logging all output to logs/daily_YYYY-MM-DD.log.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$DIR/logs/daily_$(date +%Y-%m-%d).log"

mkdir -p "$DIR/logs"

{
  echo "=== run_daily.sh started at $(date) ==="
  source "$DIR/.venv/bin/activate"
  cd "$DIR"
  python run_daily.py
  echo "=== run_daily.sh finished at $(date) ==="
} >> "$LOG_FILE" 2>&1
