"""
Desktop app (dark mode). Everything happens here - no terminal needed.

Two connection types:
  * Spare router (automatic): the console joins a spare router's Wi-Fi and
    gets all its settings from this PC (built-in DHCP). Nothing to type on
    the console.
  * Home Wi-Fi (manual DNS): the console stays on the normal Wi-Fi and its
    DNS is set by hand to this PC.
"""

import ctypes
import ipaddress
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import core
import dhcp

try:  # system-tray support (bundled in the exe; optional when run from source)
    import pystray
    from PIL import Image as PILImage
except Exception:
    pystray = None

# ---------------------------------------------------------------- theme
BG = "#262626"        # dark grey
PANEL = "#2e2e2e"
PANEL2 = "#3a3a3a"
BORDER = "#474747"
TEXT = "#ececec"
MUTED = "#a3a3a3"
ACCENT = "#3d8ee8"    # blue
ACCENT_D = "#2c6fbd"
GREEN = "#4cc96f"
RED = "#e5534b"
AMBER = "#e8a33d"
FONT = "Segoe UI"
MONO = "Consolas"

CONN_ROUTER = "router"
CONN_HOME = "home"

SETTINGS_FILE = os.path.join(core.app_dir(), "settings.json")
MAX_LOG_LINES = 600
WATCHDOG_MS = 8000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resource(rel):
    base = getattr(sys, "_MEIPASS", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(base, rel)


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def is_link_local(ip):
    return not ip or ip.startswith("169.254.")


def choose_static_ip(adapter, adapters, router_ip=None):
    """Pick a fixed address for the router-side adapter. Prefer the router's
    own subnet (so its settings page stays reachable) unless that would clash
    with another adapter; otherwise use a private subnet nobody uses."""
    others = [ipaddress.ip_address(a["ip"]) for a in adapters
              if a is not adapter and a.get("ip") and not is_link_local(a["ip"])]
    gw = adapter.get("gateway") or router_ip
    if gw:
        try:
            net = ipaddress.ip_network(f"{gw}/24", strict=False)
            if not any(o in net for o in others):
                base = int(net.network_address)
                for host in (2, 3, 4):
                    cand = str(ipaddress.ip_address(base + host))
                    if cand != gw:
                        return cand, gw
        except ValueError:
            pass
    net = ipaddress.ip_network("10.77.0.0/24")
    if any(o in net for o in others):
        net = ipaddress.ip_network("10.78.0.0/24")
    return str(net.network_address + 1), gw


def run_elevated_netsh(args):
    """Run netsh as administrator (UAC prompt). Returns True on success."""
    ps_args = args.replace("'", "''")
    ps = (f"try {{ $p = Start-Process netsh -ArgumentList '{ps_args}' -Verb RunAs "
          f"-WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode }} catch {{ exit 1223 }}")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, creationflags=NO_WINDOW)
    return r.returncode == 0


def dark_title_bar(root):
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                break
    except Exception:
        pass


def apply_theme(root):
    root.configure(bg=BG)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL2,
                bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                troughcolor=PANEL, focuscolor=ACCENT, font=(FONT, 9))
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=PANEL, relief="flat")
    s.configure("TLabel", background=BG, foreground=TEXT)
    s.configure("Card.TLabel", background=PANEL, foreground=TEXT)
    s.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 8))
    s.configure("Warn.TLabel", background=PANEL, foreground=AMBER, font=(FONT, 8))
    s.configure("Head.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 8, "bold"))
    s.configure("Title.TLabel", background=BG, foreground=TEXT, font=(FONT, 14, "bold"))
    s.configure("Sub.TLabel", background=BG, foreground=MUTED, font=(FONT, 8))
    s.configure("Ip.TLabel", background=PANEL, foreground=ACCENT, font=(MONO, 16, "bold"))
    s.configure("Check.TLabel", background=PANEL, foreground=TEXT, font=(FONT, 9))

    for name in ("TRadiobutton", "TCheckbutton"):
        s.configure(name, background=PANEL, foreground=TEXT, indicatorbackground=PANEL2,
                    indicatorforeground=ACCENT, indicatorrelief="flat", font=(FONT, 9))
        s.map(name, background=[("active", PANEL)],
              indicatorbackground=[("selected", PANEL2), ("active", BORDER)],
              foreground=[("disabled", MUTED)])
    s.configure("Bg.TCheckbutton", background=BG)
    s.map("Bg.TCheckbutton", background=[("active", BG)])

    s.configure("TButton", background=PANEL2, foreground=TEXT, borderwidth=0,
                padding=(9, 4), font=(FONT, 9))
    s.map("TButton", background=[("disabled", PANEL), ("pressed", BORDER), ("active", BORDER)],
          foreground=[("disabled", MUTED)])
    s.configure("Card.TLabelframe", background=PANEL, bordercolor=BORDER, relief="solid",
                borderwidth=1)
    s.configure("Card.TLabelframe.Label", background=PANEL, foreground=ACCENT,
                font=(FONT, 9, "bold"))
    s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                font=(FONT, 11, "bold"), padding=(20, 6))
    s.map("Accent.TButton", background=[("pressed", ACCENT_D), ("active", "#5aa2ee")])
    s.configure("Stop.TButton", background=RED, foreground="#ffffff",
                font=(FONT, 11, "bold"), padding=(20, 6))
    s.map("Stop.TButton", background=[("pressed", "#c2443d"), ("active", "#ec6f68")])

    s.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=TEXT,
                arrowcolor=TEXT, bordercolor=BORDER, selectbackground=PANEL2,
                selectforeground=TEXT, padding=4)
    s.map("TCombobox", fieldbackground=[("readonly", PANEL2)],
          foreground=[("readonly", TEXT)], selectbackground=[("readonly", PANEL2)])
    root.option_add("*TCombobox*Listbox.background", PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    s.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=PANEL,
                arrowcolor=MUTED, bordercolor=PANEL)


