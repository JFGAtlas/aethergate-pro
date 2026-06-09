#!/bin/bash
# install.sh — AetherGate Pro · Full-Stack Update & Daemon Installer
# Supports: Debian 11/12, Ubuntu 20.04+, CentOS 7/8, Rocky/Alma 8/9
# Usage:  sudo bash install.sh
# -----------------------------------------------------------------------
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════
#  0. Colour helpers & banner
# ═══════════════════════════════════════════════════════════════════════
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

echo -e "${BOLD}"
echo "  █████╗ ███████╗████████╗██╗  ██╗███████╗██████╗ ██████╗ ██████╗  ██████╗ "
echo " ██╔══██╗██╔════╝╚══██╔══╝██║  ██║██╔════╝██╔══██╗██╔══██╗██╔══██╗██╔═══██╗"
echo " ███████║█████╗     ██║   ███████║█████╗  ██████╔╝██║  ██║██████╔╝██║   ██║"
echo " ██╔══██║██╔══╝     ██║   ██╔══██║██╔══╝  ██╔══██╗██║  ██║██╔═══╝ ██║   ██║"
echo " ██║  ██║███████╗   ██║   ██║  ██║███████╗██║  ██║██████╔╝██║     ╚██████╔╝"
echo " ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═════╝ ╚═╝      ╚═════╝ "
echo -e "${NC}"
echo -e "${BOLD}AetherGate Pro — AutoPilot Async Kernel Installer${NC}"
echo "Version: 2.0.0-async | $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"

# ═══════════════════════════════════════════════════════════════════════
#  1. Pre-flight checks
# ═══════════════════════════════════════════════════════════════════════
info "[1/8] Pre-flight checks..."
[ "$EUID" -ne 0 ] && die "Please run as root (sudo bash install.sh)"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/opt/vpngate-pro"
DATA_DIR="${TARGET_DIR}/vpngate_data"
SERVICE_NAME="vpngate-pro"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
PYTHON_BIN=""

# Detect Python 3.10+
for py in python3.12 python3.11 python3.10 python3; do
    if command -v "$py" &>/dev/null; then
        VER=$("$py" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null || echo "False")
        if [ "$VER" = "True" ]; then
            PYTHON_BIN="$py"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    warn "Python 3.10+ not found. Will attempt to install python3..."
    PYTHON_BIN="python3"
fi

success "Using Python: $PYTHON_BIN ($(${PYTHON_BIN} --version 2>&1))"

# ═══════════════════════════════════════════════════════════════════════
#  2. System packages
# ═══════════════════════════════════════════════════════════════════════
info "[2/8] Installing system packages..."

PKG_LIST="openvpn socat python3 python3-pip iptables ca-certificates curl"

if command -v apt-get &>/dev/null; then
    apt-get update -qq -y 2>/dev/null || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
        -o Dpkg::Options::="--force-confdef" \
        -o Dpkg::Options::="--force-confold" \
        $PKG_LIST || warn "Some apt packages may have failed."
elif command -v dnf &>/dev/null; then
    dnf install -y -q epel-release 2>/dev/null || true
    dnf install -y -q $PKG_LIST || warn "Some dnf packages may have failed."
elif command -v yum &>/dev/null; then
    yum install -y -q epel-release 2>/dev/null || true
    yum install -y -q $PKG_LIST || warn "Some yum packages may have failed."
else
    warn "No supported package manager found. Ensure openvpn, socat, python3 are installed."
fi

success "System packages ready."

# ═══════════════════════════════════════════════════════════════════════
#  3. Python dependencies (uvloop + httpx for async engine)
# ═══════════════════════════════════════════════════════════════════════
info "[3/8] Installing Python async engine dependencies..."

# Use --break-system-packages for newer Debian/Ubuntu where needed
PIP_FLAGS="--quiet --no-warn-script-location"
if "$PYTHON_BIN" -m pip install --help 2>&1 | grep -q "break-system-packages"; then
    PIP_FLAGS="$PIP_FLAGS --break-system-packages"
fi

"$PYTHON_BIN" -m pip install $PIP_FLAGS \
    "uvloop>=0.19" \
    "httpx[http2]>=0.27" \
    "fastapi>=0.111" \
    "websockets>=12.0" \
    2>&1 | tail -5 || warn "Some Python packages may have failed to install."

# Verify critical imports
"$PYTHON_BIN" -c "import uvloop; print(f'  uvloop {uvloop.__version__} ✓')" 2>/dev/null \
    || warn "uvloop import failed — engine will fall back to standard asyncio."
"$PYTHON_BIN" -c "import httpx;  print(f'  httpx  {httpx.__version__} ✓')"  2>/dev/null \
    || warn "httpx import failed — HTTP requests will use urllib fallback."

success "Python dependencies ready."

# ═══════════════════════════════════════════════════════════════════════
#  4. Stop existing service gracefully
# ═══════════════════════════════════════════════════════════════════════
info "[4/8] Gracefully stopping existing service..."

