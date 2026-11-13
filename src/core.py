"""
Core engine: DNS server + fake connection test.

A tiny DNS server (UDP + TCP) plus a fake "connection test" web server.
Pure Python standard library - no dependencies.

Two modes:
  MODE_BLOCK_NINTENDO  Nintendo domains are blocked, everything else is
                       forwarded to a normal public resolver (internet works).
  MODE_OFFLINE         Every domain is blocked. Only the console's connection
                       test is answered, so the console thinks it is online
                       but nothing can actually load.

In both modes the connection test is answered locally by this PC, so no
request ever reaches Nintendo.
"""

import datetime
import json
import os
import socket
import socketserver
import struct
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "3.0.0"
APP_NAME = "NaviGuard"

MODE_BLOCK_NINTENDO = "block-nintendo"
MODE_OFFLINE = "offline"
MODES = (MODE_BLOCK_NINTENDO, MODE_OFFLINE)
MODE_LABELS = {
    MODE_BLOCK_NINTENDO: "Block Nintendo only (internet still works)",
    MODE_OFFLINE: "Offline (looks online, nothing loads)",
}

DEFAULT_UPSTREAMS = ["1.1.1.1", "9.9.9.9"]

# ---------------------------------------------------------------------------
# Blocking rules
# ---------------------------------------------------------------------------

# Any domain containing a label that STARTS with one of these is blocked.
# Catches nintendo.net, nintendo.com, nintendo.co.jp, nintendowifi.net,
# nintendoswitch.cn, nintendo-europe.com, ... and all of their subdomains.
BLOCK_LABEL_PREFIXES = ("nintendo",)

# Explicit list as well, in case a domain doesn't match the prefix rule.
BLOCK_SUFFIXES = {
    "nintendo.net", "nintendo.com", "nintendo.co.jp", "nintendowifi.net",
    "nintendoswitch.cn", "nintendoswitch.com.cn", "nintendo-europe.com",
    "nintendo.eu", "nintendo.de", "nintendo.fr", "nintendo.es", "nintendo.it",
    "nintendo.nl", "nintendo.co.uk", "nintendo.com.au", "nintendo.co.nz",
    "nintendo.co.kr", "nintendo.com.hk", "nintendo.tw", "nintendo.ru",
}

# The console's connection-test hosts. Instead of blocking them we point them
# at this PC and answer like Nintendo's test server would, so the console
# still considers itself online.
CONNTEST_HOSTS = {
    "ctest.cdn.nintendo.net",                 # Switch 1
    "conntest.nintendowifi.net",              # older consoles
    "ctest.cdn.n.nintendoswitch.cn",          # Switch 1 (China)
    "ctest.p01.ctest.srv.nintendo.net",       # Switch 2
}
# Also treated as a connection test: any Nintendo domain with a 'ctest' or
# 'conntest' label, so future test hosts keep working without an update.
CONNTEST_LABELS = ("ctest", "conntest")


def is_conntest(name):
    if name in CONNTEST_HOSTS:
        return True
    labels = name.split(".")
    nintendo = any(label.startswith(BLOCK_LABEL_PREFIXES) for label in labels)
    return nintendo and any(label in CONNTEST_LABELS for label in labels)

SINKHOLE_V4 = "127.0.0.1"
QTYPE_A = 1


def app_dir():
    """Folder holding the exe (frozen) or this script."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_domain_list(path):
    """One domain per line; blank lines and # comments ignored."""
    out = set()
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.split("#", 1)[0].strip().lower().rstrip(".")
            if line:
                out.add(line)
    return out


class Rules:
    def __init__(self, mode=MODE_BLOCK_NINTENDO, extra_block=(), extra_allow=()):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        self.mode = mode
        self.block = set(BLOCK_SUFFIXES) | set(extra_block)
        self.allow = set(extra_allow)

    @staticmethod
    def _matches(name, suffixes):
        return any(name == s or name.endswith("." + s) for s in suffixes)

    def classify(self, name):
        """'conntest', 'block' or 'allow' for a lowercase domain name."""
        name = name.lower().rstrip(".")
        if is_conntest(name):
            return "conntest"
        if self.mode == MODE_OFFLINE:
            return "block"
        if self._matches(name, self.allow):
            return "allow"
        if self._matches(name, self.block):
            return "block"
        if any(label.startswith(BLOCK_LABEL_PREFIXES) for label in name.split(".")):
            return "block"
        return "allow"


# ---------------------------------------------------------------------------
# DNS packets
# ---------------------------------------------------------------------------

def parse_question(packet):
    """(qname, qtype, end_of_question) or None if malformed."""
    if len(packet) < 12 or struct.unpack("!H", packet[4:6])[0] < 1:
        return None
    labels, i = [], 12
    while True:
        if i >= len(packet):
            return None
        length = packet[i]
        if length == 0:
            i += 1
            break
        if length & 0xC0:
            return None
        i += 1
        labels.append(packet[i:i + length].decode("ascii", "replace"))
        i += length
    if i + 4 > len(packet):
        return None
    qtype = struct.unpack("!H", packet[i:i + 2])[0]
    return ".".join(labels).lower(), qtype, i + 4


