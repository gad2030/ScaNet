#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LAN Profile Manager - Advanced Windows Utility
Features:
- List network adapters & current configuration
- Create/edit/delete network profiles (multiple IPs per adapter)
- Save/Load profiles to/from CSV file
- Apply profiles (static IP/DHCP, multiple IPs, gateway, DNS)
- Search profiles globally
- Live terminal output of all commands
- Auto-run as Administrator
- Tabbed UI, quarter‑screen size
"""

import sys
import os
import csv
import subprocess
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
import ipaddress
import traceback
import json
import threading
import socket
import struct
import time
import queue
import re
from datetime import datetime
import uuid


# ------------------------------------------------------------
# AUTO-ELEVATE TO ADMINISTRATOR
# ------------------------------------------------------------
def is_admin():
    try:
        import ctypes
        if sys.platform != "win32":
            return True
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

if not is_admin():
    # Re-run the script with admin rights
    import ctypes
    params = subprocess.list2cmdline(sys.argv)
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit()

# ------------------------------------------------------------
# UTILITY FUNCTIONS
# ------------------------------------------------------------
def is_valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(str(value).strip())
        return True
    except Exception:
        return False


def normalize_netmask(value: str) -> str | None:
    """Return dotted-decimal netmask for input like '255.255.255.0', '24', or '/24'."""
    s = str(value).strip()
    if not s:
        return None
    if s.startswith("/"):
        s = s[1:].strip()
    if s.isdigit():
        prefix = int(s)
        if 0 <= prefix <= 32:
            return str(ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask)
        return None
    if is_valid_ipv4(s):
        return s
    return None


def is_valid_netmask(value: str) -> bool:
    """Validate IPv4 netmask as dotted decimal or prefix length (/0..32)."""
    s = normalize_netmask(value)
    if not s:
        return False
    try:
        # IPv4Network validates contiguity of netmask bits.
        ipaddress.IPv4Network(f"0.0.0.0/{s}")
        return True
    except Exception:
        return False


def run_netsh(command, log_widget=None):
    """Run a netsh command and return (success, stdout, stderr). Optionally log."""
    full_cmd = f"netsh {command}"
    try:
        proc = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, check=False)
        output = proc.stdout + proc.stderr
        if log_widget:
            log_widget.insert(tk.END, f"> {full_cmd}\n{output}\n{'-'*60}\n")
            log_widget.see(tk.END)
        return proc.returncode == 0, proc.stdout, proc.stderr
    except Exception as e:
        error_msg = str(e)
        if log_widget:
            log_widget.insert(tk.END, f"ERROR: {error_msg}\n")
        return False, "", error_msg

def list_adapters():
    """Return dict {adapter_name: {'status':..., 'ips':[...]}} using netsh."""
    adapters = {}
    # Get interface names and status
    code, out, _ = run_netsh("interface show interface")
    if not code:
        return {}
    lines = out.splitlines()
    for line in lines:
        parts = line.split()
        # Format: "Enabled        Connected      Dedicated      Ethernet 2"
        if len(parts) >= 4 and parts[1] in ("Connected", "Disconnected"):
            state = parts[1]
            adapter_name = ' '.join(parts[3:]).strip()
            if adapter_name:
                adapters[adapter_name] = {'status': state, 'ips': []}

    # Get current IPv4 addresses and DHCP status for each adapter
    got_info = False
    if sys.platform == "win32":
        try:
            # Combined PowerShell command for efficiency
            ps_cmd = (
                "powershell -NoProfile -Command "
                "\"Get-NetIPInterface -AddressFamily IPv4 | Select-Object InterfaceAlias, Dhcp | ConvertTo-Json; "
                "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' } | Select-Object InterfaceAlias, IPAddress | ConvertTo-Json\""
            )
            proc = subprocess.run(ps_cmd, shell=True, capture_output=True, text=True, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                try:
                    # Split multiple JSON objects returned by PS
                    raw_out = proc.stdout.strip()
                    import re
                    # Match both arrays [] and objects {}
                    json_blocks = re.findall(r'(\[[\s\S]*?\]|\{[\s\S]*?\})', raw_out)
                    
                    interfaces = []
                    addresses = []
                    
                    if len(json_blocks) >= 1:
                        try: interfaces = json.loads(json_blocks[0])
                        except: pass
                    if len(json_blocks) >= 2:
                        try: addresses = json.loads(json_blocks[1])
                        except: pass

                    # Process interfaces (DHCP status)
                    for iface in (interfaces if isinstance(interfaces, list) else [interfaces]):
                        if not iface: continue
                        alias = iface.get("InterfaceAlias")
                        if alias in adapters:
                            adapters[alias]["dhcp"] = "DHCP" if iface.get("Dhcp") == 1 else "Static"

                    # Process addresses
                    for addr in (addresses if isinstance(addresses, list) else [addresses]):
                        if not addr: continue
                        alias = addr.get("InterfaceAlias")
                        ip = addr.get("IPAddress")
                        if alias in adapters:
                            adapters[alias]["ips"].append(ip)
                    got_info = True
                except Exception as e:
                    with open("crash_log.txt", "a") as f: f.write(f"PS Parse Error: {e}\n")
                for addr in (addresses if isinstance(addresses, list) else [addresses]):
                    alias = addr.get("InterfaceAlias")
                    ip = addr.get("IPAddress")
                    if alias in adapters:
                        adapters[alias]["ips"].append(ip)
                got_info = True
        except Exception:
            got_ips = False

    # Fallback: parse netsh output
    if not got_info:
        code, out, _ = run_netsh("interface ip show addresses")
        if code:
            current_adapter = None
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Interface"):
                    current_adapter = line.split("Interface", 1)[1].split("configuration", 1)[0].strip()
                elif line.startswith("IP Address") and current_adapter and current_adapter in adapters:
                    ip_part = line.split(":", 1)[1].strip()
                    if ip_part and ip_part != "0.0.0.0":
                        adapters[current_adapter]['ips'].append(ip_part)

    return adapters

def load_profiles_from_csv(csv_path="lan_profiles.csv"):
    """Load profiles from CSV; return list of dicts."""
    profiles = []
    if not os.path.exists(csv_path):
        return profiles
    try:
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Reconstruct IP list from stored string "ip1;mask1|ip2;mask2"
                ip_entries = []
                if row.get('multi_ip_masks'):
                    for entry in row['multi_ip_masks'].split('|'):
                        if ';' in entry:
                            ip, mask = entry.split(';', 1)
                            ip_entries.append((ip, mask))
                row['ip_entries'] = ip_entries
                row['dhcp'] = str(row.get('dhcp', '')).strip().lower() == 'true'
                profiles.append(row)
    except Exception as e:
        print(f"CSV load error: {e}")
    return profiles

def save_profiles_to_csv(profiles, csv_path="lan_profiles.csv"):
    """Save profiles list to CSV."""
    fieldnames = ['profile_name', 'adapter', 'dhcp', 'gateway', 'dns1', 'dns2', 'multi_ip_masks']
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        if not profiles:
            return
        for prof in profiles:
            # Encode ip_entries as "ip;mask|ip;mask"
            ip_masks_str = '|'.join([f"{ip};{mask}" for ip, mask in prof.get('ip_entries', [])])
            row = {
                'profile_name': prof['profile_name'],
                'adapter': prof['adapter'],
                'dhcp': 'True' if prof.get('dhcp', False) else 'False',
                'gateway': prof.get('gateway', ''),
                'dns1': prof.get('dns1', ''),
                'dns2': prof.get('dns2', ''),
                'multi_ip_masks': ip_masks_str
            }
            writer.writerow(row)

def apply_profile(profile, log_widget):
    """Apply a profile (static/DHCP, multiple IPs, DNS, gateway)."""
    adapter = profile['adapter']
    is_dhcp = profile.get('dhcp', False)
    ip_entries = profile.get('ip_entries', [])
    gateway = profile.get('gateway', '')
    dns1 = profile.get('dns1', '')
    dns2 = profile.get('dns2', '')

    if is_dhcp:
        # Set DHCP for IP and DNS
        success1, _, _ = run_netsh(f'interface ip set address "{adapter}" dhcp', log_widget)
        success2, _, _ = run_netsh(f'interface ip set dns "{adapter}" dhcp', log_widget)
        if success1 and success2:
            log_widget.insert(tk.END, f"✓ DHCP applied to {adapter}\n")
        else:
            log_widget.insert(tk.END, f"✗ Failed to set DHCP on {adapter}\n")
        return

    if not ip_entries:
        log_widget.insert(tk.END, f"⚠ No static IP entries defined for {adapter}\n")
        return

    # Delete any existing secondary IPs (optional cleanup)
    # We'll first set the primary IP (first entry)
    primary_ip, primary_mask = ip_entries[0]

    # Set primary IP with gateway (only for first IP)
    set_cmd = f'interface ip set address "{adapter}" static {primary_ip} {primary_mask}'
    if gateway:
        set_cmd += f" {gateway} 1"
    success, out, err = run_netsh(set_cmd, log_widget)
    if not success:
        log_widget.insert(tk.END, f"✗ Failed to set primary IP: {primary_ip}\n")
        return

    # Add additional IPs
    for ip, mask in ip_entries[1:]:
        add_cmd = f'interface ip add address "{adapter}" {ip} {mask}'
        run_netsh(add_cmd, log_widget)

    # Set DNS servers
    if dns1:
        run_netsh(f'interface ip set dns "{adapter}" static {dns1}', log_widget)
        if dns2:
            run_netsh(f'interface ip add dns "{adapter}" {dns2} index=2', log_widget)
    else:
        # Ensure no DNS set
        run_netsh(f'interface ip set dns "{adapter}" dhcp', log_widget)

    log_widget.insert(tk.END, f"✓ Profile '{profile['profile_name']}' applied to {adapter}\n")

# ------------------------------------------------------------
# OUI DICTIONARY (Common Vendors for IP Scanner)
# ------------------------------------------------------------
OUI_DICT = {
    "00:00:0C": "Cisco", "00:01:42": "Cisco", "00:05:5D": "D-Link", "00:0D:88": "D-Link",
    "00:0C:29": "VMware", "00:50:56": "VMware", "00:03:FF": "Microsoft", "00:15:5D": "Microsoft",
    "00:14:22": "Dell", "00:26:B9": "Dell", "F8:BC:12": "Dell", "00:11:85": "HP", "00:17:A4": "HP",
    "00:03:93": "Apple", "00:05:02": "Apple", "00:0A:27": "Apple", "28:CF:E9": "Apple",
    "00:12:47": "Samsung", "00:15:B9": "Samsung", "00:17:D1": "Samsung", "00:24:D7": "Intel",
    "00:1B:21": "Intel", "08:00:27": "VirtualBox", "00:0C:42": "MikroTik", "B8:27:EB": "Raspberry Pi",
    "DC:A6:32": "Raspberry Pi", "48:8F:5A": "TP-Link", "50:C7:BF": "TP-Link", "00:0B:82": "Netgear",
    "00:14:6C": "Netgear", "00:1D:AA": "Sony", "00:22:61": "ASUS", "00:E0:4C": "Realtek",
    "00:15:6D": "Ubiquiti", "00:27:22": "Ubiquiti", "24:A4:3C": "Ubiquiti", "70:A7:41": "Ubiquiti", "80:2A:A8": "Ubiquiti",
    "00:11:0A": "HP", "00:1E:0B": "HP", "00:26:55": "HP", "00:00:85": "Canon", "00:1E:8F": "Canon",
    "00:20:AF": "Epson", "00:00:48": "Seiko Epson", "00:80:77": "Brother", "00:1B:A9": "Brother",
    "00:03:68": "Xerox", "00:00:AA": "Xerox", "00:17:C8": "Kyocera"
}

# ------------------------------------------------------------
# MAIN GUI APPLICATION
# ------------------------------------------------------------
class LANManagerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("LAN Profile Manager - Advanced Edition")
        # Quarter of the screen size
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = screen_w // 2
        win_h = screen_h // 2
        self.root.geometry(f"{win_w}x{win_h}+{screen_w//4}+{screen_h//4}")
        self.root.minsize(600, 400)

        self.profiles = []          # list of profile dicts
        self.csv_path = "lan_profiles.csv"

        # Style
        style = ttk.Style()
        style.theme_use('clam')
        style.configure("TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=[12, 4])
        style.configure("TButton", font=("Segoe UI", 9), padding=4)
        style.configure("TLabel", font=("Segoe UI", 9))
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        # Main PanedWindow for resizable layout: Notebook (top) + Terminal (bottom)
        self.main_pane = ttk.PanedWindow(root, orient=tk.VERTICAL)
        self.main_pane.pack(fill=tk.BOTH, expand=True)

        # ----- Tabbed Header (Notebook) -----
        self.notebook = ttk.Notebook(self.main_pane)
        self.main_pane.add(self.notebook, weight=3)

        # Tab 1: Profile Manager
        self.profile_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.profile_frame, text="📁 Profile Manager")

        # Tab 2: Network Cards
        self.cards_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.cards_frame, text="🖧 Network Cards")

        # Tab 3: Settings / Info (optional)
        self.info_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.info_frame, text="ℹ️ Info")

        # ----- Bottom: Terminal / Status Area -----
        self.term_frame = ttk.LabelFrame(self.main_pane, text="⚡ Live Terminal Output", padding=4)
        self.main_pane.add(self.term_frame, weight=1)

        self.terminal = scrolledtext.ScrolledText(self.term_frame, height=8, bg="#1e1e1e", fg="#d4d4d4",
                                                  insertbackground="white", font=("Consolas", 9))
        self.terminal.pack(fill=tk.BOTH, expand=True)
        self.terminal.insert(tk.END, "Ready. All netsh commands will appear here.\n\n")

        # Build each tab's content
        self.build_profile_tab()
        self.build_cards_tab()
        self.build_info_tab()

        # Load existing profiles
        self.load_profiles()
        self.populate_profile_list()

        # Refresh card list
        self.refresh_cards_list()

        # Scanner initialization
        self.scan_running = False
        self.stop_event = threading.Event() # Added back missing stop_event
        self.my_mac = self.get_local_mac_fallback()
        self.history_path = "discovery_history.json"
        self.build_scanner_tab()

    # --------------------------------------------------------
    # PROFILE TAB
    # --------------------------------------------------------
    def build_profile_tab(self):
        # Top bar: search + add/delete/save/apply buttons
        top_bar = ttk.Frame(self.profile_frame)
        top_bar.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(top_bar, text="🔍 Search:").pack(side=tk.LEFT, padx=5)
        self.search_entry = ttk.Entry(top_bar, width=20)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<KeyRelease>", lambda e: self.populate_profile_list())

        ttk.Button(top_bar, text="➕ Add Profile", command=self.add_profile).pack(side=tk.RIGHT, padx=5)
        ttk.Button(top_bar, text="❌ Delete", command=self.delete_profile).pack(side=tk.RIGHT, padx=5)
        ttk.Button(top_bar, text="💾 Save All to CSV", command=self.save_profiles).pack(side=tk.RIGHT, padx=5)
        ttk.Button(top_bar, text="▶ Apply Selected", command=self.apply_selected_profile).pack(side=tk.RIGHT, padx=5)

        # Profile list (Treeview)
        columns = ("Profile", "Adapter", "DHCP", "IPs", "Gateway", "DNS")
        self.profile_tree = ttk.Treeview(self.profile_frame, columns=columns, show="headings", height=12)
        for col in columns:
            self.profile_tree.heading(col, text=col)
            self.profile_tree.column(col, width=100 if col != "IPs" else 180)
        self.profile_tree.pack(fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(self.profile_frame, orient=tk.VERTICAL, command=self.profile_tree.yview)
        self.profile_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Double-click to edit
        self.profile_tree.bind("<Double-1>", lambda e: self.edit_profile())

    def populate_profile_list(self):
        """Fill treeview with profiles, filtered by search term."""
        for item in self.profile_tree.get_children():
            self.profile_tree.delete(item)
        search = self.search_entry.get().strip().lower()
        for p in self.profiles:
            if search and search not in p['profile_name'].lower():
                continue
            ip_display = ", ".join([ip for ip, _ in p.get('ip_entries', [])]) if p.get('ip_entries') else "DHCP" if p.get('dhcp') else "None"
            gateway = p.get('gateway', '')
            dns = f"{p.get('dns1','')}" + (f", {p.get('dns2','')}" if p.get('dns2') else "")
            self.profile_tree.insert("", tk.END, values=(
                p['profile_name'], p['adapter'],
                "Yes" if p.get('dhcp') else "No",
                ip_display, gateway, dns
            ))

    def get_selected_profile(self):
        selection = self.profile_tree.selection()
        if not selection:
            messagebox.showwarning("No selection", "Please select a profile from the list.")
            return None
        item = self.profile_tree.item(selection[0])
        profile_name = item['values'][0]
        for p in self.profiles:
            if p['profile_name'] == profile_name:
                return p
        return None

    def add_profile(self):
        try:
            dialog = ProfileDialog(self.root, adapters=list_adapters().keys())
            self.root.wait_window(dialog.dialog)
            if dialog.result:
                self.profiles.append(dialog.result)
                self.save_profiles()
                self.populate_profile_list()
                self.terminal.insert(tk.END, f"✅ Profile '{dialog.result['profile_name']}' added.\n")
                self.terminal.see(tk.END)
        except Exception:
            messagebox.showerror("Add profile failed", traceback.format_exc(), parent=self.root)

    def edit_profile(self):
        try:
            prof = self.get_selected_profile()
            if not prof:
                return
            dialog = ProfileDialog(self.root, adapters=list_adapters().keys(), edit_profile=prof)
            self.root.wait_window(dialog.dialog)
            if dialog.result:
                # Replace old with new
                idx = next((i for i, p in enumerate(self.profiles) if p['profile_name'] == prof['profile_name']), None)
                if idx is not None:
                    self.profiles[idx] = dialog.result
                self.save_profiles()
                self.populate_profile_list()
                self.terminal.insert(tk.END, f"✏️ Profile '{dialog.result['profile_name']}' updated.\n")
                self.terminal.see(tk.END)
        except Exception:
            messagebox.showerror("Edit profile failed", traceback.format_exc(), parent=self.root)

    def delete_profile(self):
        prof = self.get_selected_profile()
        if not prof:
            return
        if messagebox.askyesno("Confirm Delete", f"Delete profile '{prof['profile_name']}'?"):
            self.profiles = [p for p in self.profiles if p['profile_name'] != prof['profile_name']]
            self.save_profiles()
            self.populate_profile_list()
            self.terminal.insert(tk.END, f"🗑 Deleted profile '{prof['profile_name']}'\n")

    def apply_selected_profile(self):
        prof = self.get_selected_profile()
        if not prof:
            return
        self.terminal.insert(tk.END, f"\n--- Applying profile '{prof['profile_name']}' ---\n")
        apply_profile(prof, self.terminal)
        self.refresh_cards_list()   # update card info after changes

    def save_profiles(self):
        save_profiles_to_csv(self.profiles, self.csv_path)
        self.terminal.insert(tk.END, f"💾 Profiles saved to {self.csv_path}\n")
        self.terminal.see(tk.END)

    def load_profiles(self):
        self.profiles = load_profiles_from_csv(self.csv_path)

    # --------------------------------------------------------
    # NETWORK CARDS TAB
    # --------------------------------------------------------
    def build_cards_tab(self):
        top = ttk.Frame(self.cards_frame)
        top.pack(fill=tk.X, pady=5)
        ttk.Button(top, text="🔄 Refresh Cards", command=self.refresh_cards_list).pack(side=tk.RIGHT)

        columns = ("Adapter", "Status", "IP Addresses", "Settings")
        self.cards_tree = ttk.Treeview(self.cards_frame, columns=columns, show="headings", height=15)
        for col in columns:
            self.cards_tree.heading(col, text=col)
            self.cards_tree.column(col, width=150 if col != "IP Addresses" else 250)
        self.cards_tree.pack(fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(self.cards_frame, orient=tk.VERTICAL, command=self.cards_tree.yview)
        self.cards_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.refresh_cards_list()

    def refresh_cards_list(self):
        for item in self.cards_tree.get_children():
            self.cards_tree.delete(item)
        adapters = list_adapters()
        for name, info in adapters.items():
            ips = ", ".join(info['ips']) if info['ips'] else "No IP"
            mode = info.get('dhcp', "Unknown")
            status = "✅ " + info['status'] if info['status'] == "Connected" else "❌ " + info['status']
            self.cards_tree.insert("", tk.END, values=(name, status, ips, mode))

    # --------------------------------------------------------
    # INFO TAB
    # --------------------------------------------------------
    def build_info_tab(self):
        info_text = """🧠 LAN Profile Manager - Senior Design