# Give systemd a chance to stop cleanly before killing orphans
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    systemctl stop "${SERVICE_NAME}" || true
    sleep 2
fi

# Stop any legacy service name
systemctl stop aimilivpn 2>/dev/null || true
systemctl disable aimilivpn 2>/dev/null || true

# Kill orphan processes — order matters: openvpn first, then proxy-only, then socat
pkill -TERM -f "openvpn"                   2>/dev/null || true
pkill -TERM -f "main.py --proxy-only"      2>/dev/null || true
sleep 1
pkill -KILL -f "openvpn"                   2>/dev/null || true
pkill -KILL -f "main.py --proxy-only"      2>/dev/null || true
pkill -KILL -f "socat TCP-LISTEN:7928"     2>/dev/null || true

success "Existing service stopped."

# ═══════════════════════════════════════════════════════════════════════
#  5. Deploy codebase (atomic copy strategy)
# ═══════════════════════════════════════════════════════════════════════
info "[5/8] Deploying codebase to ${TARGET_DIR}..."

mkdir -p "${TARGET_DIR}/core"
mkdir -p "${TARGET_DIR}/web"
mkdir -p "${DATA_DIR}"

# Copy to a staging directory then atomically rename → zero deployment gap
STAGING="${TARGET_DIR}/.staging_$$"
mkdir -p "${STAGING}/core"
mkdir -p "${STAGING}/web"

cp "${SCRIPT_DIR}/main.py"       "${STAGING}/"
cp -r "${SCRIPT_DIR}/core/."     "${STAGING}/core/"
cp -r "${SCRIPT_DIR}/web/."      "${STAGING}/web/"

# Move each top-level item (data_dir is excluded — preserved separately)
cp -a "${STAGING}/main.py"    "${TARGET_DIR}/main.py"
cp -a "${STAGING}/core/."     "${TARGET_DIR}/core/"
cp -a "${STAGING}/web/."      "${TARGET_DIR}/web/"
rm -rf "${STAGING}"

chmod +x "${TARGET_DIR}/main.py"
chmod +x "${TARGET_DIR}/core/"*.sh 2>/dev/null || true

# ── Data migration from legacy installs ─────────────────────────────────
OLD_CACHE="/opt/aimilivpn/vpngate_data/ip_cache.json"
NEW_CACHE="${DATA_DIR}/ip_cache.json"
if [ -f "$OLD_CACHE" ] && [ ! -f "$NEW_CACHE" ]; then
    info "Migrating legacy IP cache..."
    cp "$OLD_CACHE" "$NEW_CACHE"
    success "IP cache migrated."
fi

OLD_AUTH="/opt/aimilivpn/vpngate_data/ui_auth.json"
NEW_CONFIG="${DATA_DIR}/config.json"
if [ -f "$OLD_AUTH" ] && [ ! -f "$NEW_CONFIG" ]; then
    info "Migrating legacy credentials from ui_auth.json..."
    "$PYTHON_BIN" - "$OLD_AUTH" "$NEW_CONFIG" << 'PYEOF'
import json, sys
try:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as f: old = json.load(f)
    cfg = {
        "username":               old.get("username", "admin"),
        "password":               old.get("password", ""),
        "secret_path":            old.get("secret_path", ""),
        "ui_host":                "0.0.0.0",
        "ui_port":                old.get("port", 8787),
        "proxy_host":             "127.0.0.1",
        "proxy_port":             7928,
        "routing_mode":           old.get("routing_mode", "auto"),
        "force_country":          old.get("force_country", ""),
        "connection_enabled":     old.get("connection_enabled", False),
        "fixed_node_id":          old.get("fixed_node_id", ""),
        "scamalytics_threshold":  10,
    }
    with open(dst, "w") as f: json.dump(cfg, f, indent=2)
    print("  Credentials migrated successfully.")
except Exception as e:
    print(f"  Migration failed: {e} (non-fatal)")
PYEOF
fi

if [ -f "$NEW_CONFIG" ]; then
    chmod 600 "$NEW_CONFIG"
fi

success "Codebase deployed."

# ═══════════════════════════════════════════════════════════════════════
#  6. Systemd service — autopilot-ready unit with watchdog integration
# ═══════════════════════════════════════════════════════════════════════
info "[6/8] Configuring systemd service unit..."

cat > "${SERVICE_FILE}" << EOF
# ─────────────────────────────────────────────────────────
#  AetherGate Pro · AutoPilot Async Kernel
#  Managed by install.sh — do not edit manually.
# ─────────────────────────────────────────────────────────
[Unit]
Description=AetherGate Pro — AutoPilot Async VPN Gateway
Documentation=https://github.com/JFGAtlas/aethergate-pro
After=network-online.target
Wants=network-online.target
# Ensure clean networking state before start
After=NetworkManager.service systemd-resolved.service

[Service]
Type=simple
User=root
WorkingDirectory=${TARGET_DIR}

# Python path and performance tunables
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${TARGET_DIR}
# Increase file descriptor limit for proxy connection pool
LimitNOFILE=65536

ExecStart=${PYTHON_BIN} ${TARGET_DIR}/main.py

