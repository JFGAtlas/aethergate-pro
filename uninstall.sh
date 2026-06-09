#!/bin/bash
# uninstall.sh — AetherGate Pro · Full Clean Uninstaller
# Usage: curl -sSL https://raw.githubusercontent.com/JFGAtlas/aethergate-pro/main/uninstall.sh | bash
# ----------------------------------------------------------------------------------------------------
set -euo pipefail

# Colors
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

echo -e "${BLUE}[AetherGate Pro] 开始完全卸载流程...${NC}"

# 1. Check root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[错误] 请使用 root 权限运行此脚本 (例如: sudo bash)${NC}" >&2
    exit 1
fi

SERVICE_NAME="vpngate-pro"
TARGET_DIR="/opt/vpngate-pro"
SYSCTL_CONF="/etc/sysctl.d/99-aethergate.conf"
NS_NAME="vpn_ns"

# 2. Stop & Disable systemd service
echo -e "${BLUE}[1/6] 停止并禁用 systemd 服务...${NC}"
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    systemctl stop "${SERVICE_NAME}" || true
fi
systemctl disable "${SERVICE_NAME}" &>/dev/null || true
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
rm -f "/etc/systemd/system/aimilivpn.service" 2>/dev/null || true
systemctl daemon-reload
echo -e "${GREEN}[OK] 服务已停止并清理。${NC}"

# 3. Terminate running processes
echo -e "${BLUE}[2/6] 强制终止所有相关残留进程...${NC}"
# Stop active OpenVPN or SOCKS5 proxies
pkill -9 -f "openvpn" &>/dev/null || true
pkill -9 -f "main.py" &>/dev/null || true
pkill -9 -f "socat" &>/dev/null || true
echo -e "${GREEN}[OK] 残留进程已终止。${NC}"

# 4. Clean up network namespace & interfaces
echo -e "${BLUE}[3/6] 清理隔离网络命名空间与网卡...${NC}"
# Force kill namespace processes
if ip netns list 2>/dev/null | grep -q "${NS_NAME}"; then
    # Try to find and kill all PIDs in namespace
    PIDS=$(ip netns pids "${NS_NAME}" 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        kill -9 $PIDS &>/dev/null || true
    fi
    ip netns del "${NS_NAME}" &>/dev/null || true
fi

# Lazy unmount if namespace mount is still locked
ns_file="/run/netns/${NS_NAME}"
if [ -e "$ns_file" ]; then
    umount -l "$ns_file" &>/dev/null || true
    rm -f "$ns_file" &>/dev/null || true
fi

# Remove netns resolv overrides
rm -rf "/etc/netns/${NS_NAME}"

# Delete host virtual network interface
ip link delete veth_host &>/dev/null || true
echo -e "${GREEN}[OK] 网络命名空间与虚拟网卡已清理。${NC}"

# 5. Clean up IPTables NAT rules & sysctl
echo -e "${BLUE}[4/6] 清理防火墙 NAT 转发规则与内核配置...${NC}"
if command -v iptables &>/dev/null; then
    # Delete AetherGate POSTROUTING masquerade rules
    # Look for subnet 10.200.0 (default for AetherGate)
    rules=$(iptables -t nat -S POSTROUTING 2>/dev/null || true)
    if [ -n "$rules" ]; then
        echo "$rules" | while read -r rule; do
            if [[ "$rule" == *"10.200.0.0/24"* ]]; then
                del_rule=${rule/-A/-D}
                iptables -t nat $del_rule &>/dev/null || true
            fi
        done
    fi
fi

# Remove kernel configuration
if [ -f "$SYSCTL_CONF" ]; then
    rm -f "$SYSCTL_CONF"
    sysctl --system &>/dev/null || true
fi
echo -e "${GREEN}[OK] 防火墙与内核配置已还原。${NC}"

# 6. Delete install dir and config caches
echo -e "${BLUE}[5/6] 删除安装目录与缓存数据...${NC}"
if [ -d "$TARGET_DIR" ]; then
    rm -rf "$TARGET_DIR"
fi
# Remove legacy dir if exists
if [ -d "/opt/aimilivpn" ]; then
    rm -rf "/opt/aimilivpn"
fi
echo -e "${GREEN}[OK] 安装目录与缓存数据已彻底删除。${NC}"

# 7. Complete
echo -e "${BLUE}[6/6] 整理系统环境...${NC}"
echo ""
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  ✅  AetherGate Pro 已完全卸载并清理干净！          ${NC}"
echo -e "${GREEN}${BOLD}════════════════════════════════════════════════════${NC}"
echo -e "  所有服务、网络空间、虚拟网卡、NAT 规则以及配置缓存均已清除。"
echo ""
