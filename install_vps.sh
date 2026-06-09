#!/bin/bash
# install_vps.sh — AetherGate Pro · One-Key VPS Bootstrap Installer
# Usage: curl -sSL https://raw.githubusercontent.com/JFGAtlas/aethergate-pro/main/install_vps.sh | bash
# ----------------------------------------------------------------------------------------------------
set -euo pipefail

# Colors
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'

echo -e "${BLUE}[AetherGate Pro] 开始一键部署流程...${NC}"

# 1. Check root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}[错误] 请使用 root 权限运行此脚本 (例如: sudo bash 或在 root 用户下运行)${NC}" >&2
    exit 1
fi

# 2. Install Git if not installed
if ! command -v git &>/dev/null; then
    echo -e "${YELLOW}[提示] 未检测到 git，正在为您安装...${NC}"
    if command -v apt-get &>/dev/null; then
        apt-get update -y -q && apt-get install -y -q git
    elif command -v dnf &>/dev/null; then
        dnf install -y -q git
    elif command -v yum &>/dev/null; then
        yum install -y -q git
    else
        echo -e "${RED}[错误] 未能检测到支持的包管理器，请先手动安装 git后再运行此脚本。${NC}" >&2
        exit 1
    fi
fi

# 3. Clone Repository
TEMP_DIR="/tmp/aethergate-pro-deploy"
echo -e "${BLUE}[AetherGate Pro] 正在拉取最新的项目源码至临时目录 ${TEMP_DIR}...${NC}"
rm -rf "$TEMP_DIR"

if git clone https://github.com/JFGAtlas/aethergate-pro.git "$TEMP_DIR"; then
    echo -e "${GREEN}[OK] 源码下载成功。${NC}"
else
    echo -e "${RED}[错误] 源码克隆失败。请确认您的 VPS 网络可以正常访问 github.com 并安装了 git。${NC}" >&2
    exit 1
fi

# 4. Run Installer
cd "$TEMP_DIR"
echo -e "${BLUE}[AetherGate Pro] 正在启动主安装程序...${NC}"
bash install.sh

# 5. Clean up staging files
echo -e "${BLUE}[AetherGate Pro] 正在清理临时部署文件...${NC}"
rm -rf "$TEMP_DIR"
echo -e "${GREEN}[OK] 临时部署文件已清理完毕。${NC}"