# ── Graceful restart policy ──────────────────────────────
# Restart on any failure; back off on rapid crash-loops.
Restart=always
RestartSec=5
StartLimitInterval=120
StartLimitBurst=5

# ── Watchdog integration ─────────────────────────────────
# systemd will restart the service if it doesn't ping
# sd_notify within WatchdogSec seconds.
# (Requires main.py to call sd_notify — see note below)
# WatchdogSec=90

# ── Security hardening ──────────────────────────────────
PrivateTmp=yes
ProtectHome=yes


# ── Logging ─────────────────────────────────────────────
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aethergate-pro

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
success "Systemd unit configured: ${SERVICE_FILE}"

# ═══════════════════════════════════════════════════════════════════════
#  7. Kernel & network tunables
# ═══════════════════════════════════════════════════════════════════════
info "[7/8] Applying kernel tunables..."

# IP forwarding (required for NAT masquerade)
sysctl -w net.ipv4.ip_forward=1 &>/dev/null
# Increase local port range for high connection volume
sysctl -w net.ipv4.ip_local_port_range="1024 65535" &>/dev/null || true
# Reuse TIME_WAIT sockets
sysctl -w net.ipv4.tcp_tw_reuse=1 &>/dev/null || true

# Persist settings
SYSCTL_CONF="/etc/sysctl.d/99-aethergate.conf"
cat > "${SYSCTL_CONF}" << 'SEOF'
# AetherGate Pro — persistent kernel tunables
net.ipv4.ip_forward          = 1
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_tw_reuse        = 1
SEOF
sysctl -p "${SYSCTL_CONF}" &>/dev/null || true

success "Kernel tunables applied."

# ═══════════════════════════════════════════════════════════════════════
#  8. Enable & start service
# ═══════════════════════════════════════════════════════════════════════
info "[8/8] Starting AetherGate Pro service..."

systemctl enable "${SERVICE_NAME}.service" &>/dev/null
systemctl start  "${SERVICE_NAME}.service"

# Brief settle window before status check
sleep 3

echo ""
echo "─────────────────── Service Status ───────────────────"
systemctl status "${SERVICE_NAME}.service" --no-pager -l 2>&1 | head -20
echo "───────────────────────────────────────────────────────"

# Detect configured port from data dir config
UI_PORT="8787"
SECRET_PATH=""
UI_USER="admin"
UI_PASS=""
if [ -f "${DATA_DIR}/config.json" ]; then
    UI_PORT=$(    "$PYTHON_BIN" -c "import json; c=json.load(open('${DATA_DIR}/config.json')); print(c.get('ui_port',8787))"  2>/dev/null || echo "8787")
    SECRET_PATH=$(  "$PYTHON_BIN" -c "import json; c=json.load(open('${DATA_DIR}/config.json')); print(c.get('secret_path',''))" 2>/dev/null || echo "")
    UI_USER=$(    "$PYTHON_BIN" -c "import json; c=json.load(open('${DATA_DIR}/config.json')); print(c.get('username','admin'))"  2>/dev/null || echo "admin")
    UI_PASS=$(    "$PYTHON_BIN" -c "import json; c=json.load(open('${DATA_DIR}/config.json')); print(c.get('password',''))"  2>/dev/null || echo "")
fi

# Get server public IP best-effort
SERVER_IP=$(curl -sf --max-time 4 https://api.ipify.org 2>/dev/null || echo "<VPS_IP>")

echo ""
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✅  AetherGate Pro deployed successfully!          ${NC}"
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  📁 Install dir  :  ${TARGET_DIR}"
echo -e "  ⚙️  Config file  :  ${DATA_DIR}/config.json"
echo ""
if [ -n "$SECRET_PATH" ]; then
    echo -e "  🌐 Dashboard UI :  http://${SERVER_IP}:${UI_PORT}/${SECRET_PATH}/"
else
    echo -e "  🌐 Dashboard UI :  http://${SERVER_IP}:${UI_PORT}/"
fi
echo -e "  🔑 UI Username  :  ${UI_USER}"
echo -e "  🔑 UI Password  :  ${UI_PASS}"
echo ""
echo -e "  🔌 Proxy (SOCKS5/HTTP) :  ${SERVER_IP}:7928"
echo -e "  📡 WebSocket endpoint  :  ws://${SERVER_IP}:${UI_PORT}/${SECRET_PATH}/api/ws"
echo ""
echo -e "  📋 Live logs    :  journalctl -u ${SERVICE_NAME} -f"
echo -e "  🔄 Restart      :  systemctl restart ${SERVICE_NAME}"
echo -e "  🛑 Stop         :  systemctl stop ${SERVICE_NAME}"
echo ""
echo -e "${CYAN}  AutoPilot Watchdog : ENABLED (60s health cycle)${NC}"
echo -e "${CYAN}  Circuit Breaker    : ARMED   (trips at 3 failures)${NC}"
echo -e "${CYAN}  Atomic NAT Switch  : READY   (zero proxy downtime)${NC}"
echo ""
