#!/usr/bin/env bash
set -euo pipefail

# Cursor BYOK Service — installer
# Checks/installs dependencies (Homebrew), sets up launchd, starts service.
# Everything else (Cloudflare login, tunnel, models, API keys) is configured
# via the admin console after install.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_LABEL="com.cursor.byok.service"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
CFG_PATH="$SCRIPT_DIR/config.json"

# Resolve the absolute path of a binary, preferring Homebrew prefixes so we
# don't depend on the current shell's PATH (launchd runs with a minimal PATH).
resolve_bin() {
    for p in /opt/homebrew/bin/"$1" /usr/local/bin/"$1" /usr/bin/"$1" /bin/"$1"; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    local found; found=$(command -v "$1" 2>/dev/null || true)
    [ -n "$found" ] && { echo "$found"; return 0; }
    return 1
}

# Find a Python 3.10+ binary among the Homebrew prefixes.
find_python_310() {
    for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        [ -x "$p" ] || continue
        local ver major rest minor
        ver=$("$p" --version 2>&1 | awk '{print $2}')
        major=${ver%%.*}; rest=${ver#*.}; minor=${rest%%.*}
        if [ "${major:-0}" -eq 3 ] && [ "${minor:-0}" -ge 10 ]; then
            echo "$p"; return 0
        fi
    done
    return 1
}

# Confirm with the user, then brew install a formula. On failure (non-zero
# exit, e.g. network/timeout), print the captured reason and the manual
# command, then exit — we never leave the user without a next step.
try_brew_install() {
    local formula="$1" manual="$2"
    echo "[INFO] Missing dependency: $formula"
    read -rp "Install $formula via Homebrew now? [Y/n] " ans || ans=""
    case "${ans:-Y}" in
        Y|y) ;;
        *) echo "[ABORT] Declined. Install manually: $manual"; exit 1 ;;
    esac
    echo "[INFO] Running: brew install $formula"
    local errfile; errfile="${TMPDIR:-/tmp}/.byok-brew-$$.err"
    if ! brew install "$formula" 2>"$errfile"; then
        echo "[ERROR] brew install $formula failed."
        echo "  reason: $(head -3 "$errfile" 2>/dev/null | tr -d '\r')"
        rm -f "$errfile"
        echo "  Please install manually: $manual"
        exit 1
    fi
    rm -f "$errfile"
    echo "[OK] $formula installed"
}

# --- Homebrew is required to auto-install dependencies ---
if ! command -v brew &>/dev/null; then
    echo "[ERROR] Homebrew not found. This installer uses it for dependencies."
    echo "  Install Homebrew first: https://brew.sh"
    exit 1
fi

# --- cloudflared ---
CLOUDFLARED_BIN="$(resolve_bin cloudflared || true)"
if [ -z "$CLOUDFLARED_BIN" ]; then
    try_brew_install cloudflared "brew install cloudflared"
    CLOUDFLARED_BIN="$(resolve_bin cloudflared || true)"
fi
if [ -z "$CLOUDFLARED_BIN" ]; then
    echo "[ERROR] cloudflared still not found after install attempt."
    exit 1
fi
echo "[OK] cloudflared: $CLOUDFLARED_BIN ($("$CLOUDFLARED_BIN" --version 2>&1 | head -1))"

# --- python 3.10+ ---
PYTHON_BIN="$(find_python_310 || true)"
if [ -z "$PYTHON_BIN" ]; then
    try_brew_install python@3.14 "brew install python@3.14"
    PYTHON_BIN="$(find_python_310 || true)"
fi
if [ -z "$PYTHON_BIN" ]; then
    echo "[ERROR] Python 3.10+ still not found after install attempt."
    exit 1
fi
echo "[OK] Python: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"

# --- determine port ---
DEFAULT_PORT=8787
if [ -f "$CFG_PATH" ]; then
    EXISTING_PORT=$("$PYTHON_BIN" -c "import json;print(json.load(open('$CFG_PATH')).get('listen_port',''))" 2>/dev/null || echo "")
    if [ -n "$EXISTING_PORT" ]; then
        DEFAULT_PORT="$EXISTING_PORT"
    fi
fi
echo ""
read -rp "Listen port [$DEFAULT_PORT]: " LISTEN_PORT || LISTEN_PORT=""
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

# --- generate launchd plist (absolute python path, brew PATH for children) ---
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
echo "Next steps (all in the admin console):"
echo "  1. Login Cloudflare (button appears when not logged in)"
echo "  2. Configure tunnel (hostname + tunnel name) and Save & Restart"
echo "  3. Add models and the upstream API key"
echo ""
echo "Manage service:"
echo "  launchctl list | grep $PLIST_LABEL   # check status"
echo "  launchctl unload $PLIST_PATH          # stop"
echo "  launchctl load $PLIST_PATH            # start"
chmod +x "$SCRIPT_DIR/install.sh"