class App:
    def __init__(self, root):
        self.root = root
        self.settings = load_settings()
        self.events = queue.Queue()
        self.engine = None
        self.dhcp = None
        self.adapters = []
        self.busy = False

        root.title(core.APP_NAME)
        sh = root.winfo_screenheight()
        h = max(520, min(660, sh - 90))
        root.geometry(f"600x{h}+{max(0, (root.winfo_screenwidth() - 600) // 2)}+30")
        root.minsize(540, 500)
        self.wrap_labels = []
        apply_theme(root)
        try:
            self.logo = tk.PhotoImage(file=resource("assets/logo-64.png"))
            root.iconphoto(True, self.logo)
        except tk.TclError:
            self.logo = None
        if os.name == "nt":
            try:
                root.iconbitmap(default=resource("assets/logo.ico"))
            except tk.TclError:
                pass

        outer = ttk.Frame(root, padding=(12, 8))
        outer.pack(fill="both", expand=True)
        self._header(outer)
        self._footer(outer)  # packed at the bottom first so it's never pushed off-screen
        self._mode_card(outer)
        self._conn_card(outer)
        self._run_card(outer)
        self._log_card(outer)
        root.bind("<Configure>", self._rewrap)

        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.tray = None
        self.tray_hint_shown = False
        self.ui_calls = queue.Queue()  # work handed over from the tray thread
        root.bind("<Unmap>", self._on_unmap)
        dark_title_bar(root)
        self.show_conn()
        self.refresh_adapters()
        root.after(120, self.pump)
        # Autostart waits until the first network scan has finished.
        self.pending_autostart = self.autostart.get()

    # ---------------------------------------------------------------- layout
    def wrap(self, label):
        self.wrap_labels.append(label)
        return label

    def _rewrap(self, event):
        if event.widget is self.root:
            w = max(260, event.width - 80)
            for lbl in self.wrap_labels:
                lbl.configure(wraplength=w)

    def card(self, parent, title):
        f = ttk.LabelFrame(parent, text=f" {title} ", style="Card.TLabelframe",
                           padding=(10, 2, 10, 6))
        f.pack(fill="x", pady=3)
        return f

    def _header(self, parent):
        h = ttk.Frame(parent)
        h.pack(fill="x", pady=(0, 6))
        if self.logo:
            self.logo_small = self.logo.subsample(2, 2)
            tk.Label(h, image=self.logo_small, bg=BG).pack(side="left", padx=(0, 8))
        t = ttk.Frame(h)
        t.pack(side="left", fill="x")
        ttk.Label(t, text=core.APP_NAME, style="Title.TLabel").pack(anchor="w")
        ttk.Label(t, text="Keeps your Switch away from Nintendo - while it thinks it's online.",
                  style="Sub.TLabel").pack(anchor="w")

    def _mode_card(self, parent):
        c = self.card(parent, "1. Mode")
        self.mode = tk.StringVar(value=self.settings.get("mode", core.MODE_OFFLINE))
        self.mode_btns = []
        for m, desc, warn in (
                (core.MODE_OFFLINE, "Safest. Everything is blocked; the console shows "
                                    "'connected' but can't reach anything.", None),
                (core.MODE_BLOCK_NINTENDO, "Only Nintendo is blocked, other sites work. "
                                           "Needs real internet (Home Wi-Fi).",
                 "⚠ Less safe: a game or app that skips DNS or uses a domain not on the "
                 "list could still reach Nintendo.")):
            b = ttk.Radiobutton(c, text=core.MODE_LABELS[m], value=m, variable=self.mode)
            b.pack(anchor="w")
            self.mode_btns.append(b)
            self.wrap(ttk.Label(c, text=desc, style="Muted.TLabel", wraplength=480)).pack(
                anchor="w", padx=(22, 0))
            if warn:
                self.wrap(ttk.Label(c, text=warn, style="Warn.TLabel", wraplength=480)).pack(
                    anchor="w", padx=(22, 0))
            ttk.Frame(c, style="Card.TFrame", height=3).pack()

    def _conn_card(self, parent):
        c = self.card(parent, "2. Connection")
        seg = ttk.Frame(c, style="Card.TFrame")
        seg.pack(anchor="w", pady=(0, 6))
        self.conn = tk.StringVar(value=self.settings.get("conn", CONN_ROUTER))
        self.conn_btns = []
        for val, label in ((CONN_ROUTER, "Spare router (automatic)"),
                           (CONN_HOME, "Home Wi-Fi (manual DNS)")):
            b = ttk.Radiobutton(seg, text=label, value=val, variable=self.conn,
                                command=self.show_conn)
            b.pack(side="left", padx=(0, 14))
            self.conn_btns.append(b)

        # --- spare router panel
        self.router_panel = ttk.Frame(c, style="Card.TFrame")
        row = ttk.Frame(self.router_panel, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="Cable to router is on:", style="Card.TLabel").pack(side="left")
        self.adapter_var = tk.StringVar()
        self.adapter_box = ttk.Combobox(row, textvariable=self.adapter_var, state="readonly",
                                        width=28)
        self.adapter_box.pack(side="left", padx=8)
        self.adapter_box.bind("<<ComboboxSelected>>", lambda e: self.update_checklist())
        ttk.Button(row, text="↻", width=3, command=self.refresh_adapters).pack(side="left")

        self.checks = {}
        cl = ttk.Frame(self.router_panel, style="Card.TFrame")
        cl.pack(fill="x", pady=(8, 4))
        for key in ("cable", "fixed", "dhcp"):
            lbl = ttk.Label(cl, text="", style="Check.TLabel")
            lbl.pack(anchor="w", pady=1)
            self.checks[key] = lbl

        btns = ttk.Frame(self.router_panel, style="Card.TFrame")
        btns.pack(fill="x", pady=(6, 0))
        self.setup_btn = ttk.Button(btns, text="Set up this PC  (admin)",
                                    command=self.setup_pc)
        self.setup_btn.pack(side="left")
        self.router_btn = ttk.Button(btns, text="Open router settings",
                                     command=self.open_router)
        self.router_btn.pack(side="left", padx=6)
        self.undo_btn = ttk.Button(btns, text="Undo PC setup", command=self.undo_pc)
        self.undo_btn.pack(side="right")
        self.wrap(ttk.Label(self.router_panel, style="Muted.TLabel", wraplength=480, justify="left",
                  text="One-time: cable from this PC to a router LAN port (INTERNET port empty), "
                       "turn the router's DHCP off, click 'Set up this PC'. Then the console "
                       "just joins the router's Wi-Fi.")).pack(anchor="w", pady=(6, 0))

        # --- home wifi panel
        self.home_panel = ttk.Frame(c, style="Card.TFrame")
        ttk.Label(self.home_panel, style="Card.TLabel",
                  text="On the console set Primary AND Secondary DNS to:").pack(anchor="w")
        self.home_ips = ttk.Frame(self.home_panel, style="Card.TFrame")
        self.home_ips.pack(fill="x", pady=4)
        self.wrap(ttk.Label(self.home_panel, style="Muted.TLabel", wraplength=480, justify="left",
                  text="Console: Internet Settings → your network → Change Settings → DNS → "
                       "Manual. Same address for both - never a public DNS.")).pack(anchor="w")
        self.wrap(ttk.Label(self.home_panel, style="Muted.TLabel", wraplength=480, justify="left",
                  text="DNS only - no DHCP, no router or PC changes needed.")).pack(
            anchor="w", pady=(4, 0))
        self.wrap(ttk.Label(self.home_panel, style="Warn.TLabel", wraplength=480, justify="left",
                  text="⚠ Less safe than the spare router: apps that skip DNS can still get "
                       "out. Not plug and play: if this PC's address changes, update the "
                       "console's DNS (tip: add a DHCP reservation for this PC in your home "
                       "router).")).pack(anchor="w")

    def _run_card(self, parent):
        c = self.card(parent, "3. Run")
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="Start", style="Accent.TButton", command=self.toggle)
        self.start_btn.pack(side="left")
        st = ttk.Frame(row, style="Card.TFrame")
        st.pack(side="left", padx=16, fill="x")
        self.status = tk.Label(st, text="●  Stopped", bg=PANEL, fg=MUTED, font=(FONT, 10, "bold"))
        self.status.pack(anchor="w")
        self.console_lbl = tk.Label(st, text="Console: not seen yet", bg=PANEL, fg=MUTED,
                                    font=(FONT, 9))
        self.console_lbl.pack(anchor="w")
        self.count_lbl = tk.Label(st, text="", bg=PANEL, fg=MUTED, font=(FONT, 8))
        self.count_lbl.pack(anchor="w")

    def _log_card(self, parent):
        f = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        f.pack(fill="both", expand=True, pady=4)
        self.log = tk.Text(f, height=5, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                           relief="flat", font=(MONO, 9), wrap="word", state="disabled",
                           padx=10, pady=8, borderwidth=0, highlightthickness=0)
        sb = ttk.Scrollbar(f, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for tag, col in (("block", RED), ("allow", MUTED), ("conntest", ACCENT),
                         ("conntest_ok", GREEN), ("dhcp", GREEN), ("info", AMBER)):
            self.log.tag_configure(tag, foreground=col)

    def _footer(self, parent):
        f = ttk.Frame(parent)
        f.pack(side="bottom", fill="x", pady=(4, 0))
        self.autostart = tk.BooleanVar(value=self.settings.get("autostart", False))
        ttk.Checkbutton(f, text="Start automatically when opened", variable=self.autostart,
                        style="Bg.TCheckbutton", command=self.persist).pack(side="left")
        self.hide_allowed = tk.BooleanVar(value=self.settings.get("hide_allowed", False))
        ttk.Checkbutton(f, text="Only show blocked", variable=self.hide_allowed,
                        style="Bg.TCheckbutton", command=self.persist).pack(side="left", padx=14)
        ttk.Label(f, text=f"v{core.VERSION}", foreground=MUTED).pack(side="right")

    # ------------------------------------------------------------- adapters
    def show_conn(self):
        if self.conn.get() == CONN_ROUTER:
            self.home_panel.pack_forget()
            self.router_panel.pack(fill="x")
        else:
            self.router_panel.pack_forget()
            self.home_panel.pack(fill="x")
        self.persist()

    def refresh_adapters(self):
        def work():
            ads = core.list_adapters()
            self.root.after(0, lambda: self._got_adapters(ads))
        threading.Thread(target=work, daemon=True).start()

    def _got_adapters(self, ads):
        self.adapters = ads
        wired = [a for a in ads if not a.get("virtual")]
        names = [f"{a['name']}  -  {a.get('ip') or 'no address'}" for a in wired]
        self.adapter_box["values"] = names
        want = self.settings.get("adapter")
        pick = None
        for i, a in enumerate(wired):
            if a["name"] == want:
                pick = i
        if pick is None:  # default: first wired (non Wi-Fi) adapter
            pick = next((i for i, a in enumerate(wired) if not a.get("wifi")), 0 if wired else None)
        if pick is not None:
            self.adapter_box.current(pick)
        self._wired = wired
        self.update_checklist()
        self._fill_home_ips()
        if getattr(self, "pending_autostart", False):
            self.pending_autostart = False
            self.start()

    def current_adapter(self):
        i = self.adapter_box.current()
        wired = getattr(self, "_wired", [])
        return wired[i] if 0 <= i < len(wired) else None

    def set_check(self, key, state, text):
        icon, col = {"ok": ("✔", GREEN), "bad": ("✖", RED), "warn": ("●", AMBER),
                     "todo": ("○", MUTED)}[state]
        self.checks[key].configure(text=f"{icon}  {text}", foreground=col if state != "ok" else TEXT)

    def update_checklist(self):
        a = self.current_adapter()
        if not a:
            self.set_check("cable", "bad", "No wired adapter is connected - plug the cable in, then ↻")
            self.set_check("fixed", "todo", "PC has a fixed address on the router network")
            self.set_check("dhcp", "todo", "Router's own DHCP is off (checked when you press Start)")
            return
        self.settings["adapter"] = a["name"]
        if a.get("wifi"):
            self.set_check("cable", "warn", f"{a['name']} is Wi-Fi - pick the adapter the router "
                                            "cable is plugged into")
        else:
            self.set_check("cable", "ok", f"Cable connected ({a['name']})")
        if a.get("origin") == "Manual" and not is_link_local(a.get("ip")):
            self.set_check("fixed", "ok", f"PC has a fixed address: {a['ip']}")
        else:
            self.set_check("fixed", "bad", "PC needs a fixed address on the router network - "
                                           "click 'Set up this PC'")
        if a.get("gateway"):
            self.settings["router_ip"] = a["gateway"]
        self.set_check("dhcp", "todo", "Router's own DHCP is off (checked when you press Start)")
        self.persist()

    def _fill_home_ips(self):
        for w in self.home_ips.winfo_children():
            w.destroy()
        rows = [(a["name"], a["ip"]) for a in self.adapters
                if a.get("ip") and not is_link_local(a["ip"]) and not a.get("virtual")]
        if not rows:
            ttk.Label(self.home_ips, text="No network connection found", style="Muted.TLabel").pack(anchor="w")
        for name, ip in rows:
            r = ttk.Frame(self.home_ips, style="Card.TFrame")
            r.pack(anchor="w")
            ttk.Label(r, text=ip, style="Ip.TLabel").pack(side="left")
            ttk.Label(r, text=f"   {name}", style="Muted.TLabel").pack(side="left")

    # ---------------------------------------------------------- PC set-up
    def setup_pc(self, confirm=True, then_start=False):
        a = self.current_adapter()
        if not a or a.get("wifi"):
            messagebox.showwarning(core.APP_NAME, "Choose the wired adapter the router cable is "
                                                  "plugged into first.")
            return
        ip, router_ip = choose_static_ip(a, self.adapters, self.settings.get("router_ip"))
        if self.settings.get("fixed_ip") and a.get("name") == self.settings.get("fixed_adapter"):
            ip = self.settings["fixed_ip"]  # reuse the same address as last time
        self.start_after_netsh = then_start
        if confirm and not messagebox.askokcancel(
                "Set up this PC",
                f"This gives the '{a['name']}' adapter the fixed address {ip} "
                f"(mask 255.255.255.0, no gateway), so this PC can run the router "
                f"network while its own internet keeps using Wi-Fi.\n\n"
                f"Windows will ask for administrator permission.\n"
                f"You can reverse it any time with 'Undo PC setup'."):
            return
        self.settings["router_ip"] = router_ip
        self.settings["fixed_ip"] = ip
        self.settings["fixed_adapter"] = a["name"]
        name = a["name"].replace('"', "")
        self._netsh(f'interface ipv4 set address name="{name}" source=static '
                    f'address={ip} mask=255.255.255.0 gateway=none',
                    f"PC set up: {a['name']} now uses {ip}")

    def undo_pc(self):
        a = self.current_adapter()
        if not a:
            return
        if not messagebox.askokcancel("Undo PC setup",
                                      f"Put '{a['name']}' back to automatic (DHCP) addressing?"):
            return
        name = a["name"].replace('"', "")
        self._netsh(f'interface ipv4 set address name="{name}" source=dhcp',
                    f"{a['name']} is back to automatic addressing")

    def _netsh(self, args, ok_msg):
        self.setup_btn.state(["disabled"])
        self.undo_btn.state(["disabled"])

        def work():
            ok = run_elevated_netsh(args)
            self.root.after(0, lambda: self._netsh_done(ok, ok_msg))
        threading.Thread(target=work, daemon=True).start()

    def _netsh_done(self, ok, ok_msg):
        self.setup_btn.state(["!disabled"])
        self.undo_btn.state(["!disabled"])
        if ok:
            self.add_log("info", "", ok_msg)
            self.root.after(2500, self.refresh_adapters)
            if getattr(self, "start_after_netsh", False):
                self.start_after_netsh = False
                self.root.after(5000, self.start)
        else:
            messagebox.showerror(core.APP_NAME, "That didn't work (administrator permission "
                                                "was declined or Windows refused the change).")

    def open_router(self):
        ip = self.settings.get("router_ip") or "192.168.0.1"
        webbrowser.open(f"http://{ip}/")

    # ------------------------------------------------------------ start/stop
    def toggle(self):
        if self.busy:
            return
        self.stop() if self.engine else self.start()

    def start(self):
        if self.engine or self.busy:
            return
        router = self.conn.get() == CONN_ROUTER
        a = self.current_adapter() if router else None
        if router:
            if not a or a.get("wifi"):
                messagebox.showwarning(core.APP_NAME, "Plug the router cable in and pick its "
                                                      "adapter first.")
                return
            if is_link_local(a.get("ip")) or a.get("origin") != "Manual":
                messagebox.showwarning(core.APP_NAME, "This PC doesn't have a fixed address on "
                                                      "the router network yet.\n\nClick 'Set up "
                                                      "this PC' first.")
                return
        self.busy = True
        self.start_btn.state(["disabled"])
        self.set_status("Starting...", AMBER)

        def work():
            other = dhcp.probe_other_dhcp(a["ip"]) if router else None
            self.root.after(0, lambda: self._start2(router, a, other))
        threading.Thread(target=work, daemon=True).start()

    def _start2(self, router, a, other_dhcp):
        self.busy = False
        self.start_btn.state(["!disabled"])
        mode = self.mode.get()
        engine = core.Engine(mode=mode, on_event=lambda *e: self.events.put(e))
        try:
            engine.start()
        except core.StartError as e:
            self.set_status("Stopped", MUTED)
            messagebox.showerror("Can't start", str(e))
            return
        self.engine = engine
        self.router_mode = router
        self.serving_ip = a["ip"] if router else None
        self.serving_adapter = a["name"] if router else None
        self.reprompted = False
        self.blocked_by = None
        if not router:
            self.check_home_ip()
        if router:
            if other_dhcp and other_dhcp != "unknown":
                self.blocked_by = other_dhcp
                self.serving_ip = None  # watchdog keeps re-checking until it's off
                self.set_check("dhcp", "bad", f"Router's DHCP is still ON ({other_dhcp}) - "
                                              "turn it off in the router settings")
                self.add_log("info", "", f"Another DHCP server ({other_dhcp}) answers on this "
                                         "network, so automatic setup is paused. Turn the "
                                         "router's DHCP off - this app notices within seconds.")
                self.root.after(300, lambda: messagebox.showwarning(
                    "Router DHCP is still on",
                    f"The router ({other_dhcp}) is still handing out addresses, so this app "
                    "can't do it for the console.\n\nClick 'Open router settings', find the "
                    "DHCP server option (often under Network / LAN settings), turn it OFF and "
                    "save. Keep this window running - it switches over by itself."))
            else:
                try:
                    self.dhcp = dhcp.DhcpServer(a["ip"], on_event=lambda *e: self.events.put(e))
                    self.dhcp.start()
                    self.set_check("dhcp", "ok", "Router's DHCP is off - this PC hands out the "
                                                 "settings")
                    if other_dhcp == "unknown":
                        self.add_log("info", "", "Couldn't double-check for another DHCP server.")
                except dhcp.DhcpError as e:
                    self.dhcp = None
                    self.serving_ip = None
                    self.add_log("info", "", str(e))
        for w in self.mode_btns + self.conn_btns:
            w.state(["disabled"])
        self.adapter_box.state(["disabled"])
        self.setup_btn.state(["disabled"])
        self.undo_btn.state(["disabled"])
        self.start_btn.configure(text="Stop", style="Stop.TButton")
        where = f"router network {a['ip']}" if router else "home Wi-Fi"
        self.set_status(f"Running  ·  {core.MODE_LABELS[mode]}", GREEN)
        self.console_lbl.configure(text="Console: waiting - join the router's Wi-Fi" if router
                                   else "Console: waiting - set its DNS to this PC", fg=MUTED)
        self.add_log("info", "", f"Started ({core.MODE_LABELS[mode]}, {where})")
        if not engine.http_ok:
            self.add_log("info", "", "Port 80 is busy - the console may say the connection "
                                     "test failed.")
        self.persist()
        self.root.after(WATCHDOG_MS, self.watchdog)

    # ------------------------------------------------------------- watchdog
    def watchdog(self):
        """Every few seconds: re-read the network and repair what changed."""
        if not self.engine:
            return

        def work():
            ads = core.list_adapters()
            self.root.after(0, lambda: self._watch(ads))
        threading.Thread(target=work, daemon=True).start()

    def _watch(self, ads):
        if not self.engine:
            return
        self.adapters = ads
        self.engine.interfaces = [(a["name"], a["ip"]) for a in ads
                                  if a.get("ip") and not is_link_local(a["ip"])]
        if self.router_mode:
            self._watch_router(ads)
        else:
            self.check_home_ip()
        self.root.after(WATCHDOG_MS, self.watchdog)

    def _watch_router(self, ads):
        a = next((x for x in ads if x["name"] == self.serving_adapter), None)
        ip = a.get("ip") if a else None
        if not a or is_link_local(ip):
            if self.serving_ip or self.dhcp:
                self.add_log("info", "", "Router cable disconnected - waiting for it to come "
                                         "back...")
                self.set_status("Waiting for router cable...", AMBER)
                if self.dhcp:
                    self.dhcp.stop()
                    self.dhcp = None
                self.serving_ip = None
            return
        if a.get("origin") != "Manual":
            # Something removed the fixed address. Fixing it needs admin, so ask once.
            if not self.reprompted:
                self.reprompted = True
                self.add_log("info", "", f"{a['name']} lost its fixed address (now {ip}).")
                if messagebox.askyesno(core.APP_NAME,
                                       f"'{a['name']}' lost its fixed address, so the console "
                                       "may stop connecting.\n\nFix it now? (Windows will ask "
                                       "for administrator permission.)"):
                    self.stop()
                    self.setup_pc(confirm=False, then_start=True)
            return
        if ip != self.serving_ip:
            old = self.serving_ip
            self.serving_ip = ip
            self.reprompted = False
            if old:
                self.add_log("info", "", f"PC address changed {old} → {ip} - restarting "
                                         "automatically")
            elif not self.blocked_by and not self.dhcp:
                self.add_log("info", "", f"Router network is back on {ip}")
            if self.dhcp:
                self.dhcp.stop()
                self.dhcp = None

            def work():
                other = dhcp.probe_other_dhcp(ip)
                self.root.after(0, lambda: self._restart_dhcp(ip, other))
            threading.Thread(target=work, daemon=True).start()

    def _restart_dhcp(self, ip, other):
        if not self.engine or ip != self.serving_ip:
            return
        if other and other != "unknown":
            self.set_check("dhcp", "bad", f"Router's DHCP is ON ({other}) - turn it off")
            if other != self.blocked_by:
                self.add_log("info", "", f"Another DHCP server ({other}) is answering - "
                                         "automatic setup paused until it's turned off.")
            self.blocked_by = other
            self.serving_ip = None  # try again on the next check
            return
        try:
            self.dhcp = dhcp.DhcpServer(ip, on_event=lambda *e: self.events.put(e))
            self.dhcp.start()
            self.set_status(f"Running  ·  {core.MODE_LABELS[self.engine.mode]}", GREEN)
            self.set_check("dhcp", "ok", "Router's DHCP is off - this PC hands out the settings")
            if self.blocked_by:
                self.add_log("dhcp", ip, "Router's DHCP is off now - automatic setup is ON")
                self.blocked_by = None
        except dhcp.DhcpError as e:
            self.add_log("info", "", str(e))
            self.serving_ip = None

    def check_home_ip(self):
        """Home Wi-Fi: the console's DNS is typed in by hand, so warn if our
        address changes (the router may hand this PC a new one)."""
        ips = sorted(a["ip"] for a in self.adapters
                     if a.get("ip") and not is_link_local(a["ip"]) and not a.get("virtual"))
        self._fill_home_ips()
        last = self.settings.get("home_ips")
        if last and ips and set(last) != set(ips):
            self.add_log("info", "", f"This PC's address changed ({', '.join(last)} → "
                                     f"{', '.join(ips)}). Update the console's DNS, or use the "
                                     "spare-router setup, which updates itself.")
            messagebox.showwarning(core.APP_NAME,
                                   "This PC's network address changed:\n\n"
                                   f"  was  {', '.join(last)}\n  now  {', '.join(ips)}\n\n"
                                   "Update the console's DNS to the new address.\nTip: the "
                                   "Spare router setup never needs this.")
        if ips:
            self.settings["home_ips"] = ips
            self.persist()

    def stop(self):
        if self.dhcp:
            self.dhcp.stop()
            self.dhcp = None
        if self.engine:
            self.engine.stop()
            self.engine = None
        for w in self.mode_btns + self.conn_btns:
            w.state(["!disabled"])
        self.adapter_box.state(["!disabled", "readonly"])
        self.setup_btn.state(["!disabled"])
        self.undo_btn.state(["!disabled"])
        self.start_btn.configure(text="Start", style="Accent.TButton")
        self.set_status("Stopped", MUTED)
        self.add_log("info", "", "Stopped")

    def set_status(self, text, col):
        self.status.configure(text=f"●  {text}", fg=col)

    # ----------------------------------------------------------------- tray
    def _on_unmap(self, event):
        """Minimize button -> hide the window and live in the system tray."""
        if event.widget is not self.root or pystray is None:
            return
        self.root.after(10, self._maybe_to_tray)

    def _maybe_to_tray(self):
        if self.root.state() != "iconic":
            return
        try:
            self._ensure_tray()
        except Exception as e:  # tray unavailable -> just stay minimized
            sys.stderr.write(f"tray unavailable: {e}\n")
            return
        self.root.withdraw()
        self.tray.visible = True
        if not self.tray_hint_shown:
            self.tray_hint_shown = True
            try:
                self.tray.notify("NaviGuard is still running here. Double-click the icon "
                                 "to open it.", core.APP_NAME)
            except Exception:
                pass

    def _ensure_tray(self):
        if self.tray:
            return
        with PILImage.open(resource("assets/logo-64.png")) as f:
            img = f.convert("RGBA")  # fully loaded now - the tray thread reads it later
        menu = pystray.Menu(
            pystray.MenuItem("Show NaviGuard", lambda: self.ui_calls.put(self.show_window),
                             default=True),
            pystray.MenuItem("Quit", lambda: self.ui_calls.put(self.quit)))
        self.tray = pystray.Icon("NaviGuard", img, self._tray_title(), menu)
        self.tray.run_detached()

    def _tray_title(self):
        return f"{core.APP_NAME} - " + ("running" if self.engine else "stopped")

    def show_window(self):
        if self.tray:
            self.tray.visible = False
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    # ------------------------------------------------------------------ log
    def pump(self):
        try:
            while True:
                self.ui_calls.get_nowait()()
        except queue.Empty:
            pass
        if getattr(self, "closing", False):
            return
        if self.tray:
            title = self._tray_title()
            if self.tray.title != title:
                self.tray.title = title
        try:
            for _ in range(200):
                kind, client, detail = self.events.get_nowait()
                if kind == "dhcp":
                    self.console_lbl.configure(text=f"Console got address {client}", fg=ACCENT)
                elif kind == "conntest_ok":
                    self.console_lbl.configure(
                        text=f"Console {client}: connection test PASSED  ({core.timestamp()})",
                        fg=GREEN)
                    detail = f"connection test answered ({detail}) - console thinks it's online"
                if kind == "allow" and self.hide_allowed.get():
                    continue
                self.add_log(kind, client, detail)
        except queue.Empty:
            pass
        if self.engine:
            s = self.engine.stats
            self.count_lbl.configure(text=f"blocked {s['block']}   allowed {s['allow']}")
        self.root.after(150, self.pump)

    def add_log(self, kind, client, detail):
        tag = {"allow": "allowed ", "block": "BLOCKED ", "conntest": "CONNTEST",
               "conntest_ok": "TEST OK ", "dhcp": "JOINED  ", "info": "NOTE    "}.get(kind, kind)
        if client:
            line = f"{core.timestamp()}  {tag}  {client:<15}  {detail}\n"
        else:
            line = f"{core.timestamp()}  {tag}  {detail}\n"
        self.log.configure(state="normal")
        self.log.insert("end", line, kind)
        extra = int(self.log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log.delete("1.0", f"{extra + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ----------------------------------------------------------------- misc
    def persist(self):
        self.settings.update(mode=self.mode.get(), conn=self.conn.get(),
                             autostart=self.autostart.get(),
                             hide_allowed=self.hide_allowed.get())
        save_settings(self.settings)

    def quit(self):
        if getattr(self, "closing", False):
            return
        self.closing = True
        self.persist()
        self.stop() if self.engine else None
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()


def setup_error_log():
    """A windowless exe has no console, so send errors to a log file next to
    the exe instead of losing them."""
    import traceback
    path = os.path.join(core.app_dir(), "naviguard-log.txt")
    if sys.stderr is None or sys.stdout is None:
        try:
            f = open(path, "a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or f
            sys.stderr = sys.stderr or f
        except OSError:
            import io
            sys.stdout = sys.stdout or io.StringIO()
            sys.stderr = sys.stderr or io.StringIO()

    def log_exc(kind, exc_type, exc, tb):
        try:
            sys.stderr.write(f"\n[{core.timestamp()}] {kind} error:\n")
            traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
            sys.stderr.flush()
        except Exception:
            pass

    sys.excepthook = lambda t, e, tb: log_exc("unhandled", t, e, tb)
    threading.excepthook = lambda a: log_exc(f"thread {a.thread.name if a.thread else ''}",
                                             a.exc_type, a.exc_value, a.exc_traceback)
    return log_exc


def main():
    log_exc = setup_error_log()
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    root.report_callback_exception = lambda t, e, tb: log_exc("callback", t, e, tb)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
