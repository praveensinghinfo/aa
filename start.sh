#!/usr/bin/env bash
# Start the Live Translation & Transcription server
# Usage: ANTHROPIC_API_KEY=sk-... ./start.sh

set -e

if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "Error: ANTHROPIC_API_KEY environment variable is not set."
  echo "Usage: ANTHROPIC_API_KEY=sk-... ./start.sh"
  exit 1
fi

echo "Starting Live Translation & Transcription on http://0.0.0.0:8000"
echo "Open http://localhost:8000 in your browser."
echo ""
python app.py