def build_answer(query, q_end, qtype, ipv4):
    """Answer an A query with `ipv4`; other types get an empty NOERROR."""
    tid = query[0:2]
    flags = struct.pack("!BB", 0x84 | (query[2] & 0x01), 0x80)
    question = query[12:q_end]
    if qtype == QTYPE_A:
        return (tid + flags + struct.pack("!HHHH", 1, 1, 0, 0) + question
                + b"\xc0\x0c" + struct.pack("!HHIH", QTYPE_A, 1, 60, 4)
                + socket.inet_aton(ipv4))
    return tid + flags + struct.pack("!HHHH", 1, 0, 0, 0) + question


def build_servfail(query):
    tid = query[0:2] if len(query) >= 2 else b"\x00\x00"
    return tid + b"\x81\x82" + b"\x00" * 8


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("connection closed")
        buf += chunk
    return buf


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def default_route_ip():
    """IP of the adapter Windows/Linux would use to reach the internet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # nothing is actually sent
            return s.getsockname()[0]
    except OSError:
        return None


_ADAPTER_PS = r"""
$out = foreach ($a in Get-NetAdapter | Where-Object { $_.Status -eq 'Up' }) {
  $ip = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Select-Object -First 1
  $gw = Get-NetRoute -InterfaceIndex $a.ifIndex -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue |
        Select-Object -First 1
  [pscustomobject]@{
    name = $a.Name; desc = $a.InterfaceDescription; mac = $a.MacAddress;
    wifi = ($a.PhysicalMediaType -match '802.11' -or $a.InterfaceDescription -match 'Wi-?Fi|Wireless');
    virtual = [bool]$a.Virtual -or ($a.InterfaceDescription -match 'Virtual|Hyper-V|VPN|TAP|Loopback|Bluetooth|Wi-Fi Direct');
    ip = $ip.IPAddress; prefix = $ip.PrefixLength; origin = [string]$ip.PrefixOrigin;
    gateway = $gw.NextHop
  }
}
@($out) | ConvertTo-Json -Compress
"""


def list_adapters():
    """Windows only: [{name, desc, wifi, virtual, ip, prefix, origin, gateway}]
    for adapters that are connected. origin is 'Dhcp' or 'Manual'."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _ADAPTER_PS],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            if d.get("gateway") in ("0.0.0.0", ""):
                d["gateway"] = None
        return data
    except Exception:
        return []


def list_interfaces():
    """[(adapter_name, ipv4)] for usable adapters. Adapter names are only
    available on Windows; elsewhere the name is ''."""
    result = [(a["name"], a["ip"]) for a in list_adapters() if a.get("ip")]
    if not result:
        ips = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
        r = default_route_ip()
        if r:
            ips.add(r)
        result = [("", ip) for ip in sorted(ips)]
    return [(n, ip) for n, ip in result
            if not ip.startswith(("127.", "169.254.")) and ip != "0.0.0.0"]


def ip_facing(client, interfaces):
    """Our IP on the same /24 as `client`, so the connection test reaches us
    no matter which adapter the console is on."""
    if client.startswith("127."):
        return "127.0.0.1"
    prefix = client.rsplit(".", 1)[0] + "."
    for _, ip in interfaces:
        if ip.startswith(prefix):
            return ip
    return default_route_ip() or "127.0.0.1"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        sock.sendto(self.server.engine.answer(data, self.client_address[0]),
                    self.client_address)


class _TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            while True:
                ln = struct.unpack("!H", _recv_exact(self.request, 2))[0]
                data = _recv_exact(self.request, ln)
                reply = self.server.engine.answer(data, self.client_address[0], tcp=True)
                self.request.sendall(struct.pack("!H", len(reply)) + reply)
        except (OSError, struct.error):
            pass


class _UDP(socketserver.ThreadingMixIn, socketserver.UDPServer):
    daemon_threads = True
    allow_reuse_address = False


class _TCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


