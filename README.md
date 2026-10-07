# Speedport Plus 2 CLI

Manage a Cosmote **Speedport Plus 2** (Sercomm VD4224BDTP, tested on firmware 2.9.003.4) from the terminal. No browser needed.

One file, Python 3.8+ standard library only.

> The scripts for the original Speedport Plus (`/data/*.json` API, firmware 09022001.00.0xx) were removed. They live in git history up to commit `07dc87f`.

## Setup

Create `.env` next to `speedport.py` (it is gitignored):

```
USER=admin
PASS=your router password
# optional, for watch-devices / dyndns notifications
TELEGRAM_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

```
ln -s "$PWD/speedport.py" ~/.local/bin/speedport
speedport status
```

Set `SPEEDPORT_HOST` if the router is not on `192.168.1.1`.

## Commands

```
speedport status                         # model, uptime, WAN link, IPv6, DNS
speedport devices [--all]                # connected devices (wifi band / ethernet, RSSI)
speedport reboot [-y]

speedport wifi [--show-password]         # all networks + radios
speedport wifi set main --ssid Home --password      # prompts for the password
speedport wifi set guest --on --security wpa2
speedport wifi set 5 --hidden            # roles: main guest 2.4 5 guest-2.4 guest-5
speedport wifi wps                       # push button pairing

speedport ports                          # port forwards, flags ones unreachable under MAP-E
speedport ports add 1424 192.168.1.20 [internal_port] [--protocol TCP|UDP|BOTH] [--name ssh]
speedport ports add 1424-1430 192.168.1.20
speedport ports rm 2
speedport dmz | dmz set 192.168.1.20 | dmz off

speedport public-ip
speedport cgnat                          # is the IPv4 shared, which ports can be forwarded
speedport watch-devices                  # print + Telegram (dis)connected devices since last run
speedport dyndns home.example.com --run 'curl -s "https://dns.example/update?ip=$SPEEDPORT_IP"'

speedport get Device.WiFi.SSID.1.        # raw TR-181 read, trailing dot = subtree
speedport set Device.NAT.X_CISCO_COM_DMZ.Enable 0   # raw write, true/false become booleans
```

Every read command (`status`, `devices`, `wifi`, `ports`, `dmz`, `public-ip`, `cgnat`) takes `--json`, e.g. `speedport devices --json | jq -r '.[].name'`. See `AGENTS.md` for the shapes.

Cron example:

```
*/5 * * * * /home/me/.local/bin/speedport watch-devices
*/10 * * * * /home/me/.local/bin/speedport dyndns home.example.com --run '/home/me/update-dns.sh'
```

## Things worth knowing

* **Shared IPv4 (MAP-E).** Cosmote fibre gives many customers the same public IPv4 and each one a slice of its ports (a PSID). The router only knows the MAP rule, not the public IPv4, so `public-ip` asks `api.ipify.org`. Port forwards outside your slice never receive IPv4 traffic. `cgnat` lists your slice and `ports` / `ports add` warn about rules outside it. IPv6 is not affected.
* **TLS.** The router uses a self-signed certificate. The CLI pins its SHA-256 on first use (`~/.cache/speedport/cert.sha256`) and refuses to connect if it changes. Delete that file after a firmware update that replaces the certificate.
* **Session.** The session cookie is cached in `~/.cache/speedport/session.json` (mode 0600) and renewed automatically. Logging in through the CLI logs out a browser session and vice versa.
* **Wrong password.** Login is not retried: the router delays further attempts after failed logins.
* **Call logs** are not available. This model exposes no voice data (the web UI call log page is static demo data).

## How the API works

Useful if you want to add a command. Everything is under `https://192.168.1.1`.

1. `GET /login.html` and read `csrf_token = '...'` from the inline script.
2. `POST /data/login.json?_=<ms>&csrf_token=<tok>` with form body `LoginName=<user>&LoginPWD=<sha256 hex of password>`. Reply `"1"` is success (sets the `session_id` cookie), `"3"`/`"4"` wrong credentials, `"5"` locked out, `"6"` another session is active (send again to kick it). Over plain HTTP the UI encrypts everything with SJCL instead; HTTPS is plain JSON.
3. `POST /data/data.cgi?_=<ms>&csrf_token=<tok>` with a JSON-RPC 2.0 batch on the RDK-B / TR-181 data model:

```json
[{"jsonrpc":"2.0","method":"GET","params":"Device.Hosts.Host.","id":1},
 {"jsonrpc":"2.0","method":"SET","params":{"Device.NAT.X_CISCO_COM_DMZ.Enable":"0"},"id":2}]
```

A SET replies `{"code":200,"message":"SCM_SUCCESS"}`. A missing path reads as `"N/A"`.

* **Tables** (port forwards, DHCP reservations) have no ADD/DEL. Send two SETs: `{"action.Device.NAT.PortMapping@":{"modify":"","delete":"2"}}` and `{"Device.NAT.PortMapping@":[rows]}`, where a new row has `"index":0` and an edited row has its index listed in `modify`.
* **WiFi** changes only apply when the batch ends with `Device.WiFi.Radio.1.X_CISCO_COM_ApplySetting` and `Device.WiFi.Radio.2.X_CISCO_COM_ApplySetting` (true for each radio you touched). Index map: SSID/AccessPoint 1 = 2.4 GHz, 2 = 5 GHz, 3 = guest 2.4 GHz, 4 = guest 5 GHz.
* **Reboot** is `Device.DeviceInfo.X_RDKCENTRAL-COM_UI_ACCESS=reboot_device` followed by `Device.X_CISCO_COM_DeviceControl.RebootDevice=Device,delay=3`.

The web UI JavaScript (`/js/*.js`, e.g. `wifi_general.js`, `internet_port_mappin.js`) shows the exact payload for every page.

## Tests

```
python3 test_speedport.py
```
