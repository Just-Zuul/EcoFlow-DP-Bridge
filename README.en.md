# EcoFlow DELTA Pro (DP) Bridge

*[Deutsche Version → README.md](README.md)*

A local, cloud-free TCP → MQTT bridge for the **EcoFlow DELTA Pro
GEN 1** (Product ID 14).

The bridge sits between the DP and Home Assistant, decodes the binary
`aa02` protocol and publishes everything via MQTT with full
**Home Assistant auto-discovery** — including writable switches,
numbers and selects so you can actually *control* the DP from HA,
not just read it.

No EcoFlow account needed. No cloud. Everything stays local in your LAN.

## ⚠️ Notice & Disclaimer

This repository solely describes how I personally control and read out my own EcoFlow DeltaPro
locally and directly by means of a local container.

**It is neither a guide nor an invitation to replicate this setup.**

### Use at your own risk

Anyone who uses, implements, replicates or otherwise makes use of the contents of this
repository – in whole or in part – does so **entirely at their own risk and on their own
responsibility**. Every person using these contents is assumed to possess the necessary
fundamental knowledge as well as an awareness of the applicable legal regulations.

### Limitation of liability

The author assumes **no liability whatsoever** for:

- Harm to people, animals or property
- Damage to devices, installations or the electrical system
- Data loss or malfunctions
- Consequential damages of any kind
- Violations of manufacturer warranty terms or certifications
- Violations of legal regulations or standards

This applies regardless of whether damage arises from direct use, modification or faulty
implementation of the methods described here.

### No warranty

The information provided here is supplied **without any warranty** – neither express nor
implied. No guarantee is given as to correctness, completeness, timeliness or functionality.

### Trademarks & third parties

EcoFlow and DeltaPro are registered trademarks of EcoFlow Technology Inc. This project is
not affiliated with EcoFlow and is neither endorsed nor approved by EcoFlow.

---

> **Status:** Battle-tested in production with 3× DELTA Pro, 24/7
> without intervention. Multi-language support for the friendly names
> in Home Assistant.

---

## Features

- **Local only.** The DP talks to your bridge instead of the
  EcoFlow cloud.
- **Home Assistant auto-discovery.** Sensors, switches, numbers,
  selects, connectivity binary_sensor, last-seen timestamp — all
  created automatically.
- **Multi-device.** One container handles several DPs, identified by
  source IP.
- **Writable controls** (commands flow DP-ward, not just data out):
  - Switches: AC Out, X-Boost, DC Out (12V), Beep
  - Numbers: AC Charging Limit, Battery Max Charge, Battery Min Discharge
  - Selects: Screen Timeout, Unit Timeout
- **Energy-dashboard ready.** Lifetime in/out counters (Wh) with
  `state_class: total_increasing`.
- **Light on the MQTT broker.** Publish-on-change + per-unit
  tolerance windows — the broker doesn't drown in measurement jitter.
- **Multi-language.** Friendly names from JSON files, one per
  language. Ships with German and English; add your own in 10 minutes.

---

## How it works

The DELTA Pro insists on connecting to `mqtt-e.ecoflow.com`. It has no
"fully local" mode. The trick: **lie to it via DNS.** When the DP
resolves the EcoFlow hostname, your local DNS server answers with the
bridge's IP. The DP opens a normal TCP connection — to the bridge.

```
   ┌────────┐                  ┌──────────────┐                ┌────────────┐
   │ DP #1  │── TCP :6500 ────▶│              │── MQTT pub ───▶│  Home      │
   ├────────┤   (aa02 frames)  │  DP-Bridge   │                │  Assistant │
   │ DP #2  │── TCP :6500 ────▶│  (this repo) │◀── MQTT cmds ──│            │
   └────────┘                  └──────▲───────┘                └────────────┘
        │                             │
        │ DNS for                     │ DNS, discovery, publish
        │ mqtt-e.ecoflow.com          │
        ▼                             │
   ┌──────────────────────────────────▼──┐
   │  Local DNS (AdGuard (tested) /      │
   │  Pi-hole / Unbound / dnsmasq / ...) │
   │  Rewrite: *.ecoflow.com → bridge IP │
   └─────────────────────────────────────┘
```

