import os
import sys
import subprocess
import shutil

class NetNSManager:
    def __init__(self, ns_name="vpn_ns", subnet="10.200.0", host_port=7928, ns_port=7928):
        self.ns_name = ns_name
        self.subnet = subnet
        self.host_ip = f"{subnet}.1"
        self.ns_ip = f"{subnet}.2"
        self.host_port = host_port
        self.ns_port = ns_port
        
        self.is_linux = sys.platform.startswith("linux")
        self.socat_proc = None

    def run_cmd(self, cmd, check=True):
        """Helper to run a shell command."""
        try:
            res = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, check=check)
            return True, res.stdout.strip()
        except subprocess.CalledProcessError as e:
            print(f"[NetNS] Command failed: {cmd}\nError: {e.stderr.strip()}", flush=True)
            return False, e.stderr.strip()

    def exists(self):
        """Check if namespace exists."""
        if not self.is_linux:
            return False
        success, stdout = self.run_cmd("ip netns list")
        if success:
            namespaces = [line.split()[0] for line in stdout.splitlines() if line]
            return self.ns_name in namespaces
        return False

    def setup(self) -> bool:
        """Set up the namespace, veth pair, routing, and iptables rules."""
        if not self.is_linux:
            print("[NetNS] Non-Linux platform detected. Skipping namespace setup (mock mode).", flush=True)
            return True

        print(f"[NetNS] Setting up network namespace '{self.ns_name}'...", flush=True)
        
        # 1. Cleanup first if exists
        self.cleanup()

        # 2. Create namespace
        ok, err = self.run_cmd(f"ip netns add {self.ns_name}")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to add network namespace '{self.ns_name}': {err}", flush=True)
            return False
            
        ok, _ = self.run_cmd(f"ip netns exec {self.ns_name} ip link set lo up")
        if not ok:
            print("[NetNS] Warning: Failed to set lo interface up inside namespace.", flush=True)

        # 3. Create veth pair
        # veth_host (host side) <-> veth_vpn (inside namespace)
        ok, err = self.run_cmd(f"ip link add veth_host type veth peer name veth_vpn")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to create veth pair: {err}", flush=True)
            return False
            
        ok, err = self.run_cmd(f"ip link set veth_vpn netns {self.ns_name}")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to move veth_vpn into namespace: {err}", flush=True)
            return False

        # 4. Configure Host side of veth
        ok, err = self.run_cmd(f"ip addr add {self.host_ip}/24 dev veth_host")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to assign address to veth_host: {err}", flush=True)
            return False
            
        ok, err = self.run_cmd(f"ip link set veth_host up")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to set veth_host interface up: {err}", flush=True)
            return False

        # 5. Configure NS side of veth
        ok, err = self.run_cmd(f"ip netns exec {self.ns_name} ip addr add {self.ns_ip}/24 dev veth_vpn")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to assign address to veth_vpn inside namespace: {err}", flush=True)
            return False
            
        ok, err = self.run_cmd(f"ip netns exec {self.ns_name} ip link set veth_vpn up")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to set veth_vpn interface up inside namespace: {err}", flush=True)
            return False

        # 6. Set default gateway inside the namespace to go through Host IP
        ok, err = self.run_cmd(f"ip netns exec {self.ns_name} ip route add default via {self.host_ip}")
        if not ok:
            print(f"[NetNS] Critical Error: Failed to set default route inside namespace: {err}", flush=True)
            return False

        # 7. Enable IP forwarding on Host
        self.run_cmd("sysctl -w net.ipv4.ip_forward=1")

        # 8. Configure NAT on Host to masquerade namespace outbound traffic
        # Try to find default physical interface
        _, route_out = self.run_cmd("ip route show default")
        physical_interface = None
        if route_out and "dev" in route_out:
            parts = route_out.split()
            try:
                idx = parts.index("dev")
                physical_interface = parts[idx + 1]
            except (ValueError, IndexError):
                pass
        
        masq_cmd = f"iptables -t nat -A POSTROUTING -s {self.subnet}.0/24 -j MASQUERADE"
        if physical_interface:
            masq_cmd = f"iptables -t nat -A POSTROUTING -s {self.subnet}.0/24 -o {physical_interface} -j MASQUERADE"
            
        self.run_cmd(masq_cmd)
        
        # 9. Configure DNS overrides for the namespace
        try:
            os.makedirs(f"/etc/netns/{self.ns_name}", exist_ok=True)
            with open(f"/etc/netns/{self.ns_name}/resolv.conf", "w") as f:
                f.write("nameserver 8.8.8.8\nnameserver 1.1.1.1\n")
            print(f"[NetNS] DNS overrides configured for namespace: /etc/netns/{self.ns_name}/resolv.conf", flush=True)
        except Exception as e:
            print(f"[NetNS] Warning: Failed to create DNS override: {e}", flush=True)

        print(f"[NetNS] Network namespace '{self.ns_name}' successfully configured. Subnet: {self.subnet}.0/24", flush=True)
        return True

    def start_port_forward(self, listen_host="127.0.0.1"):
        """Expose namespace proxy port to the host using socat."""
        if not self.is_linux:
            return
            
        if not shutil.which("socat"):
            print("[NetNS] Warning: 'socat' not found. Please install it on the VPS to enable port forwarding.", flush=True)
            return

        print(f"[NetNS] Starting socat port forwarder: {listen_host}:{self.host_port} -> {self.ns_ip}:{self.ns_port}...", flush=True)
        self.stop_port_forward()
        
        # Start socat in the background
        cmd = [
            "socat",
            f"TCP-LISTEN:{self.host_port},fork,reuseaddr,bind={listen_host}",
            f"TCP:{self.ns_ip}:{self.ns_port}"
        ]
        try:
            self.socat_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[NetNS] Failed to start socat: {e}", flush=True)

    def stop_port_forward(self):
        """Stop the socat forwarder."""
        if self.socat_proc:
            try:
                self.socat_proc.terminate()
                self.socat_proc.wait(timeout=2)
            except Exception:
                try:
                    self.socat_proc.kill()
                except Exception:
                    pass
            self.socat_proc = None
            
        # Also kill any orphan socats for our port
        if self.is_linux:
            subprocess.run(f"pkill -f 'socat TCP-LISTEN:{self.host_port}'", shell=True, capture_output=True)

    def cleanup(self):
        """Tear down routing rules, veth interfaces, and namespace."""
        if not self.is_linux:
            return

        print(f"[NetNS] Cleaning up network namespace '{self.ns_name}' assets...", flush=True)
        self.stop_port_forward()

        # 1. Force kill all processes running inside the namespace
        try:
            res = subprocess.run(["ip", "netns", "pids", self.ns_name], capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip():
                pids = res.stdout.strip().split()
                print(f"[NetNS] Force killing processes inside namespace: {pids}", flush=True)
                subprocess.run(["kill", "-9"] + pids, capture_output=True)
        except Exception as e:
            print(f"[NetNS] Warning: Failed to kill processes inside namespace: {e}", flush=True)

        # 2. Delete namespace (automatically deletes veth_vpn and routes inside it)
        if self.exists():
            self.run_cmd(f"ip netns del {self.ns_name}", check=False)

        # 3. Double check and force unmount if namespace file still exists (resource busy lock)
        ns_file = f"/run/netns/{self.ns_name}"
        if os.path.exists(ns_file):
            print(f"[NetNS] Namespace file still exists after del, force unmounting {ns_file}...", flush=True)
            self.run_cmd(f"umount -l {ns_file}", check=False)
            self.run_cmd(f"rm -f {ns_file}", check=False)

        # Delete host veth interface if it exists
        self.run_cmd("ip link delete veth_host", check=False)

        # Clean up iptables NAT rules
        # Find and delete rules containing our subnet
        success, rules = self.run_cmd("iptables -t nat -S POSTROUTING")
        if success:
            for rule in rules.splitlines():
                if f"{self.subnet}.0/24" in rule:
                    del_rule = rule.replace("-A", "-D")
                    self.run_cmd(f"iptables -t nat {del_rule}", check=False)

        # Delete DNS override files
        ns_resolv_dir = f"/etc/netns/{self.ns_name}"
        if os.path.exists(ns_resolv_dir):
            try:
                shutil.rmtree(ns_resolv_dir)
            except Exception:
                pass

        print("[NetNS] Cleanup complete.", flush=True)

    def get_ns_prefix(self):
        """Get the command prefix to run things inside the namespace."""
        if self.is_linux:
            return ["ip", "netns", "exec", self.ns_name]
        return []
