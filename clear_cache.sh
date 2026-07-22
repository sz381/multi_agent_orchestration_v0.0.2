#!/bin/bash
set -euo pipefail

echo "Clearing .pyc files..."
find . -name "*.pyc" -delete

echo "Clearing __pycache__ directories..."
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null

pyc_count=$(find . -name "*.pyc" | wc -l | tr -d ' ')
pycache_count=$(find . -type d -name "__pycache__" | wc -l | tr -d ' ')

if [ "$pyc_count" -eq 0 ] && [ "$pycache_count" -eq 0 ]; then
    echo "Done. Cache cleared."
else
    echo "Warning: $pyc_count .pyc file(s) and $pycache_count __pycache__ dir(s) remain."
fi
