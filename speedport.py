#!/usr/bin/env python3
"""CLI for the Cosmote Speedport Plus 2 (Sercomm VD4224BDTP) router."""
import argparse
import getpass
import hashlib
import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HOST = os.environ.get("SPEEDPORT_HOST", "192.168.1.1")
ENV_FILE = Path(__file__).resolve().parent / ".env"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "speedport"
PUBLIC_IP_URL = "https://api.ipify.org"
LOGIN_ERRORS = {
    '"2"': "another user is logged in",
    '"3"': "wrong username or password",
    '"4"': "wrong username or password",
    '"5"': "too many failed logins, router is delaying new attempts",
}

WIFI_ROLES = {"2.4": (1,), "5": (2,), "guest-2.4": (3,), "guest-5": (4,), "main": (1, 2), "guest": (3, 4)}
WIFI_NAMES = {1: "2.4", 2: "5", 3: "guest-2.4", 4: "guest-5"}
RADIO_OF_SSID = {1: 1, 2: 2, 3: 1, 4: 2}
WIFI_SECURITY = {
    "wpa2": "WPA2-Personal",
    "wpa3": "WPA3-Personal",
    "wpa2-wpa3": "WPA3-Personal-Transition",
    "open": "None",
}
WAN_TYPES = {2: "ethernet", 3: "lte", 4: "wifi-client"}
MAP_RULE = "Device.DHCPv6.Client.1.X_RDKCENTRAL-COM_RcvOption."
DMZ = "Device.NAT.X_CISCO_COM_DMZ."
PORT_TABLE = "Device.NAT.PortMapping"
PROMPT = object()


class RouterError(Exception):
    pass


def load_env():
    if not ENV_FILE.exists():
        raise RouterError(f"missing {ENV_FILE} (needs USER and PASS)")
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    return env


def write_private(path, text):
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


