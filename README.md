# EcoFlow DELTA Pro (DP) Bridge

*[English version → README.en.md](README.en.md)*

Eine lokale, cloud-freie TCP → MQTT-Brücke für die **EcoFlow DELTA Pro
GEN 1** (Product ID 14).

Die Bridge sitzt zwischen DP und Home Assistant, dekodiert das binäre
`aa02`-Protokoll und published alles per MQTT mit voller
**Home Assistant Auto-Discovery** — inklusive schreibbarer Switches,
Numbers und Selects, sodass du die DP aus HA heraus auch *steuern*
kannst, nicht nur lesen.

Kein EcoFlow-Account nötig. Keine Cloud. Alles bleibt lokal in deinem LAN.

## ⚠️ Hinweis & Haftungsausschluss

Dieses Repository beschreibt ausschließlich, wie ich persönlich meine EcoFlow DeltaPro mittels
eines lokalen Containers direkt und lokal steuere und auslese.

**Es handelt sich weder um eine Anleitung noch um eine Aufforderung zum Nachbau.**

### Nutzung auf eigenes Risiko

Wer Inhalte dieses Repositories – ganz oder in Teilen – verwendet, umsetzt, nachbaut oder
anderweitig nutzt, tut dies **ausschließlich auf eigene Gefahr und eigenes Risiko**.
Von jeder Person, die diese Inhalte verwendet, wird vorausgesetzt, dass sie über das
notwendige Grundlagenwissen sowie den jeweils geltenden gesetzlichen Vorschriften verfügt.

### Haftungsausschluss

Der Autor übernimmt **keinerlei Haftung** für:

- Schäden an Personen, Tieren oder Sachen
- Schäden an Geräten, Anlagen oder der Elektroinstallation
- Datenverluste oder Fehlfunktionen
- Folgeschäden jeglicher Art
- Verstöße gegen Hersteller-Garantiebedingungen oder Zertifizierungen
- Verstöße gegen gesetzliche Vorschriften oder Normen

Dies gilt unabhängig davon, ob Schäden durch direkte Nutzung, Modifikation oder fehlerhafte
Umsetzung der hier beschriebenen Methoden entstehen.

### Keine Gewährleistung

Die hier bereitgestellten Informationen werden **ohne jegliche Gewährleistung** bereitgestellt –
weder ausdrücklich noch stillschweigend. Es wird keine Garantie für Richtigkeit,
Vollständigkeit, Aktualität oder Funktionsfähigkeit übernommen.

### Marken & Drittanbieter

EcoFlow und DeltaPro sind eingetragene Marken der EcoFlow Technology Inc. Dieses Projekt
steht in keiner Verbindung zu EcoFlow und wird von EcoFlow weder unterstützt noch genehmigt.

---

> **Status:** Im Produktivbetrieb getestet mit 3× DELTA Pro,
> 24/7 ohne Eingriff. Multi-Language-Support für die Friendly Names
> in Home Assistant.

---

## Features

- **Lokal only.** Die DP spricht mit deiner Bridge statt mit der
  EcoFlow-Cloud.
- **Home-Assistant Auto-Discovery.** Sensoren, Switches, Numbers,
  Selects, Connectivity-Binary-Sensor, Last-Seen-Timestamp — alles
  wird automatisch angelegt.
- **Multi-Device.** Ein Container für mehrere DPs, identifiziert über
  die Quell-IP.
- **Schreibbare Steuerungen** (Befehle gehen DP-wärts, nicht nur Daten
  raus):
  - Switches: AC Out, X-Boost, DC Out (12V), Beep
  - Numbers: AC Charging Limit, Battery Max Charge, Battery Min Discharge
  - Selects: Screen Timeout, Unit Timeout
- **Energy-Dashboard-fähig.** Lebenslange In/Out-Zähler (Wh) mit
  `state_class: total_increasing`.
- **Sparsam zum MQTT-Broker.** Publish-on-Change + Toleranz-Fenster pro
  Einheit — der Broker erstickt nicht im Mess-Rauschen.
- **Multi-Language.** Friendly Names aus JSON-Dateien, eine pro Sprache.
  Geliefert werden Deutsch und Englisch; eigene Sprache in 10 Minuten
  ergänzt.

---

## Wie es funktioniert

