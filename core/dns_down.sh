#!/bin/bash
# OpenVPN down script to restore DNS inside the namespace
python3 /opt/vpngate-pro/core/dns_update.py down