class Router:
    session_file = CACHE_DIR / "session.json"
    pin_file = CACHE_DIR / "cert.sha256"

    def __init__(self):
        self.sid = None
        self.token = None
        try:
            saved = json.loads(self.session_file.read_text())
            self.sid, self.token = saved["sid"], saved["token"]
        except (OSError, ValueError, KeyError):
            pass

    def _check_pin(self, der_cert):
        # Router cert is self-signed, so trust it on first use and refuse if it ever changes.
        fingerprint = hashlib.sha256(der_cert).hexdigest()
        if not self.pin_file.exists():
            write_private(self.pin_file, fingerprint)
        elif self.pin_file.read_text().strip() != fingerprint:
            raise RouterError(f"router TLS certificate changed. If expected (firmware update), delete {self.pin_file}")

    def _request(self, method, path, body=None, content_type="application/x-www-form-urlencoded"):
        conn = http.client.HTTPSConnection(HOST, timeout=30, context=ssl._create_unverified_context())
        try:
            conn.connect()
            self._check_pin(conn.sock.getpeercert(binary_form=True))
            headers = {"Accept-Language": "en"}
            if self.sid:
                headers["Cookie"] = f"session_id={self.sid}"
            if body is not None:
                headers["Content-Type"] = content_type
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            data = response.read().decode(errors="replace")
            cookie = re.search(r"session_id=([^;]+)", response.getheader("Set-Cookie") or "")
            if cookie:
                self.sid = cookie.group(1)
            return response.status, data
        except OSError as e:
            raise RouterError(f"cannot reach router at {HOST}: {e}") from e
        finally:
            conn.close()

    def _query(self):
        return f"?_={int(time.time() * 1000)}&csrf_token={self.token}"

    def login(self):
        self.sid = None
        _, html = self._request("GET", "/login.html")
        match = re.search(r"csrf_token\s*=\s*'([^']+)'", html)
        if not match:
            raise RouterError("csrf token not found on login page")
        self.token = match.group(1)
        env = load_env()
        # The web UI URI-encodes the username twice; urlencode does the second pass.
        body = urllib.parse.urlencode({
            "LoginName": urllib.parse.quote(env["USER"], safe=""),
            "LoginPWD": hashlib.sha256(env["PASS"].encode()).hexdigest(),
        })
        _, reply = self._request("POST", "/data/login.json" + self._query(), body)
        if reply.strip() == '"6"':  # another session is active, resending kicks it out
            _, reply = self._request("POST", "/data/login.json" + self._query(), body)
        reply = reply.strip()
        if reply != '"1"':
            raise RouterError("login failed: " + LOGIN_ERRORS.get(reply, f"unexpected reply {reply[:80]}"))
        write_private(self.session_file, json.dumps({"sid": self.sid, "token": self.token}))

    def call(self, method, path, body=None, content_type="application/x-www-form-urlencoded"):
        if not self.sid:
            self.login()
        status, data = self._request(method, path + self._query(), body, content_type)
        # Expired sessions redirect (302); stale ones sometimes get HTTP 500 or an empty 200. A fresh login clears both.
        if status != 200 or not data.strip():
            self.login()
            status, data = self._request(method, path + self._query(), body, content_type)
        if status != 200 or not data.strip():
            raise RouterError(f"{method} {path} returned HTTP {status} with {len(data)} bytes")
        return data

    def rpc(self, calls):
        payload = [{"jsonrpc": "2.0", "method": m, "params": p, "id": i} for i, (m, p) in enumerate(calls, 1)]
        reply = self.call("POST", "/data/data.cgi", json.dumps(payload), "application/json")
        try:
            replies = json.loads(reply)
        except ValueError:
            raise RouterError(f"router sent invalid JSON: {reply[:100]!r}") from None
        failures = [r.get("error") or r.get("result") for r in replies if rpc_failed(r)]
        if failures:
            raise RouterError("router rejected request: " + json.dumps(failures))
        return replies

    def get(self, *paths):
        result = {}
        for reply in self.rpc([("GET", p) for p in paths]):
            result.update(reply.get("result", {}))
        return result

    def set(self, values):
        return self.rpc([("SET", {path: value}) for path, value in values.items()])

    def table_write(self, root, rows=(), modify=(), delete=()):
        # Tables have no ADD/DEL methods: new rows use index 0, deletes are only listed in the action element.
        action = {"modify": ",".join(map(str, modify)), "delete": ",".join(map(str, delete))}
        return self.rpc([("SET", {f"action.{root}@": action}), ("SET", {f"{root}@": list(rows)})])


def rpc_failed(reply):
    result = reply.get("result")
    return bool(reply.get("error")) or (isinstance(result, dict) and str(result.get("code", 200)) != "200")


def table(values, root):
    rows = {}
    for path, value in values.items():
        match = re.fullmatch(re.escape(root) + r"\.(\d+)\.([^.]+)", path)
        if match:
            rows.setdefault(int(match[1]), {})[match[2]] = value
    return dict(sorted(rows.items()))


def map_port_ranges(psid, psid_len, offset):
    """RFC 7597 MAP port set: the external port blocks a shared IPv4 customer owns."""
    block = 1 << (16 - offset - psid_len)
    first_block = 1 if offset else 0  # with an offset, ports 0-1023 (block 0) are excluded
    starts = ((i << (16 - offset)) | (psid * block) for i in range(first_block, 1 << offset))
    return [(start, start + block - 1) for start in starts]


def read_map_ranges(router):
    v = router.get(MAP_RULE + "MapPSID", MAP_RULE + "MapPSIDLen", MAP_RULE + "MapPSIDOffset")
    try:
        return map_port_ranges(*(int(v[MAP_RULE + k]) for k in ("MapPSID", "MapPSIDLen", "MapPSIDOffset")))
    except (KeyError, ValueError):
        return None


def public_ipv4():
    try:
        with urllib.request.urlopen(PUBLIC_IP_URL, timeout=10) as response:
            return str(ipaddress.IPv4Address(response.read().decode().strip()))
    except (OSError, ValueError) as e:
        raise RouterError(f"cannot get public IPv4 from {PUBLIC_IP_URL}: {e}") from e