Die DELTA Pro besteht darauf, sich mit `mqtt-e.ecoflow.com` zu
verbinden. Sie hat keinen "rein lokalen" Modus. Der Trick: **täusche sie
per DNS.** Wenn die DP den EcoFlow-Hostnamen auflöst, antwortet dein
lokaler DNS-Server mit der Bridge-IP. Die DP öffnet eine ganz normale
TCP-Verbindung — zur Bridge.

```
   ┌────────┐                  ┌──────────────┐                ┌────────────┐
   │ DP #1  │── TCP :6500 ────▶│              │── MQTT pub ───▶│  Home      │
   ├────────┤   (aa02 frames)  │  DP-Bridge   │                │  Assistant │
   │ DP #2  │── TCP :6500 ────▶│ (dieses Repo)│◀── MQTT cmds ──│            │
   └────────┘                  └──────▲───────┘                └────────────┘
        │                             │
        │ DNS für                     │ DNS, Discovery, Publish
        │ mqtt-e.ecoflow.com          │
        ▼                             │
   ┌──────────────────────────────────▼──┐
   │  Lokaler DNS (AdGuard (getestet) /  │
   │  Pi-hole / Unbound / dnsmasq / ...) │
   │  Rewrite: *.ecoflow.com → Bridge-IP │
   └─────────────────────────────────────┘
```

Die DPs **dürfen die echte** `*.ecoflow.com`-Infrastruktur im Internet
**nicht erreichen.** Wenn sie es können, tun sie es auch, und du siehst
Stille auf deiner Bridge. Zwei Wege:

- **Kein Internet-Routing.** Die DPs in ein Netzsegment stecken, dessen
  Gateway ausgehendes Internet für diese Quell-IPs blockt.
- **Nur DNS.** EcoFlow-Hostnamen am DNS überschreiben und sonst Internet
  zulassen. Solange die Hostnamen auf deine Bridge zeigen, finden die
  DPs die Cloud nicht.

Der Autor nutzt die zweite Variante (DNS-Redirect auf AdGuard, kein
VLAN, einfach ein separater physischer LAN-Port am NAS).

---

## Voraussetzungen

| Was | Wofür | Beispiel |
|---|---|---|
| **Eine DELTA Pro GEN 1** (Product ID 14) | Das Gerät, mit dem die Bridge redet | EcoFlow DELTA Pro |
| **Docker + Docker Compose** | Wo die Bridge läuft | Synology, QNAP, Linux-Kiste |
| **Ein MQTT-Broker** | Wo die Werte landen | Mosquitto, EMQX, HA-Mosquitto-Add-on |
| **Lokaler DNS-Server mit Rewrites** | Der eigentliche Workaround | AdGuard Home, Pi-hole, dnsmasq, Unbound |
| **Home Assistant** *(optional)* | Was das MQTT konsumiert | jede Version mit MQTT-Integration |
| **Bridge-Host im DP-Netz erreichbar** | DPs müssen die Bridge-IP routen können | gleiches Subnetz oder geroutet |

Andere Modelle (DELTA Max, Mini, RIVER, neuere Generationen) sind weder
getestet noch werden sie von mir implementiert — diese Brücke ist
speziell für die GEN 1 (Product ID 14) entwickelt, da es für die
Nachfolger bereits funktionale Repos gibt.

---

## Setup

> **Reihenfolge ist wichtig.** Erst DNS/DHCP einrichten, dann die DPs
> in dieses Netz bringen und feste IPs vergeben, *dann* erst die
> compose anpassen — sonst hast du in `DEVICE_MAP` noch keine IPs
> einzutragen.

### 1. DNS-Redirect einrichten

Die Bridge funktioniert ohne das nicht.

Die DELTA Pro fragt diese Hostnamen ab (verifiziert, aber EcoFlow kann
das in Zukunft ändern — bei Problemen Paketmitschnitt machen):

- `mqtt-e.ecoflow.com`
- `tcp-e.ecoflow.com`

Beide müssen für Clients im DP-Netz **auf die Bridge-IP** auflösen.

#### Option A: AdGuard Home

DNS-Rewrites → Rewrite hinzufügen:

| Domain | Antwort |
|---|---|
| `mqtt-e.ecoflow.com` | `192.168.x.x` *(Bridge-IP)* |
| `tcp-e.ecoflow.com`  | `192.168.x.x` *(Bridge-IP)* |