The DPs **must not** be able to reach the real `*.ecoflow.com`
infrastructure on the internet. If they can, they will, and you'll see
silence on your bridge. Two ways to enforce this:

- **No-internet route.** Put the DPs on a network segment whose
  gateway blocks outbound internet for those source IPs.
- **DNS only.** Override the EcoFlow hostnames at the DNS level and
  let the DPs have internet otherwise. As long as the hostnames
  resolve to your bridge, they won't find the cloud.

The author runs the second variant (DNS redirect on AdGuard, no VLAN,
just a separate physical LAN port on the NAS).

---

## Prerequisites

| What | What for | Example |
|---|---|---|
| **A DELTA Pro GEN 1** (Product ID 14) | The device the bridge talks to | EcoFlow DELTA Pro |
| **Docker + Docker Compose** | Where the bridge runs | Synology, QNAP, any Linux box |
| **An MQTT broker** | Where the values land | Mosquitto, EMQX, HA Mosquitto add-on |
| **Local DNS server with rewrites** | The actual workaround | AdGuard Home, Pi-hole, dnsmasq, Unbound |
| **Home Assistant** *(optional)* | What consumes the MQTT | Any version with MQTT integration |
| **Bridge host reachable from DP network** | DPs must be able to route to the bridge IP | Same subnet or routed |

Other models (DELTA Max, Mini, RIVER, newer generations) are neither
tested nor will they be implemented by me — this bridge is
specifically built for GEN 1 (Product ID 14), because functional
repos for the successor models already exist.

---

## Setup

> **Order matters.** First set up DNS/DHCP, then move the DPs into
> this network and assign fixed IPs, *then* edit the compose file —
> otherwise you don't have any IPs to put into `DEVICE_MAP` yet.

### 1. Set up DNS redirect

The bridge does not work without this.

The DELTA Pro queries these hostnames (verified, but EcoFlow may
change them in the future — packet-sniff to confirm if something
stops working):

- `mqtt-e.ecoflow.com`
- `tcp-e.ecoflow.com`

Both must resolve to **the bridge's IP** for clients on the DP network.

#### Option A: AdGuard Home

DNS rewrites → Add rewrite:

| Domain | Answer |
|---|---|
| `mqtt-e.ecoflow.com` | `192.168.x.x` *(bridge IP)* |
| `tcp-e.ecoflow.com`  | `192.168.x.x` *(bridge IP)* |

Then point your DP-network DHCP at AdGuard as the DNS server, or
override the DNS server in the static IP config of the DPs.

#### Option B: Pi-hole

Local DNS → DNS Records: same idea, one entry per hostname.

#### Option C: dnsmasq

```
address=/mqtt-e.ecoflow.com/192.168.x.x
address=/tcp-e.ecoflow.com/192.168.x.x
```

#### Option D: Unbound

```
local-zone: "mqtt-e.ecoflow.com" redirect
local-data: "mqtt-e.ecoflow.com A 192.168.x.x"
local-zone: "tcp-e.ecoflow.com" redirect
local-data: "tcp-e.ecoflow.com A 192.168.x.x"
```

**Verify the redirect** before continuing. From a machine on the DP
network:

```bash
nslookup mqtt-e.ecoflow.com   # → must show the bridge IP
nslookup tcp-e.ecoflow.com    # → must show the bridge IP
```

If they still resolve to EcoFlow's public IPs, the DPs will too, and
the bridge will see nothing.

### 2. Move the DPs into the controlled network

The DPs need to be in a network where your DNS server is responsible
for them. **How you get them there is up to you** — common variants:

- Separate subnet with its own AP/SSID
- Separate VLAN
- Dedicated physical LAN port on the DHCP/DNS host

> **⚠️ Important about Wi-Fi config:** Some EcoFlow devices *first*
> query `api-e.ecoflow.com` when changing Wi-Fi, get credentials and
> MQTT URL from there, and *then* connect to `mqtt-e.ecoflow.com`.
> If the `api-e` request fails (e.g. no internet in the DP network),
> the DP stalls and never reaches you.
>
> Recommendation: **don't change the Wi-Fi settings of an
> already-configured DP.** If the DP already knows an SSID and you
> offer the same SSID (same name + password) on your controlled AP,
> it will connect there after a reboot without further app
> configuration.

### 3. Assign fixed IPs to the DPs