def notify(text):
    env = load_env()
    token, chat = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10).close()
    except OSError as e:
        raise RouterError(f"telegram notification failed: {e}") from e


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def duration(seconds):
    if seconds is None:
        return "?"
    days, s = divmod(seconds, 86400)
    hours, s = divmod(s, 3600)
    return f"{days}d {hours}h {s // 60}m"


def print_table(headers, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    for row in [headers, *rows]:
        print("  ".join(str(x).ljust(w) for x, w in zip(row, widths)).rstrip())


def print_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def port_range(text):
    try:
        start, _, end = text.partition("-")
        start, end = int(start), int(end or start)
    except ValueError:
        raise argparse.ArgumentTypeError(f"bad port or range: {text}")
    if not 1 <= start <= end <= 65535:
        raise argparse.ArgumentTypeError(f"ports must be 1-65535 and start <= end: {text}")
    return start, end


def lan_ipv4(text):
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an IPv4 address: {text}")
    if not ip.is_private or str(ip) == HOST:
        raise argparse.ArgumentTypeError(f"{text} must be a LAN device, not the router or a public address")
    return str(ip)


def cmd_status(router, args):
    info = "Device.DeviceInfo."
    wan_paths = [f"Device.X_RDK_WanManager.CPEInterface.{i}.Wan.{k}" for i in WAN_TYPES for k in ("LinkStatus", "Name")]
    v = router.get(
        info + "ModelName", info + "SoftwareVersion", info + "UpTime", info + "X_COMCAST-COM_WAN_IPv6",
        "Virtual.Virtual.WAN.Interface.Name", "Device.IP.Interface.1.LastChange",
        "Device.IP.Interface.1.IPv6Prefix.1.Prefix", "Device.Ethernet.Interface.1.CurrentBitRate",
        "Device.IP.Interface.1.IPv4Address.1.X_SC_PrimaryV4DNSServer",
        "Device.IP.Interface.1.IPv4Address.1.X_SC_SecondV4DNSServer",
        *wan_paths,
    )
    wan = next((name for i, name in WAN_TYPES.items()
                if v.get(f"Device.X_RDK_WanManager.CPEInterface.{i}.Wan.LinkStatus") == "Up"
                and v.get(f"Device.X_RDK_WanManager.CPEInterface.{i}.Wan.Name") == v.get("Virtual.Virtual.WAN.Interface.Name")),
               "down")
    status = {
        "model": v[info + "ModelName"],
        "firmware": v[info + "SoftwareVersion"],
        "uptime_s": as_int(v[info + "UpTime"]),
        "wan": wan,
        "wan_link_mbit": as_int(v["Device.Ethernet.Interface.1.CurrentBitRate"]) if wan == "ethernet" else None,
        "wan_uptime_s": as_int(v["Device.IP.Interface.1.LastChange"]),
        "ipv6": v[info + "X_COMCAST-COM_WAN_IPv6"],
        "ipv6_prefix": v["Device.IP.Interface.1.IPv6Prefix.1.Prefix"],
        "dns": [dns for k in ("Primary", "Second") if (dns := v.get(f"Device.IP.Interface.1.IPv4Address.1.X_SC_{k}V4DNSServer"))],
    }
    if args.json:
        print_json(status)
        return
    link = f" {status['wan_link_mbit']} Mbit/s" if status["wan_link_mbit"] else ""
    print(f"model      {status['model']} fw {status['firmware']}")
    print(f"uptime     {duration(status['uptime_s'])}")
    print(f"wan        {wan}{link}, up {duration(status['wan_uptime_s'])}")
    print(f"ipv6       {status['ipv6']} prefix {status['ipv6_prefix']}")
    print(f"dns        {', '.join(status['dns'])}")


def active_hosts(router, include_inactive=False):
    hosts = table(router.get("Device.Hosts.Host."), "Device.Hosts.Host").values()
    return [h for h in hosts if include_inactive or h.get("Active") is True]


def host_via(host):
    match = re.fullmatch(r"Device\.WiFi\.SSID\.(\d)", host.get("Layer1Interface", ""))
    return f"wifi {WIFI_NAMES.get(int(match[1]), '?')}" if match else host.get("InterfaceType", "?").lower()


def cmd_devices(router, args):
    hosts = active_hosts(router, args.all)
    rows = [{"name": h.get("HostName", ""), "ip": h.get("IPAddress", ""), "mac": h.get("PhysAddress", ""),
             "via": host_via(h), "rssi": h.get("X_CISCO_COM_RSSI", ""), "active": h.get("Active") is True}
            for h in hosts]
    if args.json:
        print_json(rows)
        return
    headers = ["NAME", "IP", "MAC", "VIA", "RSSI"] + (["ACTIVE"] if args.all else [])
    print_table(headers, [[r["name"], r["ip"], r["mac"], r["via"], r["rssi"] if r["via"].startswith("wifi") else ""]
                          + (["yes" if r["active"] else "no"] if args.all else []) for r in rows])


def cmd_watch_devices(router, args):
    state_file = CACHE_DIR / "devices.json"
    now = {h["PhysAddress"]: h.get("HostName") or h.get("IPAddress", "") for h in active_hosts(router)}
    try:
        before = json.loads(state_file.read_text())
    except (OSError, ValueError):
        write_private(state_file, json.dumps(now))
        print(f"baseline saved: {len(now)} devices")
        return
    for label, macs, names in (("connected", now.keys() - before.keys(), now),
                               ("disconnected", before.keys() - now.keys(), before)):
        if macs:
            message = f"Device(s) {label}: " + ", ".join(f"{names[m]} ({m})" for m in sorted(macs))
            print(message)
            notify(message)
    write_private(state_file, json.dumps(now))


def cmd_reboot(router, args):
    if not args.yes and input("Reboot the router? [y/N] ").strip().lower() != "y":
        sys.exit("aborted")
    router.set({
        "Device.DeviceInfo.X_RDKCENTRAL-COM_UI_ACCESS": "reboot_device",
        "Device.X_CISCO_COM_DeviceControl.RebootDevice": "Device,delay=3",
    })
    print("rebooting, back in a few minutes")


def cmd_wifi_show(router, args):
    fields = ["SSID.{i}.Enable", "SSID.{i}.SSID", "AccessPoint.{i}.Security.ModeEnabled",
              "AccessPoint.{i}.SSIDAdvertisementEnabled"]
    if args.show_password:
        fields.append("AccessPoint.{i}.Security.KeyPassphrase")
    radio_fields = ["Radio.{i}.Enable", "Radio.{i}.Channel", "Radio.{i}.AutoChannelEnable",
                    "Radio.{i}.OperatingChannelBandwidth"]
    paths = [f"Device.WiFi.{f.format(i=i)}" for i in WIFI_NAMES for f in fields]
    paths += [f"Device.WiFi.{f.format(i=i)}" for i in (1, 2) for f in radio_fields]
    v = router.get(*paths, "Device.WiFi.X_RDKCENTRAL-COM_BandSteering.Enable")
    w = lambda path: v.get("Device.WiFi." + path)
    networks = []
    for i, role in WIFI_NAMES.items():
        network = {"role": role, "enabled": w(f"SSID.{i}.Enable") is True, "ssid": w(f"SSID.{i}.SSID"),
                   "security": w(f"AccessPoint.{i}.Security.ModeEnabled"),
                   "visible": w(f"AccessPoint.{i}.SSIDAdvertisementEnabled") is not False}
        if args.show_password:
            network["password"] = w(f"AccessPoint.{i}.Security.KeyPassphrase")
        networks.append(network)
    radios = [{"band": band, "enabled": w(f"Radio.{i}.Enable") is True, "channel": as_int(w(f"Radio.{i}.Channel")),
               "auto_channel": w(f"Radio.{i}.AutoChannelEnable") is True,
               "bandwidth": w(f"Radio.{i}.OperatingChannelBandwidth")}
              for i, band in ((1, "2.4GHz"), (2, "5GHz"))]
    band_steering = w("X_RDKCENTRAL-COM_BandSteering.Enable") is True
    if args.json:
        print_json({"networks": networks, "radios": radios, "band_steering": band_steering})
        return
    on_off = lambda flag: "on" if flag else "off"
    print_table(["ROLE", "STATE", "SSID", "SECURITY", "VISIBLE", "PASSWORD"], [
        [n["role"], on_off(n["enabled"]), n["ssid"], n["security"], "yes" if n["visible"] else "no",
         n.get("password", "***")] for n in networks])
    print()
    for r in radios:
        print(f"radio {r['band']:<8} {on_off(r['enabled'])}, channel {r['channel']} "
              f"({'auto' if r['auto_channel'] else 'fixed'}), {r['bandwidth']}")
    print(f"band steering  {on_off(band_steering)}")


def cmd_wifi_set(router, args):
    password = args.password
    if password is PROMPT:
        password = getpass.getpass("New WiFi password: ")
        if password != getpass.getpass("Repeat: "):
            sys.exit("speedport: passwords do not match")
    if password is not None and not 8 <= len(password) <= 63:
        sys.exit("speedport: WiFi password must be 8-63 characters")
    if args.ssid is not None and not 1 <= len(args.ssid.encode()) <= 32:
        sys.exit("speedport: SSID must be 1-32 bytes")
    values = {}
    for i in WIFI_ROLES[args.role]:
        ap = f"Device.WiFi.AccessPoint.{i}."
        changes = {
            f"Device.WiFi.SSID.{i}.SSID": args.ssid,
            f"Device.WiFi.SSID.{i}.Enable": args.enable,
            ap + "Security.KeyPassphrase": password,
            ap + "Security.ModeEnabled": WIFI_SECURITY.get(args.security),
            ap + "SSIDAdvertisementEnabled": None if args.hidden is None else not args.hidden,
        }
        values.update({path: value for path, value in changes.items() if value is not None})
    if not values:
        sys.exit("speedport: nothing to change")
    radios = {RADIO_OF_SSID[i] for i in WIFI_ROLES[args.role]}
    for radio in (1, 2):  # the UI always sends both flags last; true applies that radio's changes
        values[f"Device.WiFi.Radio.{radio}.X_CISCO_COM_ApplySetting"] = radio in radios
    router.set(values)
    print(f"wifi {args.role} updated, clients may reconnect")


def cmd_wifi_wps(router, args):
    router.set({f"Device.WiFi.AccessPoint.{i}.WPS.X_DT_PressPushButton": "true" for i in (1, 2)})
    print("WPS started on 2.4 and 5 GHz, press WPS on the device now")


def cmd_ports_list(router, args):
    ranges = read_map_ranges(router)
    forwards = []
    for i, r in table(router.get(PORT_TABLE + "."), PORT_TABLE).items():
        ext_start, ext_end = int(r["ExternalPort"]), int(r["ExternalPortEndRange"])
        forwards.append({
            "id": i, "enabled": r.get("Enable") is True, "protocol": r.get("Protocol"),
            "external_start": ext_start, "external_end": ext_end, "client": r.get("InternalClient"),
            "internal_start": int(r["InternalPort"]), "internal_end": int(r["InternalPortEndRange"]),
            "name": r.get("Description", ""), "reachable": is_reachable(ranges, ext_start, ext_end),
        })
    if args.json:
        print_json(forwards)
        return
    print_table(["ID", "STATE", "PROTO", "EXTERNAL", "TO", "NAME", "REACHABLE"], [
        [f["id"], "on" if f["enabled"] else "off", f["protocol"], span(f["external_start"], f["external_end"]),
         f"{f['client']}:{span(f['internal_start'], f['internal_end'])}", f["name"],
         "yes" if f["reachable"] else "no (outside MAP-E port set)"]
        for f in forwards])


def span(start, end):
    return str(start) if start == end else f"{start}-{end}"


def is_reachable(ranges, start, end):
    return ranges is None or any(lo <= start and end <= hi for lo, hi in ranges)


def cmd_ports_add(router, args):
    ext_start, ext_end = args.external
    int_start = args.internal_port or ext_start
    int_end = int_start + ext_end - ext_start
    if int_end > 65535:
        sys.exit("speedport: internal port range goes past 65535")
    if not is_reachable(read_map_ranges(router), ext_start, ext_end):
        print("warning: external ports are outside your MAP-E port set, not reachable over IPv4. See `cgnat`.",
              file=sys.stderr)
    row = {
        "index": 0, "create_index": 1, "Description": args.name, "Protocol": args.protocol, "Enable": True,
        "ExternalPort": str(ext_start), "ExternalPortEndRange": str(ext_end),
        "InternalClient": args.client, "InternalPort": str(int_start), "InternalPortEndRange": str(int_end),
    }
    router.table_write(PORT_TABLE, rows=[row])
    print(f"forwarding {args.protocol} {span(ext_start, ext_end)} -> {args.client}:{span(int_start, int_end)}")


def cmd_ports_rm(router, args):
    rows = table(router.get(PORT_TABLE + "."), PORT_TABLE)
    if args.id not in rows:
        sys.exit(f"speedport: no port forward with id {args.id} (see `ports`)")
    router.table_write(PORT_TABLE, delete=[args.id])
    print(f"removed port forward {args.id} ({rows[args.id].get('Description', '')})")


def cmd_dmz(router, args):
    v = router.get(DMZ + "Enable", DMZ + "InternalIP")
    enabled = v[DMZ + "Enable"] is True
    if args.json:
        print_json({"enabled": enabled, "ip": v[DMZ + "InternalIP"] if enabled else None})
        return
    print(f"dmz on -> {v[DMZ + 'InternalIP']}" if enabled else "dmz off")


def cmd_dmz_set(router, args):
    router.set({DMZ + "Enable": "1", DMZ + "InternalIP": args.ip})
    print(f"dmz on -> {args.ip}")


def cmd_dmz_off(router, args):
    router.set({DMZ + "Enable": "0"})
    print("dmz off")


def cmd_public_ip(router, args):
    ip = public_ipv4()
    if args.json:
        print_json({"public_ipv4": ip})
    else:
        print(ip)


def cmd_cgnat(router, args):
    ip = public_ipv4()
    ranges = read_map_ranges(router)
    total = 65536 if ranges is None else sum(hi - lo + 1 for lo, hi in ranges)
    if args.json:
        print_json({"public_ipv4": ip, "shared": ranges is not None, "port_count": total, "port_ranges": ranges})
        return
    if ranges is None:
        print(f"public IPv4 {ip} is yours alone (no MAP-E port sharing). Port forwarding works on any port.")
        return
    print(f"public IPv4 {ip} is shared (MAP-E). You own {total} external ports, only these can be forwarded:")
    print(", ".join(f"{lo}-{hi}" for lo, hi in ranges))


def cmd_dyndns(router, args):
    ip = public_ipv4()
    try:
        current = socket.gethostbyname(args.domain)
    except OSError:
        current = None
    if current == ip:
        return
    print(f"{args.domain}: {current} -> {ip}", flush=True)
    result = subprocess.run(args.run, shell=True, env={**os.environ, "SPEEDPORT_IP": ip})
    if result.returncode:
        raise RouterError(f"update command failed with exit code {result.returncode}")
    notify(f"{args.domain} now points to {ip}")


def cmd_get(router, args):
    print_json(router.get(*args.paths))


def cmd_set(router, args):
    literals = {"true": True, "false": False}
    pairs = zip(args.pairs[::2], args.pairs[1::2])
    print_json(router.set({path: literals.get(value, value) for path, value in pairs}))


def build_parser():
    parser = argparse.ArgumentParser(prog="speedport", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    json_flag = argparse.ArgumentParser(add_help=False)
    json_flag.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("status", parents=[json_flag], help="model, uptime, WAN link, IPv6, DNS").set_defaults(func=cmd_status)

    p = sub.add_parser("devices", parents=[json_flag], help="connected devices")
    p.add_argument("--all", action="store_true", help="include devices that are no longer connected")
    p.set_defaults(func=cmd_devices)

    sub.add_parser("watch-devices", help="report (and Telegram) devices that (dis)connected since last run") \
        .set_defaults(func=cmd_watch_devices)

    p = sub.add_parser("reboot", help="reboot the router")
    p.add_argument("-y", "--yes", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_reboot)

    wifi = sub.add_parser("wifi", parents=[json_flag], help="show WiFi networks; `wifi set` / `wifi wps` to change")
    wifi.add_argument("--show-password", action="store_true")
    wifi.set_defaults(func=cmd_wifi_show)
    wifi_sub = wifi.add_subparsers(dest="wifi_command")
    p = wifi_sub.add_parser("set", help="change a WiFi network")
    p.add_argument("role", choices=WIFI_ROLES, help="main/guest change both bands")
    p.add_argument("--ssid")
    p.add_argument("--password", nargs="?", const=PROMPT, help="omit the value to be prompted (keeps it out of history)")
    p.add_argument("--security", choices=WIFI_SECURITY)
    state = p.add_mutually_exclusive_group()
    state.add_argument("--on", dest="enable", action="store_const", const=True)
    state.add_argument("--off", dest="enable", action="store_const", const=False)
    visibility = p.add_mutually_exclusive_group()
    visibility.add_argument("--hidden", dest="hidden", action="store_const", const=True)
    visibility.add_argument("--visible", dest="hidden", action="store_const", const=False)
    p.set_defaults(func=cmd_wifi_set)
    wifi_sub.add_parser("wps", help="start WPS push button pairing").set_defaults(func=cmd_wifi_wps)

    ports = sub.add_parser("ports", parents=[json_flag], help="list port forwards; `ports add` / `ports rm` to change")
    ports.set_defaults(func=cmd_ports_list)
    ports_sub = ports.add_subparsers(dest="ports_command")
    p = ports_sub.add_parser("add", help="forward external port(s) to a LAN device")
    p.add_argument("external", type=port_range, help="external port or range, e.g. 1424 or 1424-1430")
    p.add_argument("client", type=lan_ipv4, help="LAN device IP")
    p.add_argument("internal_port", type=int, nargs="?", help="first internal port (default: same as external)")
    p.add_argument("--protocol", choices=["TCP", "UDP", "BOTH"], default="TCP")
    p.add_argument("--name", default="speedport-cli")
    p.set_defaults(func=cmd_ports_add)
    p = ports_sub.add_parser("rm", help="remove a port forward by ID")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_ports_rm)

    dmz = sub.add_parser("dmz", parents=[json_flag], help="show DMZ; `dmz set IP` / `dmz off` to change")
    dmz.set_defaults(func=cmd_dmz)
    dmz_sub = dmz.add_subparsers(dest="dmz_command")
    p = dmz_sub.add_parser("set", help="expose a LAN device to all inbound traffic")
    p.add_argument("ip", type=lan_ipv4)
    p.set_defaults(func=cmd_dmz_set)
    dmz_sub.add_parser("off").set_defaults(func=cmd_dmz_off)

    sub.add_parser("public-ip", parents=[json_flag], help=f"public IPv4 (asks {PUBLIC_IP_URL}, router only knows the MAP-E prefix)") \
        .set_defaults(func=cmd_public_ip)
    sub.add_parser("cgnat", parents=[json_flag], help="check IPv4 sharing (MAP-E) and which ports can be forwarded") \
        .set_defaults(func=cmd_cgnat)

    p = sub.add_parser("dyndns", help="run a command when DOMAIN no longer resolves to the public IPv4")
    p.add_argument("domain")
    p.add_argument("--run", required=True, help="shell command that updates DNS; new IP is in $SPEEDPORT_IP")
    p.set_defaults(func=cmd_dyndns)

    p = sub.add_parser("get", help="read raw TR-181 paths (trailing dot = whole subtree)")
    p.add_argument("paths", nargs="+")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("set", help="write raw TR-181 values: PATH VALUE [PATH VALUE ...] (true/false become booleans)")
    p.add_argument("pairs", nargs="+")
    p.set_defaults(func=cmd_set)
    return parser


def main():
    args = build_parser().parse_args()
    if getattr(args, "pairs", None) and len(args.pairs) % 2:
        sys.exit("speedport: set needs PATH VALUE pairs")
    try:
        args.func(Router(), args)
    except RouterError as e:
        sys.exit(f"speedport: {e}")


if __name__ == "__main__":
    main()
