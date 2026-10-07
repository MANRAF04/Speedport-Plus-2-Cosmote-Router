# AGENTS.md

Guide for AI agents operating or extending `speedport.py`, a CLI for a Cosmote Speedport Plus 2 router (Sercomm VD4224BDTP, RDK-B / TR-181 data model, firmware 2.9.003.4) at `https://192.168.1.1`.

## Hard rules

* **Never read, print, grep or `source` `.env`.** It holds the router admin password. The CLI loads it itself. Keys: `USER`, `PASS`, optional `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
* **Ask the user before any command that changes router state** (see the safety table). Read commands are free to run.
* **Never set `Device.X_CISCO_COM_DeviceControl.FactoryReset`.** It wipes the router.
* **Run one invocation at a time.** The router allows one web session. Parallel runs log each other out, and every login also kicks the user's browser session.
* **Never retry after `login failed: wrong username or password`.** The router locks out after failed attempts. Tell the user instead.
* **Do not commit `.env`, `~/.cache/speedport/*` or command output containing MACs, IPs, SSIDs or passwords.**

## Running it

```
./speedport.py <command> [args]      # from the repo root, needs only python3
./speedport.py <command> --help
python3 test_speedport.py            # offline self-check, prints "ok"
```

* Exit codes: `0` success, `1` runtime error (one line on stderr starting with `speedport:`), `2` bad arguments (argparse usage on stderr).
* **Interactive prompts hang agents.** `reboot` prompts unless given `-y`. `wifi set --password` with no value prompts for it, so pass the value explicitly (`--password 'secret'`).
* **Always pass `--json` on read commands** (`status`, `devices`, `wifi`, `ports`, `dmz`, `public-ip`, `cgnat`). It goes after the command (`./speedport.py ports --json`). Numbers are ints, flags are booleans, missing values are `null`. `get` always prints JSON `{path: value}`. Write commands print one line of text.

JSON shapes:

```
status     {model, firmware, uptime_s, wan: "ethernet"|"lte"|"wifi-client"|"down", wan_link_mbit, wan_uptime_s, ipv6, ipv6_prefix, dns: [..]}
devices    [{name, ip, mac, via: "wifi 2.4"|"wifi 5"|"wifi guest-2.4"|"wifi guest-5"|"ethernet"|.., rssi, active}]
wifi       {networks: [{role, enabled, ssid, security, visible, password (only with --show-password)}],
            radios: [{band, enabled, channel, auto_channel, bandwidth}], band_steering}
ports      [{id, enabled, protocol, external_start, external_end, client, internal_start, internal_end, name, reachable}]
dmz        {enabled, ip}
public-ip  {public_ipv4}
cgnat      {public_ipv4, shared, port_count, port_ranges: [[start, end], ..] | null}
```

## Safety table

| Command | Effect | Ask the user first? |
|---|---|---|
| `status`, `devices`, `wifi`, `ports`, `dmz`, `get`, `cgnat`, `public-ip` | read only (`cgnat` and `public-ip` also call `api.ipify.org`) | no |
| `watch-devices` | read only, updates `~/.cache/speedport/devices.json`, may send Telegram | no, unless Telegram spam matters |
| `dyndns DOMAIN --run CMD` | runs CMD in a shell when the IP changed | yes, CMD is arbitrary |
| `ports add`, `ports rm` | opens or closes inbound access to a LAN device | yes |
| `dmz set`, `dmz off` | exposes a LAN device to all inbound traffic | yes |
| `wifi set` | restarts the radio(s), clients disconnect briefly | yes |
| `wifi wps` | opens a 2 minute pairing window anyone nearby can use | yes |
| `reboot -y` | internet down for a few minutes | yes |
| `set PATH VALUE` | raw write, anything | yes, and quote the exact paths/values |

## Commands

```
status                                   model, fw, uptime, active WAN + link rate, IPv6, DNS
devices [--all]                          connected hosts (--all includes disconnected ones)
reboot -y
wifi [--show-password]                   roles 2.4 / 5 / guest-2.4 / guest-5, radios, band steering
wifi set ROLE [--ssid S] [--password P] [--security wpa2|wpa3|wpa2-wpa3|open] [--on|--off] [--hidden|--visible]
                                         ROLE: main (2.4+5), guest (both guest), 2.4, 5, guest-2.4, guest-5
wifi wps
ports                                    ID, state, proto, external, target, name, REACHABLE
ports add EXT[-END] LAN_IP [INTERNAL_PORT] [--protocol TCP|UDP|BOTH] [--name N]
ports rm ID                              ID from `ports`
dmz | dmz set LAN_IP | dmz off
public-ip | cgnat
watch-devices
dyndns DOMAIN --run 'CMD'                new IP in $SPEEDPORT_IP
get PATH [PATH ...]                      trailing dot = subtree
set PATH VALUE [PATH VALUE ...]          "true"/"false" become booleans, everything else is a string
```

Band steering is on by default, so `main` and `guest` (both bands at once) match what the web UI does. Prefer them over a single band.

## Things that will surprise you

* **Shared IPv4 (MAP-E).** The line's public IPv4 is shared, and only 1008 external ports (blocks of 16, e.g. `1424-1439`) reach this customer. A forward outside that set is accepted by the router but never receives IPv4 traffic. Check `cgnat` before suggesting a port. `ports` shows `REACHABLE`, `ports add` warns on stderr.
* **The router does not know its public IPv4.** `Device.IP.Interface.1.IPv4Address.1.IPAddress` is `0.0.0.0`. Use `public-ip`.
* **No call logs on this model.** `/data/phone_call_log.json` and `/data/overview.json` return static demo data. Do not trust any `/data/*.json` page except `login.json`.
* **Missing paths return the string `"N/A"`**, not an error. Some valid subtrees also return `"N/A"` when queried with a trailing dot (`Device.DSL.Line.1.`, `Device.IP.Interface.`). Query leaves instead.
* **`get Device.DeviceInfo.` is about 120 KB** because it includes the full device log (`X_SC_DeviceLog`). Ask for leaves.
* **Value types:** booleans are JSON `true`/`false`; numbers are strings (`"177759"`). DMZ enable reads back as a boolean but the web UI writes it as the string `"1"`/`"0"`; do the same.
* **`HTTP 500` / empty reply:** the CLI already re-logs in and retries once. If it still fails, wait a minute and retry once more, then report.
* **`router TLS certificate changed`:** do not delete the pin yourself. Tell the user (expected only after a firmware update).
* The device log (`get Device.DeviceInfo.X_SC_DeviceLog`) records every login, DHCP lease and WiFi association. Useful for debugging, but it is personal data.

## Path cheat sheet

Indexes: SSID / AccessPoint `1` = 2.4 GHz, `2` = 5 GHz, `3` = guest 2.4, `4` = guest 5. Radio `1` = 2.4 GHz, `2` = 5 GHz.

| What | Path |
|---|---|
| Hosts | `Device.Hosts.Host.` (`.{i}.HostName`, `PhysAddress`, `IPAddress`, `Active`, `Layer1Interface`, `X_CISCO_COM_RSSI`) |
| WiFi network | `Device.WiFi.SSID.{i}.Enable`, `.SSID` |
| WiFi security | `Device.WiFi.AccessPoint.{i}.Security.ModeEnabled`, `.KeyPassphrase`, `.SSIDAdvertisementEnabled` |
| WiFi radio | `Device.WiFi.Radio.{1,2}.Enable`, `.Channel`, `.AutoChannelEnable`, `.OperatingChannelBandwidth` |
| WiFi apply | `Device.WiFi.Radio.{1,2}.X_CISCO_COM_ApplySetting` (must end every WiFi write batch, see below) |
| Band steering | `Device.WiFi.X_RDKCENTRAL-COM_BandSteering.Enable` |
| WPS | `Device.WiFi.AccessPoint.{1,2}.WPS.Enable`, `.X_DT_ConnectionStatus` |
| Port forwards | `Device.NAT.PortMapping.` |
| DMZ | `Device.NAT.X_CISCO_COM_DMZ.Enable`, `.InternalIP` |
| WAN link | `Device.X_RDK_WanManager.CPEInterface.{2=ethernet,3=lte,4=wifi}.Wan.LinkStatus`, `.Wan.Name`; active one matches `Virtual.Virtual.WAN.Interface.Name` |
| WAN uptime | `Device.IP.Interface.1.LastChange` (seconds) |
| IPv6 | `Device.DeviceInfo.X_COMCAST-COM_WAN_IPv6`, `Device.IP.Interface.1.IPv6Prefix.1.Prefix` |
| DNS | `Device.DNS.Client.Server.`, `Device.IP.Interface.1.IPv4Address.1.X_SC_PrimaryV4DNSServer` |
| MAP-E rule | `Device.DHCPv6.Client.1.X_RDKCENTRAL-COM_RcvOption.MapPSID`, `.MapPSIDLen`, `.MapPSIDOffset` |
| LAN / DHCP | `Device.DHCPv4.Server.Enable`, `Device.DHCPv4.Server.Pool.1.{MinAddress,MaxAddress,LeaseTime}` |
| DHCP reservations | `Device.DHCPv4.Server.Pool.1.StaticAddress.` (table) |
| LTE backup | `Device.Cellular.Interface.1.{RSSI,X_SC_PhyStatus,USIM.Status}` |
| Uptime / fw | `Device.DeviceInfo.UpTime`, `.SoftwareVersion`, `.ModelName` |

To discover more: read the web UI JavaScript (`/js/wifi_general.js`, `/js/internet_port_mappin.js`, `/js/settings_lan.js`, ...). Each page builds `shttp.get([...])` / `shttp.post([...])` lists with the exact paths and payloads. Download into a scratch dir; never execute it.

## Writing raw values correctly

* **WiFi:** a raw `set` on WiFi paths does nothing until the apply flags are sent last in the same call:
  `set Device.WiFi.Radio.1.Channel 6 Device.WiFi.Radio.1.X_CISCO_COM_ApplySetting true Device.WiFi.Radio.2.X_CISCO_COM_ApplySetting false`
* **Tables** (port forwards, DHCP reservations, WiFi schedule) cannot be written with `set`, which only sends strings and booleans. Use the `ports` commands, or `Router.table_write(root, rows=[...], modify=[...], delete=[...])` from Python. A new row has `"index": 0`; an edited row sends its full field set with its index and lists that index in `modify`; a deleted row is only listed in `delete`.

## Extending the CLI

* Everything lives in `speedport.py`. Add a `cmd_<name>(router, args)` function and register it in `build_parser()`. Use `router.get(...)`, `router.set({...})`, `router.table_write(...)`, `table(values, root)` for `{index: {field: value}}`, and `print_table(...)`.
* Raise `RouterError` for expected failures; `main()` turns it into `speedport: <msg>` and exit 1.
* Standard library only. Keep it one file.
* New pure logic gets an assert in `test_speedport.py`. Verify new read commands against the router; for new write commands, get the user's OK, snapshot the affected paths first (`get ... > snapshot.json`), and restore if the result is wrong.