The DPs need fixed IPs because `DEVICE_MAP` identifies devices by
source IP. The cleanest path is using a DNS server that can also do
DHCP (AdGuard Home, Pi-hole and dnsmasq can all do this):

1. Let the DP connect to the controlled network for the first time —
   it gets a dynamic IP at first.
2. In your AdGuard's (or Pi-hole/dnsmasq) DHCP section, create a
   **static lease / reservation** based on the DP's Wi-Fi MAC: MAC →
   desired fixed IP.
3. Power-cycle the DP briefly — it now picks up the fixed IP.
4. Remember that fixed IP — it goes into `DEVICE_MAP` next.

Advantage of this variant: the DHCP-pushed DNS server automatically
points at the same host doing the DNS rewrites. No separate DNS
configuration needed.

**If your DHCP server is NOT on the same host as the DNS server:**
push your DNS host's address explicitly in the DHCP options — and
make sure the DPs actually pick it up (some firmware insists on a
hard-coded Google DNS). If in doubt, run `tcpdump -i <interface>
port 53` on the DNS host and verify the DPs really query there.

### 4. Clone the repo and edit compose

```bash
git clone <this-repo>
cd ecoflow-dp-bridge
cp docker-compose.example.en.yml docker-compose.yml
```

Edit `docker-compose.yml` — every value you need to change is marked
`# EDIT ME`. The minimum is:

- `MQTT_HOST` — your broker's IP
- `DEVICE_MAP` — `<DP-IP>=<short-name>` per DP (the fixed IPs from
  step 3)
- `LANGUAGE` — `de`, `en`, or your own code

### 5. Build and start

```bash
docker compose build
docker compose up -d
docker compose logs -f dp-bridge
```

You should see:

```
DP-Bridge v4.8.1 starting:
  listen=:6500  mqtt=192.168.1.100:1883
  devices (2):
    dp1  (EcoFlow Delta Pro 1)  ip=192.168.10.150  id=ecoflow_dp1
    dp2  (EcoFlow Delta Pro 2)  ip=192.168.10.160  id=ecoflow_dp2
loaded 64 translations from /app/translations.en.json (LANGUAGE=en)
MQTT connected to 192.168.1.100:1883
HA-Discovery: 2 devices × 41 sensors, 4 switches, 3 numbers, 2 selects
Listening on 0.0.0.0:6500
```

Within a few seconds (after the DPs reconnect), lines like this appear:

```
[dp1] DP connected: 192.168.10.150:54321
```

### 6. Verify in Home Assistant

If your MQTT integration is set up with auto-discovery enabled
(default), you'll find a new device `EcoFlow Delta Pro 1` under
**Settings → Devices & Services → MQTT**. Open it — you'll see
~50 entities, all named in your chosen language.

---

## Configuration

All settings via environment variables on the container.

| Variable | Default | Purpose |
|---|---|---|
| `DP_LISTEN_PORT` | `6500` | TCP port the bridge listens on. |
| `MQTT_HOST` | `192.168.1.100` | MQTT broker IP/hostname. **Must be changed.** |
| `MQTT_PORT` | `1883` | MQTT broker port. |
| `MQTT_USER` | *(empty)* | MQTT username, if your broker requires auth. |
| `MQTT_PASS` | *(empty)* | MQTT password. |
| `DEVICE_MAP` | *(empty)* | `IP1=name1\|IP2=name2\|...` — required. |
| `LANGUAGE` | `de` | Selects `translations.<LANGUAGE>.json`. |
| `TRANSLATIONS_DIR` | `/app` | Search directory for translation files. |
| `TRANSLATIONS_PATH` | *(empty)* | Optional override: full path to one translation file. Overrides `LANGUAGE`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `POLL_INTERVAL` | `5` | Seconds between poll rounds per device. |
| `IDLE_TIMEOUT` | `30` | Seconds without a frame before the device is marked offline. |
| `BRIDGE_VERSION` | `v4.8.1` | Cosmetic, shown in MQTT device info. |

---

## Multi-device setup

`DEVICE_MAP` maps DP source IPs to short names:

```yaml
DEVICE_MAP: "192.168.10.150=dp1|192.168.10.160=dp2|192.168.10.170=dp3"
```

