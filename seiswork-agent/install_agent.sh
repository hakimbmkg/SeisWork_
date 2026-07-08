#!/usr/bin/env bash
# SeisWork Agent Installer — by HakimBMKG
# Install the agent on a SeisComP server (local or remote)
# Usage: bash seiswork-agent/install_agent.sh [--port 7001] [--init]

set -uo pipefail

AGENT_DIR="$HOME/.seiswork-agent"
AGENT_SCRIPT="$AGENT_DIR/agent.py"
PORT=7001
DO_INIT=false

for arg in "$@"; do
    case $arg in
        --port) shift; PORT="$1" ;;
        --init) DO_INIT=true ;;
    esac
done

# Resolve the agent.py path
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_AGENT="$SCRIPT_DIR/agent.py"

if [ ! -f "$SRC_AGENT" ]; then
    echo "[ERROR] agent.py not found at $SRC_AGENT"
    exit 1
fi

echo "=== SeisWork Agent Installer ==="
mkdir -p "$AGENT_DIR"
cp "$SRC_AGENT" "$AGENT_SCRIPT"
chmod +x "$AGENT_SCRIPT"

# Find a suitable Python (SeisComP ships its own, or fall back to system)
SC_PYTHON=""
for candidate in \
    "$HOME/seiscomp/bin/python3" \
    "/opt/seiscomp/bin/python3" \
    "/usr/local/seiscomp/bin/python3" \
    "$(which python3 2>/dev/null)"; do
    if [ -x "$candidate" ]; then
        SC_PYTHON="$candidate"
        break
    fi
done

if [ -z "$SC_PYTHON" ]; then
    echo "[ERROR] Python3 not found"
    exit 1
fi
echo "[OK] Python: $SC_PYTHON"

# Install Flask when missing (agent REST API: /register, /sync, /push/bindings)
$SC_PYTHON -c "import flask" 2>/dev/null || {
    echo "[INFO] Installing Flask..."
    $SC_PYTHON -m pip install flask --quiet
}

# Install pymysql when missing — REQUIRED for the DB-poll bridge fallback
# (scautopick → SeisWork). The bridge prefers the seiscomp.client message bus; if
# that import fails (e.g. a NumPy ABI mismatch in the env), the bridge falls back to
# polling the Pick table in the SeisComP DB via pymysql. Without it, scautopick pick integration is dead.
$SC_PYTHON -c "import pymysql" 2>/dev/null || {
    echo "[INFO] Installing pymysql (scautopick DB-poll bridge)..."
    $SC_PYTHON -m pip install pymysql --quiet \
        && echo "[OK] pymysql installed" \
        || echo "[WARN] pymysql failed — the DB-poll bridge is unavailable (the message bus is still tried)"
}

# Create the launcher script
cat > "$AGENT_DIR/start_agent.sh" << EOFSH
#!/usr/bin/env bash
# SeisWork Agent launcher — by HakimBMKG
exec $SC_PYTHON $AGENT_SCRIPT --start --port $PORT
EOFSH
chmod +x "$AGENT_DIR/start_agent.sh"

# Create the systemd user service (when systemd is available)
if command -v systemctl &>/dev/null; then
    SVC_FILE="$HOME/.config/systemd/user/seiswork-agent.service"
    mkdir -p "$(dirname "$SVC_FILE")"
    cat > "$SVC_FILE" << EOFSVC
[Unit]
Description=SeisWork Agent
After=network.target

[Service]
Type=simple
ExecStart=$AGENT_DIR/start_agent.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOFSVC
    # systemctl --user needs a D-Bus session — use XDG_RUNTIME_DIR when available,
    # falling back gracefully when absent (e.g. a su/sudo non-login shell).
    UID_NOW="$(id -u)"
    XDG_BUS="unix:path=/run/user/${UID_NOW}/bus"
    if [ -S "/run/user/${UID_NOW}/bus" ]; then
        XDG_RUNTIME_DIR="/run/user/${UID_NOW}" DBUS_SESSION_BUS_ADDRESS="$XDG_BUS" \
            systemctl --user daemon-reload 2>/dev/null && \
        XDG_RUNTIME_DIR="/run/user/${UID_NOW}" DBUS_SESSION_BUS_ADDRESS="$XDG_BUS" \
            systemctl --user enable seiswork-agent 2>/dev/null || true
        echo "[OK] Systemd service: seiswork-agent"
    else
        echo "[WARN] D-Bus session unavailable — the service file was created but not auto-enabled."
        echo "       Run manually after logging in: systemctl --user enable --now seiswork-agent"
    fi
fi

if $DO_INIT || [ ! -f "$HOME/.seiswork-agent.conf" ]; then
    echo ""
    $SC_PYTHON "$AGENT_SCRIPT" --init
fi

echo ""
echo "=== Installation Complete ==="
echo "Run the agent: bash $AGENT_DIR/start_agent.sh"
echo "Or: systemctl --user start seiswork-agent"
echo ""
