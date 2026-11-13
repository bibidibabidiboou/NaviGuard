"""
Minimal DHCP server for the spare-router setup.

When the spare router's own DHCP is switched off, this hands every device
that joins the router's Wi-Fi an address, with THIS PC as its DNS server and
gateway. The console can then stay on fully automatic network settings.

Safety:
  * It only listens on ONE adapter address (never on all adapters), so it
    can't answer on your home network.
  * Before starting it probes for another DHCP server on that network and
    refuses to run if one answers (e.g. the router's DHCP is still on, or the
    wrong adapter was picked).
"""

import ipaddress
import os
import random
import socket
import struct
import threading
import time

MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = 1, 2, 3, 4, 5, 6, 7, 8
OPT_MASK, OPT_ROUTER, OPT_DNS, OPT_HOSTNAME, OPT_BCAST = 1, 3, 6, 12, 28
OPT_REQ_IP, OPT_LEASE, OPT_MSGTYPE, OPT_SERVER_ID = 50, 51, 53, 54
OPT_RENEW, OPT_REBIND, OPT_END = 58, 59, 255

LEASE_SECONDS = 24 * 3600


class DhcpError(Exception):
    pass


def _parse(packet):
    """-> dict with op, xid, flags, ciaddr, chaddr(bytes6), opts{code: bytes}"""
    if len(packet) < 240 or packet[236:240] != MAGIC:
        return None
    op, htype, hlen = packet[0], packet[1], packet[2]
    xid = packet[4:8]
    flags = struct.unpack("!H", packet[10:12])[0]
    ciaddr = socket.inet_ntoa(packet[12:16])
    chaddr = packet[28:28 + min(hlen, 16)]
    opts, i = {}, 240
    while i < len(packet):
        code = packet[i]
        if code == OPT_END:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(packet):
            break
        ln = packet[i + 1]
        opts[code] = packet[i + 2:i + 2 + ln]
        i += 2 + ln
    return {"op": op, "htype": htype, "xid": xid, "flags": flags,
            "ciaddr": ciaddr, "chaddr": chaddr, "opts": opts}


def _opt(code, data):
    return bytes([code, len(data)]) + data


