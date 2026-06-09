import os
import sys

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "up"
    resolv_path = "/etc/resolv.conf"
    
    if action == "up":
        dns_servers = []
        # Parse foreign options for DNS pushed by OpenVPN
        for key, val in os.environ.items():
            if key.startswith("foreign_option_"):
                # Example: dhcp-option DNS 10.211.254.254
                parts = val.split()
                if len(parts) >= 3 and parts[0] == "dhcp-option" and parts[1] == "DNS":
                    dns_servers.append(parts[2])
        
        # Fallback to public DNS if none pushed
        if not dns_servers:
            dns_servers = ["8.8.8.8", "1.1.1.1"]
            
        print(f"[DNS-Update] Setting DNS servers: {dns_servers}", flush=True)
        try:
            # Overwrite the bind-mounted resolv.conf file directly
            with open(resolv_path, "w") as f:
                for dns in dns_servers:
                    f.write(f"nameserver {dns}\n")
            print("[DNS-Update] DNS update successful.", flush=True)
        except Exception as e:
            print(f"[DNS-Update] Error writing resolv.conf: {e}", flush=True)
            
    elif action == "down":
        print("[DNS-Update] Restoring default DNS servers (8.8.8.8, 1.1.1.1)", flush=True)
        try:
            with open(resolv_path, "w") as f:
                f.write("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")
            print("[DNS-Update] DNS restore successful.", flush=True)
        except Exception as e:
            print(f"[DNS-Update] Error writing resolv.conf: {e}", flush=True)

if __name__ == "__main__":
    main()