Dann den AdGuard als DNS-Server im DHCP des DP-Netzes setzen oder den
DNS-Server in der statischen IP-Konfig der DPs überschreiben.

#### Option B: Pi-hole

Local DNS → DNS Records: gleiches Prinzip, ein Eintrag pro Hostname.

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

**Redirect verifizieren** vor dem Weitermachen. Von einer Maschine im
DP-Netz:

```bash
nslookup mqtt-e.ecoflow.com   # → muss die Bridge-IP zeigen
nslookup tcp-e.ecoflow.com    # → muss die Bridge-IP zeigen
```

Wenn die noch immer auf EcoFlows öffentliche IPs zeigen, tun das die
DPs auch — und die Bridge sieht nichts.

### 2. DPs ins kontrollierte Netz bringen

Die DPs müssen in einem Netz sein, in dem dein DNS-Server für sie
zuständig ist. **Wie du sie dahin bringst, ist deine Sache** —
typische Varianten:

- Separates Subnetz mit eigenem AP/SSID
- Separates VLAN
- Eigener physischer LAN-Port am DHCP/DNS-Host

> **⚠️ Wichtig zur WLAN-Konfig:** Manche EcoFlow-Geräte fragen beim
> WLAN-Wechsel *zuerst* `api-e.ecoflow.com` ab, bekommen erst von dort
> Username/Passwort/MQTT-URL und gehen *dann* zu `mqtt-e.ecoflow.com`.
> Wenn die `api-e`-Anfrage scheitert (z.B. kein Internet im DP-Netz),
> bleibt die DP stehen und kommt nie bei dir an.
>
> Empfehlung: **WLAN-Einstellungen einer bereits konfigurierten DP
> nicht ändern.** Wenn die DP schon eine SSID kennt und du dieselbe
> SSID (gleicher Name + Passwort) auf deinem kontrollierten AP
> anbietest, verbindet sie sich nach einem Neustart ohne erneute
> App-Konfiguration dorthin.

### 3. Feste IPs für die DPs vergeben

Die DPs brauchen feste IPs, weil `DEVICE_MAP` Geräte über die Quell-IP
identifiziert. Der eleganteste Weg ist, einen DNS-Server zu nutzen, der
auch DHCP kann (AdGuard Home, Pi-hole und dnsmasq können das alle):

1. DP sich erstmals mit dem kontrollierten Netz verbinden lassen — sie
   bekommt zunächst eine dynamische IP.
2. Im DHCP-Bereich deines AdGuard (oder Pi-hole/dnsmasq) anhand der
   WLAN-MAC der DP eine **Static-Lease/Reservation** anlegen: MAC →
   gewünschte feste IP.
3. DP einmal kurz stromlos schalten — sie holt sich jetzt die feste IP.
4. Diese feste IP merken — sie kommt gleich in `DEVICE_MAP`.

Vorteil dieser Variante: der DNS-Server-Push via DHCP zeigt automatisch
auf den gleichen Host, der auch die DNS-Rewrites macht. Keine separate
DNS-Konfiguration nötig.

**Falls dein DHCP NICHT auf dem gleichen Host wie der DNS-Server läuft:**
explizit in den DHCP-Optionen den DNS-Server-Push auf deinen DNS-Host
zeigen lassen — und sicherstellen, dass die DPs den auch wirklich
übernehmen (statt eines hartkodierten Google-DNS aus der DP-Firmware).
Im Zweifel mit `tcpdump -i <interface> port 53` auf dem DNS-Host
mitschnüffeln, ob die DPs ihre EcoFlow-Anfragen wirklich dort stellen.

### 4. Repo klonen und compose anpassen

```bash
git clone <dieses-repo>
cd ecoflow-dp-bridge
cp docker-compose.example.de.yml docker-compose.yml
```

Editiere `docker-compose.yml` — jeder Wert, den du ändern musst, ist
mit `# EDIT ME` markiert. Das Minimum ist:

- `MQTT_HOST` — die IP deines Brokers
- `DEVICE_MAP` — `<DP-IP>=<Kurzname>` pro DP (die festen IPs aus
  Schritt 3)
- `LANGUAGE` — `de`, `en` oder ein eigener Code

### 5. Bauen und starten