Each DP gets its own Home Assistant device and its own set of entities
(`sensor.ecoflow_dp1_battery_soc`, `sensor.ecoflow_dp2_battery_soc`,
…). Commands are routed by device name too:

```
ecoflow/dp1/cmd/ac_out/set                ON
ecoflow/dp2/cmd/battery_level_max/set     85
```

The bridge keeps an independent send/receive session per DP, so a
slow or restarting DP doesn't affect the others.

---

## Adding your own language

1. Copy `translations.de.json` (or `translations.en.json` — whichever
   is closer to your target language) to a new file.
2. Name it `translations.<your-code>.json`, e.g.
   `translations.fr.json`. The code is freely chosen — `LANGUAGE=fr`
   loads `translations.fr.json`.
3. Translate the `name`/`label` values. **Keys stay unchanged.**
   Unknown keys (or missing entries) silently fall back to the
   English code defaults.
4. Mount the file as a volume in `docker-compose.yml`:

   ```yaml
   volumes:
     - ./translations.fr.json:/app/translations.fr.json:ro
   ```

5. Set `LANGUAGE: "fr"` and restart the container:

   ```bash
   docker compose up -d
   ```

6. Discovery topics are republished on every bridge start, so HA
   picks up the new names immediately.

PRs adding more languages are welcome.

---

## Troubleshooting

### "DP connected" shows up, but sensors don't update

The DP is connected but isn't sending any frames right now. Check the
log for `decode_packet error` — if that appears, it's protocol drift
(rare; open an issue with a hex dump).

### No "DP connected" line at all

The DPs aren't reaching the bridge. Check:

1. **DNS redirect actually works**:
   `nslookup mqtt-e.ecoflow.com` from inside the DP network — answer
   must be the bridge IP.
2. **DPs can actually reach the bridge IP**:
   from a host on the DP network, `nc -zv <bridge-ip> 6500`.
3. **DPs are secretly still talking to the cloud**:
   if outbound internet to `*.ecoflow.com` isn't blocked and the DPs
   resolved the hostname before your redirect was in place, they may
   have cached the address. Power-cycle the DPs after fixing DNS.
4. **Firewall on the bridge host**: port 6500 (or whatever you chose)
   must be reachable.

### "connection from unknown IP" in the log

A DP is reaching the bridge but its IP isn't in `DEVICE_MAP`. Either
add it, or check why an unexpected host is hitting your port.

### Home Assistant shows entities, but all "Unavailable"

Either the bridge has disconnected from MQTT, or the DP is genuinely
offline. Check:

- `mosquitto_sub -h <broker> -t 'ecoflow/+/availability' -v`
  → should show `online` for each connected DP.
- Container log for MQTT reconnects.

### Battery State of Health looks weird

Computed from `battery_capacity_full / 80,000 mAh × 100`. The 80,000
mAh design capacity is hardcoded for the DELTA Pro
(`DESIGN_CAPACITY_MAH` in `dp_bridge.py`).

---

## Repository layout

```
.
├── README.md                       ← German (primary)
├── README.en.md                    ← you are here
├── LICENSE.md                      ← terms of use
├── Dockerfile
├── docker-compose.example.de.yml   ← copy → docker-compose.yml, edit - German Version
├── docker-compose.example.en.yml   ← copy → docker-compose.yml, edit - English Version 
├── dp_bridge.py                    ← main daemon
├── ecoflow_codec.py                ← CRC, frame parsing primitives
├── ecoflow_receive.py              ← decoders (BMS / EMS / inverter / MPPT / PD)
├── ecoflow_send.py                 ← command builders (writable controls)
├── translations.de.json            ← German friendly names
├── translations.en.json            ← English friendly names (also code default)
└── .gitignore
```

---

## Credits

`ecoflow_codec.py`, `ecoflow_receive.py` and `ecoflow_send.py` are
based on the reverse-engineering work of the community around the
EcoFlow `aa02` protocol. The bridge daemon, the Home Assistant
discovery layer, the multi-device session handling, the
publish-on-change cache and the multi-language support are this
project's own code.

If you spot something that came from elsewhere and isn't properly
attributed — please open an issue.

---

## License

Hobby project — not a formal license. Details in
[LICENSE.md](LICENSE.md). Short: personal use yes, commercial use no,
no warranties.
