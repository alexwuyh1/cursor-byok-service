#!/usr/bin/env bash
set -euo pipefail

# Cursor BYOK Service — installer
# Checks dependencies, sets up launchd, starts service.
# Configuration is done via the admin console after install.
# See README.md for Cloudflare and Cursor setup instructions.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.cursor.byok.service"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
CFG_PATH="$SCRIPT_DIR/config.json"

# --- detect python3 (need 3.10+) ---
PYTHON_BIN=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$p" &>/dev/null; then
        ver=$("$p" --version 2>&1 | awk '{print $2}')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "${major:-0}" -ge 3 ] && [ "${minor:-0}" -ge 10 ]; then
            PYTHON_BIN="$p"
            break
        fi
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "[ERROR] Python 3.10+ not found."
    echo "  Install with: brew install python@3.14"
    exit 1
fi
echo "[OK] Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# --- detect cloudflared ---
CLOUDFLARED_BIN=""
for c in /opt/homebrew/bin/cloudflared /usr/local/bin/cloudflared cloudflared; do
    if command -v "$c" &>/dev/null || [ -x "$c" ]; then
        CLOUDFLARED_BIN="$c"
        break
    fi
done
if [ -z "$CLOUDFLARED_BIN" ]; then
    echo "[ERROR] cloudflared not found."
    echo "  Install with: brew install cloudflared"
    exit 1
fi
echo "[OK] cloudflared: $CLOUDFLARED_BIN ($("$CLOUDFLARED_BIN" --version 2>&1 | head -1))"

# --- determine port ---
DEFAULT_PORT=8787
if [ -f "$CFG_PATH" ]; then
    EXISTING_PORT=$("$PYTHON_BIN" -c "import json;print(json.load(open('$CFG_PATH')).get('listen_port',''))" 2>/dev/null || echo "")
    if [ -n "$EXISTING_PORT" ]; then
        DEFAULT_PORT="$EXISTING_PORT"
    fi
fi
echo ""
read -rp "Listen port [$DEFAULT_PORT]: " LISTEN_PORT
LISTEN_PORT="${LISTEN_PORT:-$DEFAULT_PORT}"

# --- write/update config.json (port only, preserve existing fields) ---
if [ ! -f "$CFG_PATH" ]; then
    cat > "$CFG_PATH" << JSONEOF
{
  "listen_port": $LISTEN_PORT
}
JSONEOF
    echo "[OK] config.json created (port=$LISTEN_PORT)"
elif [ "$LISTEN_PORT" != "$DEFAULT_PORT" ]; then
    "$PYTHON_BIN" -c "
import json
with open('$CFG_PATH') as f:
    cfg = json.load(f)
cfg['listen_port'] = $LISTEN_PORT
with open('$CFG_PATH','w') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write('\n')
"
    echo "[OK] config.json updated (port=$LISTEN_PORT)"
else
    echo "[OK] config.json exists (port=$LISTEN_PORT)"
fi

# --- generate launchd plist ---
cat > "$PLIST_PATH" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>$SCRIPT_DIR/cursor-byok-service.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>ProcessType</key>
    <string>Background</string>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/service.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/service.log</string>
</dict>
</plist>
PLISTEOF
echo "[OK] launchd plist written to $PLIST_PATH"

# --- reload service (unload first frees the port for existing installs) ---
launchctl unload "$PLIST_PATH" 2>/dev/null || true
sleep 1

# Check if port is still in use by a non-cursor-byok process
if command -v lsof &>/dev/null; then
    if lsof -i ":$LISTEN_PORT" -sTCP:LISTEN -P -n >/dev/null 2>&1; then
        echo "[WARN] Port $LISTEN_PORT is in use by another process."
        echo "  Service may fail to start. Check service.log for errors."
    fi
fi

launchctl load "$PLIST_PATH"
echo "[OK] Service loaded"

# --- wait for service to come up (poll, not hardcoded sleep) ---
echo ""
echo "[INFO] Waiting for service..."
for _ in $(seq 1 15); do
    if curl -s -m 2 "http://127.0.0.1:$LISTEN_PORT/admin" >/dev/null 2>&1; then
        echo "[OK] Service is up"
        break
    fi
    sleep 1
done

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Admin console:  http://127.0.0.1:$LISTEN_PORT/admin"
echo ""
echo "Next steps:"
echo "  1. Open the admin console to configure API key, models, and tunnel"
echo "  2. Read README.md for Cloudflare and Cursor setup instructions"
echo ""
echo "Manage service:"
echo "  launchctl list | grep $PLIST_LABEL   # check status"
echo "  launchctl unload $PLIST_PATH          # stop"
echo "  launchctl load $PLIST_PATH            # start"
