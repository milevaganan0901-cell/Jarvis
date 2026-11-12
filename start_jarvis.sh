#!/bin/zsh

# J.A.R.V.I.S. local startup
# Starts Ollama (only if needed), the Daniel voice server, and the
# Spacebar-controlled assistant with its dashboard command bridge.

set -u

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROJECT="$HOME/jarvis-local"
PYTHON="$PROJECT/.venv-voice/bin/python"
LOG_DIR="$HOME/Library/Logs"

OLLAMA_URL="http://127.0.0.1:11434"
VOICE_HOST="127.0.0.1"
VOICE_PORT="8765"

VOICE_PID=""
ASSISTANT_PID=""

mkdir -p "$LOG_DIR"
cd "$PROJECT" || {
  echo "JARVIS project folder not found: $PROJECT"
  exit 1
}

if [[ ! -x "$PYTHON" ]]; then
  echo "JARVIS Python environment not found: $PYTHON"
  exit 1
fi

cleanup() {
  echo "Stopping JARVIS background services..."
  if [[ -n "$ASSISTANT_PID" ]]; then
    kill "$ASSISTANT_PID" 2>/dev/null || true
  fi
  if [[ -n "$VOICE_PID" ]]; then
    kill "$VOICE_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

# Start Ollama only when its local API is not already responding.
if ! /usr/bin/curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  OLLAMA_BIN="$(command -v ollama || true)"
  if [[ -z "$OLLAMA_BIN" ]]; then
    echo "Ollama command not found. Check that Ollama is installed."
    exit 1
  fi

  echo "Starting Ollama..."
  "$OLLAMA_BIN" serve >> "$LOG_DIR/jarvis-ollama.log" 2>&1 &

  OLLAMA_READY=0
  for i in {1..30}; do
    if /usr/bin/curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
      OLLAMA_READY=1
      break
    fi
    sleep 1
  done

  if [[ "$OLLAMA_READY" -ne 1 ]]; then
    echo "Ollama did not start. See $LOG_DIR/jarvis-ollama.log"
    exit 1
  fi
fi

# Start the existing voice server.
echo "Starting JARVIS voice server..."
"$PYTHON" "$PROJECT/voice_server.py" >> "$LOG_DIR/jarvis-voice.log" 2>&1 &
VOICE_PID=$!

# Wait for the voice server to become available.
VOICE_READY=0
for i in {1..20}; do
  if /usr/bin/nc -z "$VOICE_HOST" "$VOICE_PORT" >/dev/null 2>&1; then
    VOICE_READY=1
    break
  fi
  sleep 1
done

if [[ "$VOICE_READY" -ne 1 ]]; then
  echo "Voice server did not become ready. See $LOG_DIR/jarvis-voice.log"
  exit 1
fi

# Start the Spacebar assistant and its localhost dashboard bridge.
echo "Starting JARVIS assistant and dashboard bridge..."
"$PYTHON" "$PROJECT/jarvis_assistant_dashboard_bridge.py" \
  >> "$LOG_DIR/jarvis-assistant.log" 2>&1 &
ASSISTANT_PID=$!

# Keep this LaunchAgent alive while the assistant is running.
wait "$ASSISTANT_PID"
