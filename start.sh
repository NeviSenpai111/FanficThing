#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "Starting Fanficthing on http://localhost:8000"
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