```bash
docker compose build
docker compose up -d
docker compose logs -f dp-bridge
```

Du solltest sehen:

```
DP-Bridge v4.8.1 starting:
  listen=:6500  mqtt=192.168.1.100:1883
  devices (2):
    dp1  (EcoFlow Delta Pro 1)  ip=192.168.10.150  id=ecoflow_dp1
    dp2  (EcoFlow Delta Pro 2)  ip=192.168.10.160  id=ecoflow_dp2
loaded 64 translations from /app/translations.de.json (LANGUAGE=de)
MQTT connected to 192.168.1.100:1883
HA-Discovery: 2 devices × 41 sensors, 4 switches, 3 numbers, 2 selects
Listening on 0.0.0.0:6500
```

Innerhalb weniger Sekunden (sobald die DPs sich neu verbinden) kommen
Zeilen wie:

```
[dp1] DP connected: 192.168.10.150:54321
```

### 6. In Home Assistant prüfen

Wenn deine MQTT-Integration mit Auto-Discovery konfiguriert ist (Default
ist an), findest du ein neues Gerät `EcoFlow Delta Pro 1` unter
**Einstellungen → Geräte & Dienste → MQTT**. Aufmachen — du siehst
~50 Entities, alle in deiner gewählten Sprache benannt.

---

## Konfiguration

Alle Einstellungen via Environment-Variablen auf dem Container.

| Variable | Default | Zweck |
|---|---|---|
| `DP_LISTEN_PORT` | `6500` | TCP-Port, auf dem die Bridge lauscht. |
| `MQTT_HOST` | `192.168.1.100` | MQTT-Broker IP/Hostname. **Muss geändert werden.** |
| `MQTT_PORT` | `1883` | MQTT-Broker-Port. |
| `MQTT_USER` | *(leer)* | MQTT-Username, falls der Broker Auth verlangt. |
| `MQTT_PASS` | *(leer)* | MQTT-Passwort. |
| `DEVICE_MAP` | *(leer)* | `IP1=name1\|IP2=name2\|...` — Pflicht. |
| `LANGUAGE` | `de` | Wählt `translations.<LANGUAGE>.json`. |
| `TRANSLATIONS_DIR` | `/app` | Suchverzeichnis für Translation-Dateien. |
| `TRANSLATIONS_PATH` | *(leer)* | Legacy: voller Pfad zu einer Translation-Datei. Überschreibt `LANGUAGE`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `POLL_INTERVAL` | `5` | Sek. zwischen Poll-Runden pro Gerät. |
| `IDLE_TIMEOUT` | `30` | Sek. ohne Frame, bis das Gerät offline gemeldet wird. |
| `BRIDGE_VERSION` | `v4.8.1` | Kosmetik, wird im MQTT-Device-Info angezeigt. |

---

## Multi-Device-Setup

`DEVICE_MAP` mapped DP-Quell-IPs auf Kurznamen:

```yaml
DEVICE_MAP: "192.168.10.150=dp1|192.168.10.160=dp2|192.168.10.170=dp3"
```

Jede DP bekommt ihr eigenes Home-Assistant-Gerät und ihre eigene Menge
an Entities (`sensor.ecoflow_dp1_battery_soc`,
`sensor.ecoflow_dp2_battery_soc`, …). Befehle werden ebenfalls per
Gerätenamen geroutet:

```
ecoflow/dp1/cmd/ac_out/set                ON
ecoflow/dp2/cmd/battery_level_max/set     85
```

Die Bridge hält pro DP eine eigene Send/Receive-Session, sodass eine
langsame oder neu startende DP die anderen nicht beeinträchtigt.

---

## Eigene Sprache hinzufügen

1. Kopiere `translations.de.json` (oder `translations.en.json` — was
   näher an deiner Zielsprache ist) in eine neue Datei.
2. Benenne sie `translations.<dein-code>.json`, z.B.
   `translations.fr.json`. Der Code ist frei wählbar — `LANGUAGE=fr`
   lädt `translations.fr.json`.
3. Übersetze die `name`/`label`-Werte. **Keys bleiben unverändert.**
   Unbekannte Keys (oder fehlende Einträge) fallen still auf die
   englischen Code-Defaults zurück.
4. Datei als Volume in `docker-compose.yml` mounten:

   ```yaml
   volumes:
     - ./translations.fr.json:/app/translations.fr.json:ro
   ```

