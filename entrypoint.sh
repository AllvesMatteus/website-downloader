#!/bin/bash
set -e

# Default to 7860 (Hugging Face Spaces) if PORT is not set
export PORT="${PORT:-7860}"

echo "Starting gunicorn on port $PORT..."

# Start gunicorn with dynamic port
# Hugging Face uses 7860, Railway/Render use dynamic $PORT
exec gunicorn app:app \
    --bind "0.0.0.0:$PORT" \
    --workers 1 \
    --threads 2 \
    --timeout 300 \
    --worker-class gthread \
    --access-logfile - \
    --error-logfile -