def _build(req, msg_type, yiaddr, server_ip, net):
    pkt = bytearray(240)
    pkt[0] = 2  # BOOTREPLY
    pkt[1] = req["htype"] or 1
    pkt[2] = len(req["chaddr"])
    pkt[4:8] = req["xid"]
    pkt[10:12] = struct.pack("!H", req["flags"])
    pkt[16:20] = socket.inet_aton(yiaddr)
    pkt[20:24] = socket.inet_aton(server_ip)
    pkt[28:28 + len(req["chaddr"])] = req["chaddr"]
    pkt[236:240] = MAGIC
    ip = socket.inet_aton(server_ip)
    o = _opt(OPT_MSGTYPE, bytes([msg_type])) + _opt(OPT_SERVER_ID, ip)
    if msg_type != NAK:
        o += (_opt(OPT_LEASE, struct.pack("!I", LEASE_SECONDS))
              + _opt(OPT_RENEW, struct.pack("!I", LEASE_SECONDS // 2))
              + _opt(OPT_REBIND, struct.pack("!I", LEASE_SECONDS * 7 // 8))
              + _opt(OPT_MASK, net.netmask.packed)
              + _opt(OPT_BCAST, net.broadcast_address.packed)
              + _opt(OPT_ROUTER, ip)
              + _opt(OPT_DNS, ip))
    return bytes(pkt) + o + bytes([OPT_END])


def _mac(b):
    return ":".join(f"{x:02x}" for x in b)


def probe_other_dhcp(server_ip, timeout=2.5, client_port=68, server_port=67):
    """Broadcast a DHCPDISCOVER from `server_ip` and return the IP of any
    DHCP server that answers, or None. Returns 'unknown' if we couldn't probe."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind((server_ip, client_port))
    except OSError:
        return "unknown"
    xid = os.urandom(4)
    mac = b"\x02" + os.urandom(5)  # locally administered, random
    pkt = bytearray(240)
    pkt[0], pkt[1], pkt[2] = 1, 1, 6
    pkt[4:8] = xid
    pkt[10:12] = b"\x80\x00"  # ask for broadcast replies
    pkt[28:34] = mac
    pkt[236:240] = MAGIC
    pkt = bytes(pkt) + _opt(OPT_MSGTYPE, bytes([DISCOVER])) + bytes([OPT_END])
    found = None
    try:
        s.settimeout(0.3)
        s.sendto(pkt, ("255.255.255.255", server_port))
        end = time.time() + timeout
        while time.time() < end and not found:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            p = _parse(data)
            if p and p["op"] == 2 and p["xid"] == xid:
                sid = p["opts"].get(OPT_SERVER_ID)
                found = socket.inet_ntoa(sid) if sid and len(sid) == 4 else addr[0]
    except OSError:
        return "unknown"
    finally:
        s.close()
    return found


class DhcpServer:
    """Serves 'start'..'end' of the server's /24, DNS + gateway = server_ip."""

    def __init__(self, server_ip, prefix=24, pool=(100, 199), on_event=None,
                 server_port=67, client_port=68):
        self.server_ip = server_ip
        self.net = ipaddress.ip_network(f"{server_ip}/{prefix}", strict=False)
        base = int(self.net.network_address)
        self.pool = [str(ipaddress.ip_address(base + n)) for n in range(pool[0], pool[1] + 1)
                     if str(ipaddress.ip_address(base + n)) != server_ip]
        self.on_event = on_event
        self.server_port, self.client_port = server_port, client_port
        self.leases = {}   # mac -> ip
        self.offers = {}   # mac -> ip (pending)
        self._sock = None
        self._stop = threading.Event()

    def _event(self, kind, client, detail):
        if self.on_event:
            try:
                self.on_event(kind, client, detail)
            except Exception:
                pass

    def start(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((self.server_ip, self.server_port))
        except OSError as e:
            raise DhcpError(f"Can't open the DHCP port on {self.server_ip}: {e}\n"
                            "Turn off Windows Mobile Hotspot / Internet Connection "
                            "Sharing and close other DHCP programs.")
        s.settimeout(0.5)
        self._sock = s
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    def _taken(self, ip, mac):
        return any(v == ip and k != mac for k, v in list(self.leases.items())
                   + list(self.offers.items()))

    def _allocate(self, mac, wanted):
        if mac in self.leases:
            return self.leases[mac]
        if wanted in self.pool and not self._taken(wanted, mac):
            return wanted
        free = [ip for ip in self.pool if not self._taken(ip, mac)]
        return random.choice(free[:20]) if free else None

    def _send(self, req, reply):
        # Clients without an address need a broadcast reply.
        if req["ciaddr"] != "0.0.0.0" and not (req["flags"] & 0x8000):
            dest = req["ciaddr"]
        else:
            dest = "255.255.255.255"
        try:
            self._sock.sendto(reply, (dest, self.client_port))
        except OSError:
            try:
                self._sock.sendto(reply, (str(self.net.broadcast_address), self.client_port))
            except OSError:
                pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except ConnectionResetError:
                # Windows reports "port unreachable" from an earlier reply
                # (e.g. a console that went to sleep) on the NEXT read. Harmless.
                continue
            except OSError:
                if self._stop.is_set():
                    break
                time.sleep(0.2)
                continue
            req = _parse(data)
            if not req or req["op"] != 1 or OPT_MSGTYPE not in req["opts"]:
                continue
            try:
                self._handle(req)
            except Exception as e:  # never let one bad packet kill the server
                self._event("info", "", f"DHCP error: {e}")

    def _handle(self, req):
        mtype = req["opts"][OPT_MSGTYPE][0]
        mac = _mac(req["chaddr"])
        host = req["opts"].get(OPT_HOSTNAME, b"").decode("ascii", "replace")
        who = f"{mac}{' (' + host + ')' if host else ''}"
        wanted = req["opts"].get(OPT_REQ_IP)
        wanted = socket.inet_ntoa(wanted) if wanted and len(wanted) == 4 else None

        if mtype == DISCOVER:
            ip = self._allocate(mac, wanted)
            if not ip:
                self._event("info", "", "DHCP pool is full")
                return
            self.offers[mac] = ip
            self._send(req, _build(req, OFFER, ip, self.server_ip, self.net))

        elif mtype == REQUEST:
            sid = req["opts"].get(OPT_SERVER_ID)
            if sid and socket.inet_ntoa(sid) != self.server_ip:
                self.offers.pop(mac, None)  # client picked another server
                return
            ip = wanted or (req["ciaddr"] if req["ciaddr"] != "0.0.0.0" else None)
            if (ip and ip in self.pool and not self._taken(ip, mac)):
                self.leases[mac] = ip
                self.offers.pop(mac, None)
                self._send(req, _build(req, ACK, ip, self.server_ip, self.net))
                self._event("dhcp", ip, f"gave {ip} to {who}")
            else:
                self._send(req, _build(req, NAK, "0.0.0.0", self.server_ip, self.net))

        elif mtype == INFORM:
            self._send(req, _build(req, ACK, "0.0.0.0", self.server_ip, self.net))

        elif mtype in (RELEASE, DECLINE):
            self.leases.pop(mac, None)
            self.offers.pop(mac, None)