5. `LANGUAGE: "fr"` setzen und Container neu starten:

   ```bash
   docker compose up -d
   ```

6. Die Discovery-Topics werden bei jedem Bridge-Start neu publiziert,
   HA übernimmt die neuen Namen sofort.

PRs für weitere Sprachen sind willkommen.

---

## Troubleshooting

### "DP connected" erscheint, aber Sensoren werden nicht aktualisiert

Die DP ist verbunden, schickt aber gerade keine Frames. Im Log nach
`decode_packet error` schauen — wenn das auftaucht, ist es Protokoll-
Drift (selten; Issue mit Hex-Dump aufmachen).

### Gar keine "DP connected"-Zeile

Die DPs erreichen die Bridge nicht. Prüfen:

1. **DNS-Redirect funktioniert wirklich**:
   `nslookup mqtt-e.ecoflow.com` aus dem DP-Netz heraus — Antwort muss
   die Bridge-IP sein.
2. **DPs erreichen die Bridge-IP überhaupt**:
   von einem Host im DP-Netz `nc -zv <bridge-ip> 6500`.
3. **DPs sprechen heimlich doch mit der Cloud**:
   wenn ausgehendes Internet zu `*.ecoflow.com` nicht geblockt ist und
   die DPs den Hostnamen vor deinem Redirect bereits aufgelöst hatten,
   haben sie evtl. die Adresse gecached. DPs nach DNS-Fix stromlos
   schalten.
4. **Firewall am Bridge-Host**: Port 6500 (oder dein gewählter) muss
   erreichbar sein.

### "connection from unknown IP" im Log

Eine DP erreicht die Bridge, ihre IP steht aber nicht in `DEVICE_MAP`.
Entweder ergänzen, oder schauen warum ein unerwarteter Host den Port
trifft.

### Home Assistant zeigt Entities, aber alle "Nicht verfügbar"

Entweder ist die Bridge vom MQTT getrennt, oder die DP ist tatsächlich
offline. Prüfen:

- `mosquitto_sub -h <broker> -t 'ecoflow/+/availability' -v`
  → sollte `online` für jede verbundene DP zeigen.
- Container-Log auf MQTT-Reconnects.

### Battery State of Health sieht komisch aus

Wird berechnet aus `battery_capacity_full / 80.000 mAh × 100`. Die
80.000 mAh Auslegungskapazität sind für die DELTA Pro hartkodiert
(`DESIGN_CAPACITY_MAH` in `dp_bridge.py`).

---

## Repo-Struktur

```
.
├── README.md                     ← hier bist du
├── README.en.md                  ← Englisch
├── LICENSE.md                    ← Nutzungsbedingungen (Hobby)
├── Dockerfile
├── docker-compose.example.de.yml ← kopieren → docker-compose.yml, editieren - in Deutsch
├── docker-compose.example.en.yml ← kopieren → docker-compose.yml, editieren - in Englisch
├── dp_bridge.py                  ← Haupt-Daemon
├── ecoflow_codec.py              ← CRC, Frame-Parsing-Basics
├── ecoflow_receive.py            ← Decoder (BMS / EMS / Inverter / MPPT / PD)
├── ecoflow_send.py               ← Command-Builder (schreibbare Steuerungen)
├── translations.de.json          ← Deutsche Friendly Names
├── translations.en.json          ← Englische Friendly Names (auch Code-Default)
└── .gitignore
```

---

## Credits

`ecoflow_codec.py`, `ecoflow_receive.py` und `ecoflow_send.py` basieren
auf der Reverse-Engineering-Arbeit der Community rund um das
EcoFlow-`aa02`-Protokoll. Der Bridge-Daemon, der
Home-Assistant-Discovery-Layer, das Multi-Device-Session-Handling, der
Publish-on-Change-Cache und der Multi-Language-Support sind
Eigenentwicklung dieses Projekts.

Falls du etwas siehst, das woanders herkommt und nicht korrekt
attribuiert ist — bitte Issue aufmachen.

---

## Lizenz

Hobby-Projekt — keine formelle Lizenz. Details siehe
[LICENSE.md](LICENSE.md). Kurz: privater Gebrauch gerne, kommerzielle
Nutzung nicht, keine Garantien.