• Create profiles with MULTIPLE static IPs per adapter
• Save/load CSV anywhere
• Apply any profile (including DHCP)
• Auto-run as Administrator
• Live terminal shows all netsh commands
• Global profile search

💡 Usage:
- Add Profile → set adapter, IPs (click [+ Add IP])
- Apply → configuration changes immediately
- Network Cards tab shows current state
"""
        lbl = ttk.Label(self.info_frame, text=info_text, justify=tk.LEFT, font=("Segoe UI", 9))
        lbl.pack(padx=10, pady=10, anchor=tk.W)

    # --------------------------------------------------------
    # IP SCANNER TAB
    # --------------------------------------------------------
    def build_scanner_tab(self):
        self.scanner_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.insert(2, self.scanner_frame, text="📡 IP Scanner")

        # Top Bar
        top = ttk.Frame(self.scanner_frame)
        top.pack(fill=tk.X, pady=(0, 5))
        
        ttk.Label(top, text="Subnet:").pack(side=tk.LEFT, padx=2)
        self.subnet_combo = ttk.Combobox(top, width=20, values=self.detect_all_subnets())
        self.subnet_combo.pack(side=tk.LEFT, padx=5)
        if self.subnet_combo['values']: self.subnet_combo.current(0)

        ttk.Button(top, text="Detect", command=self.refresh_subnets).pack(side=tk.LEFT, padx=2)
        self.start_btn = ttk.Button(top, text="Start Scan", command=self.start_scan)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        self.stop_btn = ttk.Button(top, text="Stop Scan", command=self.stop_scan, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        
        # Smart Search
        ttk.Label(top, text="  🔍 AI Search:").pack(side=tk.LEFT, padx=5)
        self.scan_search_entry = ttk.Entry(top, width=20)
        self.scan_search_entry.pack(side=tk.LEFT, padx=5)
        self.scan_search_entry.bind("<KeyRelease>", lambda e: self.filter_scanner_results())

        ttk.Button(top, text="Export CSV", command=self.export_scan_results).pack(side=tk.RIGHT, padx=2)
        ttk.Button(top, text="Refresh ARP", command=self.refresh_arp_only).pack(side=tk.RIGHT, padx=2)

        # Progress & Status
        stat_frame = ttk.Frame(self.scanner_frame)
        stat_frame.pack(fill=tk.X, pady=2)
        self.scan_progress = ttk.Progressbar(stat_frame, orient=tk.HORIZONTAL, mode='determinate')
        self.scan_progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self.scan_status_lbl = ttk.Label(stat_frame, text="Ready", width=25)
        self.scan_status_lbl.pack(side=tk.RIGHT)

        # Treeview
        cols = ("IP Address", "MAC Address", "Hostname", "Vendor", "Device Type", "Response Time", "Open Ports", "Shared")
        self.scanner_tree = ttk.Treeview(self.scanner_frame, columns=cols, show="headings", height=10)
        for col in cols:
            self.scanner_tree.heading(col, text=col)
            self.scanner_tree.column(col, width=80 if "IP" in col or "Time" in col else 110)
        self.scanner_tree.pack(fill=tk.BOTH, expand=True)

        # Right-click menu for Management Actions
        self.scan_menu = tk.Menu(self.root, tearoff=0)
        self.scan_menu.add_command(label="🖥️ Remote Desktop (RDP)", command=self.action_rdp)
        self.scan_menu.add_command(label="🌐 Open in Browser (HTTP)", command=lambda: self.action_web(80))
        self.scan_menu.add_command(label="🔒 Open in Browser (HTTPS)", command=lambda: self.action_web(443))
        self.scan_menu.add_command(label="📁 Browse Shared Folders", command=self.action_browse_shares)
        self.scan_menu.add_separator()
        self.scan_menu.add_command(label="🎧 Remote Control (NetSupport)", command=self.action_netsupport)
        self.scan_menu.add_command(label="⚡ Send Wake-on-LAN (WOL)", command=self.wol_selected)
        
        self.scanner_tree.bind("<Button-3>", lambda e: self.scan_menu.post(e.x_root, e.y_root) if self.scanner_tree.identify_row(e.y) else None)

        self.found_lbl = ttk.Label(self.scanner_frame, text="0 devices found", font=("Segoe UI", 9, "italic"))
        self.found_lbl.pack(anchor=tk.W, pady=2)
        
        # Load History
        self.all_scan_results = []
        self.load_scan_history()

    def get_local_mac_fallback(self):
        try:
            node = uuid.getnode()
            return ':'.join(re.findall('..', '%012X' % node)).upper()
        except: return "Unknown"

    def detect_all_subnets(self):
        """Returns a list of all active IPv4 subnets (including Hotspots)."""
        subnets = []
        try:
            # Get all non-loopback IPv4 addresses and their prefix lengths
            ps_cmd = "powershell -NoProfile -Command \"Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.InterfaceAlias -notmatch 'Loopback' } | Select-Object IPAddress, PrefixLength | ConvertTo-Json\""
            proc = subprocess.run(ps_cmd, shell=True, capture_output=True, text=True)
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                rows = data if isinstance(data, list) else [data]
                for row in rows:
                    ip = row.get("IPAddress")
                    prefix = row.get("PrefixLength")
                    if ip and prefix:
                        net = ipaddress.IPv4Interface(f"{ip}/{prefix}").network
                        s = str(net)
                        if s not in subnets: subnets.append(s)
        except: pass
        if not subnets: subnets = ["192.168.1.0/24"]
        return subnets

    def refresh_subnets(self):
        subs = self.detect_all_subnets()
        self.subnet_combo['values'] = subs
        if subs: self.subnet_combo.current(0)
        self.terminal.insert(tk.END, f"🔍 Detected {len(subs)} subnets.\n")

    def detect_subnet(self):
        # Compatibility for other calls
        return self.detect_all_subnets()[0]

    def start_scan(self):
        cidr = self.subnet_combo.get().strip()
        try:
            net = ipaddress.IPv4Network(cidr, strict=False)
        except Exception as e:
            messagebox.showerror("Invalid Subnet", str(e))
            return

        self.scan_running = True
        self.stop_event.clear()
        self.all_scan_results = [] # Store all for smart searching
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        for item in self.scanner_tree.get_children(): self.scanner_tree.delete(item)
        
        self.terminal.insert(tk.END, f"\n--- Starting IP Scan: {cidr} ---\n")
        self.terminal.see(tk.END)
        threading.Thread(target=self._scan_thread, args=(net,), daemon=True).start()

    def stop_scan(self):
        self.scan_running = False
        self.stop_event.set()
        self.terminal.insert(tk.END, "⚠ Scanning stopped by user.\n")
        self.terminal.see(tk.END)

    def _scan_thread(self, network):
        hosts = list(network.hosts())
        total = len(hosts)
        self.root.after(0, lambda: self.scan_progress.configure(maximum=total, value=0))
        self.root.after(0, lambda: self.scan_status_lbl.config(text="Scanning..."))
        
        q = queue.Queue()
        subnet_ips = set()
        for h in hosts: 
            q.put(str(h))
            subnet_ips.add(str(h))
        
        results_found = 0
        scanned_count = 0
        threads = []
        max_threads = 100
        found_ips = set()

        def worker():
            nonlocal results_found, scanned_count
            while not q.empty() and not self.stop_event.is_set():
                ip = q.get()
                ping_time = self.ping_host(ip)
                mac = self.get_mac_from_arp(ip)
                
                # Device is alive if it responds to ping OR is found in ARP table
                if ping_time or (mac and mac != "Unknown"):
                    host = self.resolve_hostname(ip)
                    vendor = self.lookup_vendor(mac)
                    ports = self.port_scan(ip) if results_found < 50 else "-"
                    
                    # Phase 2: Inventory Probes
                    fingerprint = ""
                    if results_found < 30: # Limit deep probes to keep scan fast
                        snmp = self.snmp_probe(ip)
                        ssdp = self.ssdp_probe(ip)
                        if snmp: fingerprint += f"SNMP: {snmp} "
                        if ssdp:
                            server_match = re.search(r"Server:\s*(.*)", ssdp, re.I)
                            if server_match: fingerprint += f"SSDP: {server_match.group(1).strip()}"
                    
                    if "445" in ports or "139" in ports:
                        shared = self.get_smb_shares(ip)
                    else:
                        shared = "❌" if ports != "-" else "?"
                        
                    res_display = f"{ping_time}ms" if ping_time else "N/A (ARP)"
                    
                    # Advanced Classification
                    dtype = self.classify_device(ip, mac, host, vendor, ports, shared, fingerprint)
                    
                    # FILTER: Only show devices with a MAC or if it's the gateway/local
                    if mac != "Unknown" or "GATEWAY" in dtype.upper() or ip == self.detect_subnet().split('/')[0]:
                        res_data = (ip, mac, host, vendor, dtype, res_display, ports, shared, fingerprint)
                        self.all_scan_results.append(res_data)
                        self.root.after(0, self._add_scan_result, res_data)
                        results_found += 1
                    found_ips.add(ip)
                
                scanned_count += 1
                # Update progress bar and label
                self.root.after(0, lambda c=scanned_count: self.update_scan_progress(c, total))
                q.task_done()

        for _ in range(min(max_threads, total)):
            t = threading.Thread(target=worker, daemon=True)
            t.start()
            threads.append(t)

        for t in threads: t.join()
        
        # ARP Sweep Cleanup (Find devices that didn't respond to ping but are in ARP table)
        if not self.stop_event.is_set():
            try:
                out = subprocess.check_output("arp -a", shell=True, text=True)
                for line in out.splitlines():
                    match = re.search(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})\s+(dynamic|static)", line, re.IGNORECASE)
                    if match:
                        arp_ip = match.group(1)
                        mac = match.group(0).split()[1].upper().replace("-", ":")
                        if arp_ip in subnet_ips and arp_ip not in found_ips:
                            host = self.resolve_hostname(arp_ip)
                            vendor = self.lookup_vendor(mac)
                            ports = self.port_scan(arp_ip) if results_found < 50 else "-"
                            if "445" in ports or "139" in ports:
                                shared = self.get_smb_shares(arp_ip)
                            else:
                                shared = "❌" if ports != "-" else "?"
                            
                            dtype = self.classify_device(arp_ip, mac, host, vendor, ports, shared)
                            
                            # FILTER: Only show if MAC is known
                            if mac != "Unknown":
                                res_data = (arp_ip, mac, host, vendor, dtype, "N/A (ARP)", ports, shared, "")
                                self.all_scan_results.append(res_data)
                                self.root.after(0, self._add_scan_result, res_data)
                                results_found += 1
                            found_ips.add(arp_ip)
            except: pass
        
        self.scan_running = False
        self.root.after(0, self._finish_scan, results_found)
        self.save_scan_history()

    def update_scan_progress(self, current, total):
        """Update both progress bar and status label."""
        self.scan_progress.configure(value=current)
        self.scan_status_lbl.config(text=f"Scanning: {current} / {total}")

    def filter_scanner_results(self):
        """AI Smart Search logic to filter the treeview."""
        query = self.scan_search_entry.get().strip().lower()
        
        # Clear tree
        for item in self.scanner_tree.get_children():
            self.scanner_tree.delete(item)
            
        # Refill based on query
        for data in self.all_scan_results:
            # Smart match: check if query is in ANY of the data fields
            if any(query in str(field).lower() for field in data):
                self.scanner_tree.insert("", tk.END, values=data)

    def _add_scan_result(self, data):
        self.scanner_tree.insert("", tk.END, values=data)
        self.found_lbl.config(text=f"{len(self.scanner_tree.get_children())} devices found")
        self.terminal.insert(tk.END, f"[+] Found: {data[0]} ({data[2]})\n")
        self.terminal.see(tk.END)

    def _finish_scan(self, count):
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.scan_status_lbl.config(text="Finished")
        self.terminal.insert(tk.END, f"Done. {count} devices identified.\n")
        self.terminal.see(tk.END)

    def ping_host(self, ip):
        """Ping a host and return response time in ms, or None if down."""
        try:
            # Force ARP lookup (vital for mobile/hotspot devices)
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(0.01)
                    s.sendto(b'', (ip, 65432))
            except: pass

            cmd = f"ping -n 1 -w 1200 {ip}"
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if "Reply from" in proc.stdout:
                match = re.search(r"time[= <]+(\d+)\s*ms", proc.stdout, re.IGNORECASE)
                if match: return match.group(1)
        except: pass
        return None

    def get_mac_from_arp(self, ip):
        try:
            out = subprocess.check_output(f"arp -a {ip}", shell=True, text=True)
            for line in out.splitlines():
                if ip in line:
                    match = re.search(r"([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})", line)
                    if match: return match.group(0).upper().replace("-", ":")
        except: pass
        return "Unknown"

    def get_smb_shares(self, ip):
        try:
            # Check for shared folders using net view
            proc = subprocess.run(f"net view \\\\{ip}", shell=True, capture_output=True, text=True, timeout=2)
            if proc.returncode == 0:
                shares = []
                in_shares = False
                for line in proc.stdout.splitlines():
                    if line.startswith("----"):
                        in_shares = True
                        continue
                    if in_shares and line.strip() and not line.startswith("The command completed"):
                        share_name = line.split()[0]
                        shares.append(share_name)
                return ", ".join(shares) if shares else "✅ (Hidden)"
            else:
                return "✅ (Secured)"
        except:
            return "✅"

    def snmp_probe(self, ip):
        # SNMPv1 GET sysDescr.0 with community 'public'
        packet = b'\x30\x26\x02\x01\x00\x04\x06\x70\x75\x62\x6c\x69\x63\xa0\x19\x02\x04\x00\x00\x00\x01\x02\x01\x00\x02\x01\x00\x30\x0b\x30\x09\x06\x05\x2b\x06\x01\x02\x01\x01\x00'
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.6)
            sock.sendto(packet, (ip, 161))
            data, addr = sock.recvfrom(4096)
            if b'\x04' in data:
                parts = data.split(b'\x04')
                return parts[-1][1:].decode('utf-8', errors='ignore').strip()
        except: pass
        return ""

    def ssdp_probe(self, ip):
        msg = 'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: ssdp:all\r\n\r\n'
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.6)
            sock.sendto(msg.encode(), (ip, 1900))
            data, addr = sock.recvfrom(4096)
            return data.decode('utf-8', errors='ignore')
        except: pass
        return ""

    def classify_device(self, ip, mac, hostname, vendor, ports, shares, fingerprint=""):
        h = str(hostname).upper()
        v = str(vendor).upper()
        p = str(ports)
        f = str(fingerprint).upper()
        
        # 1. Infrastructure (Router/Switch)
        if any(x in f for x in ["SWITCH", "BRIDGE", "ROUTER"]): return "🌐 Switch/Router"
        if "UBIQUITI" in v or "UBNT" in h or "NANOSTATION" in h or "AIRMAX" in f:
            return "📡 NanoStation/AP"
        if "MIKROTIK" in v or "CISCO" in v: return "🌐 Infrastructure"
        
        # 2. Printers
        if any(x in v for x in ["HP", "EPSON", "CANON", "BROTHER", "XEROX", "KYOCERA"]):
            return "🖨️ Printer"
        if any(x in p for x in ["9100", "515", "631"]):
            return "🖨️ Printer"
        if "PRINTER" in h or "PRINTS$" in str(shares).upper() or "PRINTER" in f:
            return "🖨️ Printer"
            
        # 3. Cameras & IoT
        if any(x in f for x in ["CAMERA", "DVR", "NVR", "AXIS", "HIKVISION"]): return "📹 IP Camera"
        if any(x in v for x in ["HIKVISION", "DAHUA", "AXIS"]): return "📹 IP Camera"
        if "IOT" in f or "SMART" in f: return "🏠 IoT Device"
            
        # 4. Mobile Devices
        if any(x in v for x in ["APPLE", "SAMSUNG", "HUAWEI", "XIAOMI"]):
            return "📱 Smartphone"
            
        # 5. Computers
        if any(x in h for x in ["-PC", "DESKTOP-", "LAPTOP-"]):
            return "💻 Computer"
        if "3389" in p or "445" in p:
            return "💻 Windows PC"
            
        return "❓ Generic Device"

    def resolve_hostname(self, ip):
        try:
            # Try basic lookup
            name = socket.getfqdn(ip)
            if name and name != ip:
                return name
            # Fallback to gethostbyaddr
            return socket.gethostbyaddr(ip)[0]
        except: return "Unknown"

    def lookup_vendor(self, mac):
        if not mac or mac == "Unknown": return "Unknown"
        prefix = mac[:8].upper()
        return OUI_DICT.get(prefix, "Unknown")

    def port_scan(self, ip, ports=[80, 443, 22, 445]):
        open_p = []
        for p in ports:
            if self.stop_event.is_set(): break
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex((ip, p)) == 0: open_p.append(str(p))
        return ", ".join(open_p) if open_p else "-"

    def wol_selected(self):
        sel = self.scanner_tree.selection()
        if not sel: return
        mac = self.scanner_tree.item(sel[0])['values'][1]
        if mac == "Unknown":
            messagebox.showwarning("WOL", "Cannot send WOL: MAC address unknown.")
            return
        self.send_wol(mac)
        self.terminal.insert(tk.END, f"⚡ WOL Magic Packet sent to {mac}\n")
        self.terminal.see(tk.END)

    def send_wol(self, mac):
        try:
            clean_mac = mac.replace(":", "").replace("-", "")
            data = bytes.fromhex("F" * 12 + clean_mac * 16)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(data, ('255.255.255.255', 9))
        except Exception as e:
            messagebox.showerror("WOL Error", str(e))

    def refresh_arp_only(self):
        for item in self.scanner_tree.get_children():
            ip = self.scanner_tree.item(item)['values'][0]
            mac = self.get_mac_from_arp(ip)
            vendor = self.lookup_vendor(mac)
            self.scanner_tree.set(item, column="MAC Address", value=mac)
            self.scanner_tree.set(item, column="Vendor", value=vendor)

    def export_scan_results(self):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_filename = f"scan_res_{timestamp}.csv"
        json_filename = f"scan_res_{timestamp}.json"
        
        try:
            # Export CSV
            with open(csv_filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                header = ["IP", "MAC", "Hostname", "Vendor", "Device Type", "Response", "Ports", "Shared", "Fingerprint"]
                writer.writerow(header)
                for data in self.all_scan_results:
                    writer.writerow(data)
            
            # Export JSON
            json_data = []
            for data in self.all_scan_results:
                json_data.append({
                    "ip": data[0], "mac": data[1], "hostname": data[2], 
                    "vendor": data[3], "role": data[4], "latency": data[5],
                    "ports": data[6], "shares": data[7], "fingerprint": data[8],
                    "timestamp": datetime.now().isoformat()
                })
            with open(json_filename, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=4)
                
            messagebox.showinfo("Exported", f"Saved Inventory to:\n{csv_filename}\n{json_filename}")
        except Exception as e:
            messagebox.showerror("Export Failed", str(e))

    def save_scan_history(self):
        try:
            with open(self.history_path, 'w', encoding='utf-8') as f:
                json.dump(self.all_scan_results, f, indent=4)
        except: pass

    def load_scan_history(self):
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, 'r', encoding='utf-8') as f:
                    self.all_scan_results = json.load(f)
                for data in self.all_scan_results:
                    self.scanner_tree.insert("", tk.END, values=data)
                self.found_lbl.config(text=f"{len(self.all_scan_results)} devices loaded from history")
                self.scan_status_lbl.config(text="History Loaded")
            except: pass

    # --- ADMIN QUICK ACTIONS ---
    def action_rdp(self):
        sel = self.scanner_tree.selection()
        if not sel: return
        ip = self.scanner_tree.item(sel[0])['values'][0]
        self.terminal.insert(tk.END, f"🚀 Launching RDP to {ip}...\n")
        self.terminal.see(tk.END)
        subprocess.Popen(f"mstsc /v:{ip}", shell=True)

    def action_web(self, port=80):
        sel = self.scanner_tree.selection()
        if not sel: return
        ip = self.scanner_tree.item(sel[0])['values'][0]
        protocol = "https" if port == 443 else "http"
        url = f"{protocol}://{ip}"
        self.terminal.insert(tk.END, f"🌐 Opening {url} in browser...\n")
        self.terminal.see(tk.END)
        os.startfile(url)

    def action_netsupport(self):
        sel = self.scanner_tree.selection()
        if not sel: return
        ip = self.scanner_tree.item(sel[0])['values'][0]
        self.terminal.insert(tk.END, f"🎧 Launching NetSupport Control to {ip}...\n")
        self.terminal.see(tk.END)
        # Attempt to launch NetSupport (standard executable is pci.exe)
        subprocess.Popen(f"pci.exe /v:{ip}", shell=True)

    def action_browse_shares(self):
        sel = self.scanner_tree.selection()
        if not sel: return
        ip = self.scanner_tree.item(sel[0])['values'][0]
        unc_path = f"\\\\{ip}"
        self.terminal.insert(tk.END, f"📁 Opening Shared Folders for {ip}...\n")
        self.terminal.see(tk.END)
        subprocess.Popen(f"explorer {unc_path}", shell=True)


# ------------------------------------------------------------
# DIALOG FOR ADD/EDIT PROFILE
# ------------------------------------------------------------
class ProfileDialog:
    def __init__(self, parent, adapters, edit_profile=None):
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Edit Profile" if edit_profile else "Add New Profile")
        self.dialog.geometry("550x500")
        self.dialog.transient(parent)
        self.dialog.grab_set()
        self.result = None

        self.adapters = adapters
        self.ip_entries = []   # list of (ip, mask)
        self.edit_mode = edit_profile is not None

        main = ttk.Frame(self.dialog, padding=10)
        main.pack(fill=tk.BOTH, expand=True)

        # Profile name
        ttk.Label(main, text="Profile Name:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.profile_name = ttk.Entry(main, width=30)
        self.profile_name.grid(row=0, column=1, sticky=tk.W, padx=5)

        # Adapter
        ttk.Label(main, text="Network Adapter:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.adapter_combo = ttk.Combobox(main, values=list(adapters), state="readonly", width=28)
        self.adapter_combo.grid(row=1, column=1, sticky=tk.W, padx=5)
        if adapters:
            self.adapter_combo.current(0)

        # DHCP Checkbox
        self.dhcp_var = tk.BooleanVar()
        ttk.Checkbutton(main, text="DHCP (auto IP)", variable=self.dhcp_var, command=self.toggle_ip_frame).grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=5)

        # Frame for multiple IPs
        self.ip_frame = ttk.LabelFrame(main, text="Static IP Entries", padding=5)
        self.ip_frame.grid(row=3, column=0, columnspan=2, sticky=tk.NSEW, pady=5)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)

        self.ip_listbox = tk.Listbox(self.ip_frame, height=5, width=40)
        self.ip_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ip_btn_frame = ttk.Frame(self.ip_frame)
        ip_btn_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=5)
        ttk.Button(ip_btn_frame, text="➕ Add IP", command=self.add_ip).pack(pady=2)
        ttk.Button(ip_btn_frame, text="✖ Remove", command=self.remove_ip).pack(pady=2)

        # Gateway
        ttk.Label(main, text="Gateway:").grid(row=4, column=0, sticky=tk.W, pady=5)
        self.gateway_entry = ttk.Entry(main, width=30)
        self.gateway_entry.grid(row=4, column=1, sticky=tk.W, padx=5)

        # DNS
        ttk.Label(main, text="DNS Primary:").grid(row=5, column=0, sticky=tk.W)
        self.dns1 = ttk.Entry(main, width=30)
        self.dns1.grid(row=5, column=1, sticky=tk.W, padx=5)
        ttk.Label(main, text="DNS Secondary:").grid(row=6, column=0, sticky=tk.W)
        self.dns2 = ttk.Entry(main, width=30)
        self.dns2.grid(row=6, column=1, sticky=tk.W, padx=5)

        # Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.grid(row=7, column=0, columnspan=2, pady=15)
        ttk.Button(btn_frame, text="Save", command=self.save).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.dialog.destroy).pack(side=tk.LEFT)

        if self.edit_mode:
            self.load_profile(edit_profile)

        self.toggle_ip_frame()

    def _open_add_ip_dialog(self):
        win = tk.Toplevel(self.dialog)
        win.title("Add IP")
        win.transient(self.dialog)
        win.grab_set()
        win.resizable(False, False)

        frame = ttk.Frame(win, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="IP Address:").grid(row=0, column=0, sticky=tk.W, pady=(0, 6))
        ip_var = tk.StringVar()
        ip_entry = ttk.Entry(frame, textvariable=ip_var, width=26)
        ip_entry.grid(row=0, column=1, sticky=tk.W, pady=(0, 6))

        ttk.Label(frame, text="Subnet Mask:").grid(row=1, column=0, sticky=tk.W, pady=(0, 6))
        mask_var = tk.StringVar()
        mask_entry = ttk.Entry(frame, textvariable=mask_var, width=26)
        mask_entry.grid(row=1, column=1, sticky=tk.W, pady=(0, 6))

        hint = ttk.Label(frame, text="", foreground="#c00")
        hint.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        btns = ttk.Frame(frame)
        btns.grid(row=3, column=0, columnspan=2, sticky=tk.E)

        ok_btn = ttk.Button(btns, text="Add", command=lambda: None)
        ok_btn.pack(side=tk.RIGHT, padx=(6, 0))
        cancel_btn = ttk.Button(btns, text="Cancel", command=win.destroy)
        cancel_btn.pack(side=tk.RIGHT)

        def update_state(*_):
            ip = ip_var.get().strip()
            mask = mask_var.get().strip()

            if not ip and not mask:
                hint.config(text="")
                ok_btn.state(["disabled"])
                return

            if ip and not is_valid_ipv4(ip):
                hint.config(text="Invalid IP address (example: 192.168.1.10).")
                ok_btn.state(["disabled"])
                return

            if mask and not is_valid_netmask(mask):
                hint.config(text="Invalid subnet mask (example: 255.255.255.0 or /24).")
                ok_btn.state(["disabled"])
                return

            if ip and mask:
                hint.config(text="")
                ok_btn.state(["!disabled"])
            else:
                hint.config(text="Enter both IP address and subnet mask.")
                ok_btn.state(["disabled"])

        def on_ok():
            ip = ip_var.get().strip()
            mask = mask_var.get().strip()
            mask_norm = normalize_netmask(mask)
            if not (is_valid_ipv4(ip) and mask_norm and is_valid_netmask(mask_norm)):
                update_state()
                return
            self.ip_entries.append((ip, mask_norm))
            self.ip_listbox.insert(tk.END, f"{ip} / {mask_norm}")
            win.destroy()

        ok_btn.configure(command=on_ok)

        ip_var.trace_add("write", update_state)
        mask_var.trace_add("write", update_state)
        ok_btn.state(["disabled"])
        ip_entry.focus_set()

    def toggle_ip_frame(self):
        def set_enabled(widget, enabled: bool):
            # ttk widgets use .state(), tk widgets use config(state=...)
            try:
                if hasattr(widget, "state"):
                    widget.state(["!disabled"] if enabled else ["disabled"])
                    return
            except Exception:
                pass
            try:
                widget.config(state=(tk.NORMAL if enabled else tk.DISABLED))
            except Exception:
                pass

        enabled = not self.dhcp_var.get()
        for child in self.ip_frame.winfo_children():
            set_enabled(child, enabled)

    def add_ip(self):
        self._open_add_ip_dialog()

    def remove_ip(self):
        sel = self.ip_listbox.curselection()
        if sel:
            idx = sel[0]
            self.ip_listbox.delete(idx)
            del self.ip_entries[idx]

    def load_profile(self, prof):
        self.profile_name.insert(0, prof['profile_name'])
        if prof['adapter'] in self.adapters:
            self.adapter_combo.set(prof['adapter'])
        self.dhcp_var.set(prof.get('dhcp', False))
        self.gateway_entry.insert(0, prof.get('gateway', ''))
        self.dns1.insert(0, prof.get('dns1', ''))
        self.dns2.insert(0, prof.get('dns2', ''))
        for ip, mask in prof.get('ip_entries', []):
            self.ip_entries.append((ip, mask))
            self.ip_listbox.insert(tk.END, f"{ip} / {mask}")
        self.toggle_ip_frame()

    def save(self):
        try:
            pname = self.profile_name.get().strip()
            if not pname:
                messagebox.showerror("Error", "Profile name required.", parent=self.dialog)
                return
            adapter = self.adapter_combo.get().strip()
            if not adapter:
                messagebox.showerror("Error", "Select an adapter.", parent=self.dialog)
                return
            dhcp = self.dhcp_var.get()
            if not dhcp and not self.ip_entries:
                messagebox.showerror("Error", "At least one static IP required (or enable DHCP).", parent=self.dialog)
                return
            self.result = {
                'profile_name': pname,
                'adapter': adapter,
                'dhcp': dhcp,
                'ip_entries': self.ip_entries.copy(),
                'gateway': self.gateway_entry.get().strip(),
                'dns1': self.dns1.get().strip(),
                'dns2': self.dns2.get().strip()
            }
            self.dialog.destroy()
        except Exception:
            messagebox.showerror("Save failed", traceback.format_exc(), parent=self.dialog)


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = LANManagerApp(root)
        root.mainloop()
    except Exception:
        # Emergency logger to file if app crashes silently
        with open("crash_log.txt", "w") as f:
            f.write(traceback.format_exc())
        print(traceback.format_exc())