class _HttpsWatcher(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Listens on 443 only to REPORT secure (HTTPS) connection-test attempts.
    HTTPS can't be faked without Nintendo's certificate, so this just logs
    which console tried it, then closes the connection."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"


class _HttpsWatchHandler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        client = self.client_address[0]
        if client not in srv.reported:
            srv.reported.add(client)
            srv.engine._event("info", client, "tried a secure (HTTPS) connection on port 443 - "
                                              "this can't be faked; please report it")


class _HTTP(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets two programs share a port silently;
    # we want a clear error instead.
    allow_reuse_address = os.name != "nt"


class _ConntestHandler(BaseHTTPRequestHandler):
    """Replies the way Nintendo's connection-test server does."""
    server_version = "nginx"
    sys_version = ""

    def _reply(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Organization", "Nintendo")
        self.end_headers()
        return body

    def do_GET(self):
        self.wfile.write(self._reply())
        host = self.headers.get("Host", "")
        self.server.engine._event("conntest_ok", self.client_address[0], f"{host}{self.path}")

    def do_HEAD(self):
        self._reply()

    def log_message(self, *args):
        pass


class StartError(Exception):
    """Raised with a human-friendly explanation when the server can't start."""


class Engine:
    """Start/stop-able DNS + connection-test server.

    on_event(kind, client, detail) is called from worker threads for every
    lookup. kind is one of: 'allow', 'block', 'conntest', 'conntest_ok', 'info'.
    """

    def __init__(self, mode=MODE_BLOCK_NINTENDO, upstreams=None, bind="0.0.0.0",
                 port=53, fake_conntest=True, conntest_ip=None,
                 blocklist=None, allowlist=None, on_event=None):
        here = app_dir()
        blocklist = blocklist or os.path.join(here, "blocklist.txt")
        allowlist = allowlist or os.path.join(here, "allowlist.txt")
        self.rules = Rules(mode, load_domain_list(blocklist), load_domain_list(allowlist))
        self.upstreams = upstreams or list(DEFAULT_UPSTREAMS)
        self.bind, self.port = bind, port
        self.fake_conntest = fake_conntest
        self.conntest_ip = conntest_ip
        self.on_event = on_event
        self.interfaces = list_interfaces()
        self.stats = {"allow": 0, "block": 0, "conntest": 0, "conntest_ok": 0}
        self._lock = threading.Lock()
        self._servers = []
        self.http_ok = False

    @property
    def mode(self):
        return self.rules.mode

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        try:
            udp = _UDP((self.bind, self.port), _UDPHandler)
        except OSError as e:
            raise StartError(_port_help(self.port, e))
        try:
            tcp = _TCP((self.bind, self.port), _TCPHandler)
        except OSError as e:
            udp.server_close()
            raise StartError(_port_help(self.port, e))
        udp.engine = tcp.engine = self
        self._servers = [udp, tcp]

        if self.fake_conntest:
            try:
                http = _HTTP((self.bind, 80), _ConntestHandler)
                http.engine = self
                self._servers.append(http)
                self.http_ok = True
            except OSError as e:
                self.http_ok = False
                self._event("info", "", "Port 80 is busy, so the connection test can't be "
                            f"answered ({e}). The console may say it is not connected. "
                            "Close any local web server (IIS, Skype, XAMPP) and restart.")

            try:
                https = _HttpsWatcher((self.bind, 443), _HttpsWatchHandler)
                https.engine = self
                https.reported = set()
                self._servers.append(https)
            except OSError:
                pass  # port 443 busy - only used for diagnostics

        for srv in self._servers:
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stop(self):
        for srv in self._servers:
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        self._servers = []

    # -- DNS ---------------------------------------------------------------
    def _event(self, kind, client, detail):
        with self._lock:
            if kind in self.stats:
                self.stats[kind] += 1
        if self.on_event:
            try:
                self.on_event(kind, client, detail)
            except Exception:
                pass

    def answer(self, packet, client, tcp=False):
        q = parse_question(packet)
        if q is None:
            return build_servfail(packet)
        name, qtype, q_end = q
        kind = self.rules.classify(name)
        if kind == "conntest" and not self.fake_conntest:
            kind = "block"
        self._event(kind, client, name)
        if kind == "block":
            return build_answer(packet, q_end, qtype, SINKHOLE_V4)
        if kind == "conntest":
            ip = self.conntest_ip or ip_facing(client, self.interfaces)
            return build_answer(packet, q_end, qtype, ip)
        return self._forward(packet, tcp)

    def _forward(self, packet, tcp):
        for up in self.upstreams:
            try:
                if tcp:
                    with socket.create_connection((up, 53), timeout=4) as s:
                        s.sendall(struct.pack("!H", len(packet)) + packet)
                        ln = struct.unpack("!H", _recv_exact(s, 2))[0]
                        return _recv_exact(s, ln)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.settimeout(3)
                    s.sendto(packet, (up, 53))
                    while True:
                        data, _ = s.recvfrom(65535)
                        if data[:2] == packet[:2]:
                            return data
            except (OSError, struct.error):
                continue
        return build_servfail(packet)


def _port_help(port, err):
    msg = f"Can't listen on port {port}: {err}\n\n"
    if os.name == "nt":
        msg += ("Something else is already using the DNS port. Usual causes:\n"
                " - Windows Mobile Hotspot is on (turn it off)\n"
                " - another copy of this program is already running\n"
                " - Internet Connection Sharing or another DNS program")
    else:
        msg += "Ports below 1024 need root: run with sudo."
    return msg


def timestamp():
    return datetime.datetime.now().strftime("%H:%M:%S")
