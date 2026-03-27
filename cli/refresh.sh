#!/bin/bash
# FlipFrame daily refresh — run via cron (e.g. 5:30 AM)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
export PATH="/opt/homebrew/bin:$PATH"
export PYTHONPATH="/Users/mitchell/Library/Python/3.14/lib/python/site-packages:$PYTHONPATH"
python3 cli/flipframe.py push >> /tmp/flipframe-cron.log 2>&1
echo "--- $(date) ---" >> /tmp/flipframe-cron.log
