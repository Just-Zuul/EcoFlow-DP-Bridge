#!/usr/bin/env python3
"""
EcoFlow Delta Pro Bridge v4.9.0
===============================
Lauscht auf TCP:6500, dekodiert aa02-Frames der DP, publiziert Werte +
HA-Discovery für Sensoren UND steuerbare Switches/Numbers/Selects.

Multi-Device-Support: ein Container, mehrere DPs.
Identifikation per Source-IP via DEVICE_MAP (siehe ENV).

Steuerbare Schalter:  AC Out, X-Boost, DC Out (12V), Beep
Steuerbare Werte:     AC Charging Limit (W), Battery Max/Min Limit (%)
Steuerbare Selects:   Screen Timeout, Unit Timeout

Features:
- Multi-Language: ENV LANGUAGE wählt translations.<LANGUAGE>.json
  (z.B. de, en, fr, ...). Eine neue Sprache hinzufügen: einfach
  translations.<code>.json anlegen.
- Publish-on-Change mit Toleranz pro Einheit (±0.01 V, ±1 W, ±10 mA,
  ±0.5 °C, ±0.1 %) — der MQTT-Broker erstickt nicht im Mess-Rauschen.
- last_seen-Throttle: max. alle 30s, nicht bei jedem Frame.
- Initial-Publish bei TCP-(Re)Connect: Cache wird geleert, alle Werte
  gehen einmal frisch raus.
- Beep-Sensor wird vor dem Publish invertiert (Hardware sendet
  pd.beep=0 für "Beeper aktiv", 1 für "stumm" — invers zur Intuition).
  Sensor zeigt nun: 1=an, 0=aus, konsistent mit dem HA-Switch.
"""
import os
import socket
import threading
import time
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Dict, List, Tuple

import paho.mqtt.client as mqtt

from ecoflow_codec import calcCrc8, calcCrc16
from ecoflow_receive import (
    decode_packet,
    is_bms, is_ems, is_inverter, is_mppt, is_pd,
    parse_bms_delta, parse_ems_delta, parse_inverter_delta,
    parse_mppt_delta, parse_pd_delta,
)
from ecoflow_send import (
    build2,
    get_inverter, get_pd, get_dc_in_current,
    set_ac_out, set_dc_out, set_beep, set_lcd, set_standby_timeout,
    set_ac_in_limit, set_level_max, set_level_min,
)

# ---------- Konfiguration via ENV ----------
LISTEN_PORT    = int(os.environ.get('DP_LISTEN_PORT', '6500'))
MQTT_HOST      = os.environ.get('MQTT_HOST', '192.168.1.100')
MQTT_PORT      = int(os.environ.get('MQTT_PORT', '1883'))
MQTT_USER      = os.environ.get('MQTT_USER', '')
MQTT_PASS      = os.environ.get('MQTT_PASS', '')
LOG_LEVEL      = os.environ.get('LOG_LEVEL', 'INFO').upper()
POLL_INTERVAL  = int(os.environ.get('POLL_INTERVAL', '5'))
IDLE_TIMEOUT   = int(os.environ.get('IDLE_TIMEOUT', '30'))   # Sek. ohne Frame → availability=offline
BRIDGE_VERSION = os.environ.get('BRIDGE_VERSION', 'v4.9.0')
PRODUCT_ID     = 14  # Delta Pro
DESIGN_CAPACITY_MAH = 80000  # DP1: 80.000 mAh laut Hersteller-Datenblatt — Basis für SOH-Berechnung

# DEVICE_MAP-Format:  "<IP1>=<name1>|<IP2>=<name2>|..."
# Beispiel:  "192.168.10.150=dp1|192.168.10.160=dp2|192.168.10.170=dp3"
DEVICE_MAP_RAW = os.environ.get('DEVICE_MAP', '')

# ----- Sprach-Auswahl (Multi-Language) -----
#   LANGUAGE=de  → lädt translations.de.json
#   LANGUAGE=en  → lädt translations.en.json
#   Eigene Sprache: einfach translations.<code>.json anlegen + LANGUAGE setzen
# Optionaler Override:
#   TRANSLATIONS_PATH=/pfad/zu/datei.json  → lädt direkt diese Datei
#   (überschreibt LANGUAGE — nützlich z.B. für Tests mit Custom-Mappings)
# Wenn weder die sprachspezifische Datei noch ein Override existiert, wird
# als zweiter Fallback eine schlichte translations.json gesucht. Bleibt
# auch das aus, gelten die englischen Code-Defaults.
LANGUAGE          = os.environ.get('LANGUAGE', 'de').lower()
TRANSLATIONS_DIR  = os.environ.get('TRANSLATIONS_DIR', '/app')
TRANSLATIONS_PATH = os.environ.get('TRANSLATIONS_PATH', '')   # optionaler override

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('dp-bridge')


# ---------- Publish-on-Change: Cache + Toleranz ----------
# Pro Device ein Dict {topic: last_published_payload_str}.
# Wird beim TCP-Connect geleert → erstes Frame published alles einmal frisch.
PUBLISH_CACHE: Dict[str, Dict[str, str]] = {}
_pub_lock = threading.Lock()

# Toleranz pro Einheit. Werte unter dieser Schwelle gelten als "gleich" und werden
# nicht erneut publiziert. Spart Broker-Traffic bei Mess-Jitter, ohne echte
# Änderungen zu verschlucken.
#   ±0.01 V   — viel feiner als DP-Mess-Auflösung
#   ±1   W   — bei 3600-W-Inverter <0.03 % Schwankung
#   ±10  mA  — bei BMS-Strömen im 20-30A Bereich vernachlässigbar
#   ±0.1 A   — für AC-Out-Strom (kommt in A, nicht mA)
#   ±0.5 °C  — Temperatur-Rauschen filtern
#   ±0.1 %   — SOC-Float-Rauschen filtern (BMS SOC Float, EMS SOC Float)
TOLERANCE_BY_UNIT = {
    'V':   0.01,
    'W':   1.0,
    'mA':  10.0,
    'A':   0.1,
    '°C':  0.5,
    '%':   0.1,
}

# last_seen-Throttle: Topic published höchstens alle N Sekunden, nicht bei jedem Frame.
LAST_SEEN_THROTTLE_SEC = 30
_last_seen_published: Dict[str, float] = {}   # dev_name → unix-ts der letzten Publishs


def _values_close(old_str: str, new_str: str, tolerance: Optional[float]) -> bool:
    """
    True wenn old/new innerhalb der Toleranz gleich sind.
    Bei tolerance=None oder Strings: strikter Stringvergleich.
    """
    if tolerance is None or tolerance <= 0:
        return old_str == new_str
    try:
        return abs(float(old_str) - float(new_str)) <= tolerance
    except (ValueError, TypeError):
        return old_str == new_str


def publish_changed(mq: mqtt.Client, dev_name: str, topic: str, payload: str,
                    tolerance: Optional[float] = None,
                    qos: int = 0, retain: bool = True) -> bool:
    """
    Publiziert nur wenn sich der Wert seit letztem Publish geändert hat
    (innerhalb der angegebenen Toleranz).
    Gibt True zurück wenn published wurde.
    """
    with _pub_lock:
        cache = PUBLISH_CACHE.setdefault(dev_name, {})
        last = cache.get(topic)
        if last is not None and _values_close(last, payload, tolerance):
            return False
        cache[topic] = payload
    mq.publish(topic, payload, qos=qos, retain=retain)
    return True


def reset_publish_cache(dev_name: str):
    """Beim (Re-)Connect: Cache leeren, damit alle Werte einmal neu rausgehen."""
    with _pub_lock:
        PUBLISH_CACHE[dev_name] = {}
    _last_seen_published.pop(dev_name, None)



def parse_device_map(raw: str) -> Dict[str, dict]:
    """Parst DEVICE_MAP-String → Dict {name: {label, id, ip}}."""
    result: Dict[str, dict] = {}
    if not raw.strip():
        log.error('DEVICE_MAP is empty — no devices to handle')
        return result
    for entry in raw.split('|'):
        entry = entry.strip()
        if not entry or '=' not in entry:
            continue
        ip, name = [p.strip() for p in entry.split('=', 1)]
        if not ip or not name:
            log.warning(f'DEVICE_MAP entry skipped (empty parts): {entry!r}')
            continue
        # Schöne Defaults aus Name ableiten:  dp1 → "EcoFlow Delta Pro 1", "ecoflow_dp1"
        digits = ''.join(c for c in name if c.isdigit()) or '?'
        label = f'EcoFlow Delta Pro {digits}'
        dev_id = f'ecoflow_{name}'
        result[name] = {'name': name, 'label': label, 'id': dev_id, 'ip': ip}
    return result


DEVICES: Dict[str, dict] = parse_device_map(DEVICE_MAP_RAW)
IP_TO_NAME: Dict[str, str] = {dev['ip']: dev['name'] for dev in DEVICES.values()}


def _resolve_translation_path() -> Optional[str]:
    """
    Findet die zu ladende Übersetzungsdatei in dieser Reihenfolge:
      1) TRANSLATIONS_PATH (Override, falls gesetzt + existiert)
      2) <TRANSLATIONS_DIR>/translations.<LANGUAGE>.json
      3) <TRANSLATIONS_DIR>/translations.json   (allgemeiner Fallback)
    Gibt None zurück, wenn nichts gefunden — dann gelten die Code-Defaults (EN).
    """
    if TRANSLATIONS_PATH:
        return TRANSLATIONS_PATH if os.path.exists(TRANSLATIONS_PATH) else None
    lang_path = os.path.join(TRANSLATIONS_DIR, f'translations.{LANGUAGE}.json')
    if os.path.exists(lang_path):
        return lang_path
    legacy = os.path.join(TRANSLATIONS_DIR, 'translations.json')
    if os.path.exists(legacy):
        return legacy
    return None


def load_translations() -> Dict[str, Dict[str, str]]:
    """Lädt die zu LANGUAGE passende translations-Datei. Fehlt sie / ist kaputt → Defaults."""
    path = _resolve_translation_path()
    if path is None:
        log.info(f'no translations file found (LANGUAGE={LANGUAGE}, dir={TRANSLATIONS_DIR}) '
                 f'— using built-in defaults (English)')
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        n = sum(len(v) for v in data.values() if isinstance(v, dict))
        log.info(f'loaded {n} translations from {path} (LANGUAGE={LANGUAGE})')
        return data
    except Exception as e:
        log.warning(f'failed to load translations from {path}: {e} — using defaults')
        return {}


TRANSLATIONS = load_translations()


def tr(component: str, key: str, default: str) -> str:
    """Lookup im Übersetzungsdict — unbekannte Keys liefern den Code-Default zurück."""
    return TRANSLATIONS.get(component, {}).get(key, default)


# ---------- Topic-Helper ----------
def topic_base(name: str) -> str:    return f'ecoflow/{name}'
def topic_avail(name: str) -> str:   return f'ecoflow/{name}/availability'
def topic_cmd(name: str) -> str:     return f'ecoflow/{name}/cmd'
def topic_last_seen(name: str) -> str: return f'ecoflow/{name}/last_seen'


# ---------- Sensor-Discovery ----------
DISCOVERY_MAP = [
    {'matcher': is_ems, 'parser': parse_ems_delta, 'topic': 'ems', 'sensors': [
        ('battery_main_level',     'Battery SOC',           '%',  'battery',     'measurement', None, None),
        ('battery_main_level_f32', 'Battery SOC (Float)',   '%',  None,          'measurement', 'mdi:battery-heart-variant', None),
        ('battery_main_voltage',   'Battery Voltage',       'V',  'voltage',     'measurement', None, None),
        ('battery_main_current',   'Battery Current (EMS)', 'A',  'current',     'measurement', None, None),
        # Limits ohne device_class=battery, sonst kapert HA die Header-Anzeige des Geräts
        ('battery_level_max',      'Battery Max Limit',     '%',  None,          'measurement', 'mdi:battery-arrow-up',   None),
        ('battery_level_min',      'Battery Min Limit',     '%',  None,          'measurement', 'mdi:battery-arrow-down', None),
        # Restzeiten — DP berechnet selbst, aufgepasst: ignoriert die Limits!
        ('battery_remain_charge',    'Time to Full',  's', 'duration', 'measurement', 'mdi:battery-clock',         'timedelta_sec'),
        ('battery_remain_discharge', 'Time to Empty', 's', 'duration', 'measurement', 'mdi:battery-clock-outline', 'timedelta_sec'),
    ]},
    {'matcher': is_bms, 'parser': lambda d: parse_bms_delta(d)[1], 'topic': 'bms', 'sensors': [
        ('battery_level',           'BMS SOC',              '%',   None,          'measurement',       'mdi:battery-high', None),
        ('battery_level_f32',       'BMS SOC (Float)',      '%',   None,          'measurement',       'mdi:battery-heart', None),
        ('battery_voltage',         'BMS Voltage',          'V',   'voltage',     'measurement',       None, None),
        ('battery_current',         'BMS Current',          'mA',  'current',     'measurement',       None, 'signed32'),
        ('battery_temp',            'BMS Temperature',      '°C',  'temperature', 'measurement',       None, None),
        ('battery_capacity_design', 'BMS Design Capacity',  'mAh', None,          'measurement',       'mdi:battery-outline', None),
        ('battery_capacity_full',   'BMS Capacity Full',    'mAh', None,          'measurement',       'mdi:battery',         None),
        ('battery_capacity_remain', 'BMS Capacity Remain',  'mAh', None,          'measurement',       'mdi:battery-50',      None),
        ('battery_state_of_health', 'BMS State of Health',  '%',   None,          'measurement',       'mdi:heart-pulse',     None),
        ('battery_cycles',          'BMS Cycles',           None,  None,          'total_increasing',  'mdi:counter',    None),
        ('battery_voltage_max',     'BMS Cell Voltage Max', 'V',   'voltage',     'measurement',       None, None),
        ('battery_voltage_min',     'BMS Cell Voltage Min', 'V',   'voltage',     'measurement',       None, None),
        ('battery_temp_max',        'BMS Cell Temp Max',    '°C',  'temperature', 'measurement',       None, None),
        ('battery_temp_min',        'BMS Cell Temp Min',    '°C',  'temperature', 'measurement',       None, None),
        ('battery_mos_temp_max',    'BMS MOSFET Temp Max',  '°C',  'temperature', 'measurement',       None, None),
        ('battery_mos_temp_min',    'BMS MOSFET Temp Min',  '°C',  'temperature', 'measurement',       None, None),
        ('battery_in_power',        'BMS In Power',         'W',   'power',       'measurement',       None, 'signed32'),
        ('battery_out_power',       'BMS Out Power',        'W',   'power',       'measurement',       None, 'signed32'),
    ]},
    {'matcher': is_inverter, 'parser': parse_inverter_delta, 'topic': 'inverter', 'sensors': [
        ('ac_in_power',     'AC In Power',     'W',  'power',       'measurement', None, None),
        ('ac_out_power',    'AC Out Power',    'W',  'power',       'measurement', None, None),
        ('ac_out_voltage',  'AC Out Voltage',  'V',  'voltage',     'measurement', None, None),
        ('ac_out_current',  'AC Out Current',  'A',  'current',     'measurement', None, None),
        ('ac_in_voltage',   'AC In Voltage',   'V',  'voltage',     'measurement', None, None),
        ('ac_in_current',   'AC In Current',   'A',  'current',     'measurement', None, None),
        ('ac_out_state',    'AC Out State',    None, None,          None,          'mdi:power-socket-eu', None),
        ('ac_out_xboost',   'AC X-Boost',      None, None,          None,          'mdi:lightning-bolt',  None),
        ('ac_out_temp',     'AC Out Temp',     '°C', 'temperature', 'measurement', None, None),
        ('ac_in_limit_switch', 'AC In Limit Switch', None, None, None, 'mdi:toggle-switch', None),
        ('ac_in_limit_max',    'AC In Limit Max',    'W',  'power', 'measurement', None, None),
        ('ac_in_limit_custom', 'AC In Limit Custom', 'W',  'power', 'measurement', None, None),
    ]},
    {'matcher': is_mppt, 'parser': parse_mppt_delta, 'topic': 'mppt', 'sensors': [
        ('dc_in_voltage',  'PV Voltage',     'V',  'voltage',     'measurement', None, None),
        ('dc_in_current',  'PV Current',     'A',  'current',     'measurement', None, None),
        ('dc_in_power',    'PV Power',       'W',  'power',       'measurement', None, None),
        ('dc_in_type',     'PV Input Type',  None, None,          None,          'mdi:solar-panel', None),
        ('car_out_power',  'Car Out Power',  'W',  'power',       'measurement', None, None),
        ('car_out_state',  'Car Out State',  None, None,          None,          'mdi:car-electric', None),
    ]},
    {'matcher': is_pd, 'parser': parse_pd_delta, 'topic': 'pd', 'sensors': [
        ('battery_level', 'PD Battery Level', '%',  None, 'measurement', 'mdi:battery-charging', None),
        ('in_power',      'PD In Power',      'W',  'power',   'measurement', None, None),
        ('out_power',     'PD Out Power',     'W',  'power',   'measurement', None, None),
        ('beep',          'PD Beep State',    None, None,      None,          'mdi:bell', 'invert_bool'),
        # Energie-Zähler (lebenslang, in Wh) — Energy-Dashboard-kompatibel
        ('mppt_in_energy',  'PV Energy In Total',  'Wh', 'energy', 'total_increasing', 'mdi:solar-power',           None),
        ('ac_in_energy',    'AC Energy In Total',  'Wh', 'energy', 'total_increasing', 'mdi:transmission-tower-import', None),
        ('car_in_energy',   'Car Energy In Total', 'Wh', 'energy', 'total_increasing', 'mdi:car-battery',           None),
        ('ac_out_energy',   'AC Energy Out Total', 'Wh', 'energy', 'total_increasing', 'mdi:transmission-tower-export', None),
        ('car_out_energy',  'Car Energy Out Total','Wh', 'energy', 'total_increasing', 'mdi:car-electric',          None),
        # Laufzeiten — kommen als timedelta(sec)
        ('usb_time',     'USB Runtime',     's', 'duration', 'total_increasing', 'mdi:usb-port',         'timedelta_sec'),
        ('typec_time',   'USB-C Runtime',   's', 'duration', 'total_increasing', 'mdi:usb-c-port',       'timedelta_sec'),
        ('car_out_time', 'Car Out Runtime', 's', 'duration', 'total_increasing', 'mdi:car-electric',     'timedelta_sec'),
        ('ac_out_time',  'AC Out Runtime',  's', 'duration', 'total_increasing', 'mdi:power-socket-eu',  'timedelta_sec'),
        ('ac_in_time',   'AC In Runtime',   's', 'duration', 'total_increasing', 'mdi:transmission-tower-import', 'timedelta_sec'),
        ('car_in_time',  'Car In Runtime',  's', 'duration', 'total_increasing', 'mdi:car-battery',      'timedelta_sec'),
    ]},
]

# ---------- Switch-Definitionen ----------
SWITCH_MAP = [
    {'key':'ac_out',  'name':'AC Out',       'icon':'mdi:power-socket-eu',
     'state_field':('inverter','ac_out_state'),  'on_value':'1', 'off_value':'0',
     'build': lambda on: set_ac_out(PRODUCT_ID, enable=on),
     'poll_after':'inverter'},
    {'key':'x_boost', 'name':'X-Boost',      'icon':'mdi:lightning-bolt',
     'state_field':('inverter','ac_out_xboost'), 'on_value':'1', 'off_value':'0',
     'build': lambda on: set_ac_out(PRODUCT_ID, xboost=on),
     'poll_after':'inverter'},
    {'key':'dc_out',  'name':'DC Out (12V)', 'icon':'mdi:car-electric',
     'state_field':('mppt','car_out_state'),     'on_value':'1', 'off_value':'0',
     'build': lambda on: set_dc_out(PRODUCT_ID, enable=on),
     'poll_after':'mppt'},
    # Beep: DP-Hardware sendet pd.beep=0 für "Beeper aktiv" und pd.beep=1 für "stumm"
    # — invers zur Intuition. v4.8.0 invertiert den Sensor-Rohwert vor dem Publish
    # (siehe 'invert_bool' im DISCOVERY_MAP für pd→beep), sodass der Topic-Wert
    # intuitiv "1=an, 0=aus" zeigt. Der Switch liest diesen invertierten Topic-Wert,
    # daher on_value='1', off_value='0'. Der build-Lambda ist davon unabhängig:
    # set_beep(enable=True) sendet payload=0, was die DP als "Beeper an" interpretiert.
    {'key':'beep',    'name':'Beep',         'icon':'mdi:bell',
     'state_field':('pd','beep'),                'on_value':'1', 'off_value':'0',
     'build': lambda on: set_beep(enable=on),
     'poll_after':'pd'},
]

# ---------- Number-Definitionen (Slider mit Live-Schreiben) ----------
NUMBER_MAP = [
    {'key':'ac_charging_limit', 'name':'AC Charging Limit', 'icon':'mdi:transmission-tower',
     'unit':'W', 'min':200, 'max':2900, 'step':100, 'mode':'slider',
     'state_field':('inverter','ac_in_limit_custom'),
     'build': lambda val: set_ac_in_limit(watts=int(val)),
     'poll_after':'inverter'},
    {'key':'battery_level_max', 'name':'Battery Max Charge', 'icon':'mdi:battery-arrow-up',
     'unit':'%', 'min':50, 'max':100, 'step':1, 'mode':'slider',
     'state_field':('ems','battery_level_max'),
     'build': lambda val: set_level_max(PRODUCT_ID, int(val)),
     'poll_after':'inverter'},
    {'key':'battery_level_min', 'name':'Battery Min Discharge', 'icon':'mdi:battery-arrow-down',
     'unit':'%', 'min':0, 'max':30, 'step':1, 'mode':'slider',
     'state_field':('ems','battery_level_min'),
     'build': lambda val: set_level_min(int(val)),
     'poll_after':'inverter'},
]

# ---------- Select-Definitionen ----------
SELECT_MAP = [
    {'key':'unit_timeout',
     'name':'Unit Timeout',
     'icon':'mdi:timer-off',
     'options': [
         ('Nie',   0),
         ('30 min', 1800),
         ('1 h',   3600),
         ('2 h',   7200),
         ('4 h',  14400),
         ('6 h',  21600),
         ('12 h', 43200),
         ('24 h', 86400),
     ],
     'state_field':('pd','standby_timeout'),
     'build': lambda sec: set_standby_timeout(int(sec)),
     'poll_after':'pd'},
    {'key':'screen_timeout',
     'name':'Screen Timeout',
     'icon':'mdi:monitor-clean',
     'options': [
         ('10 s',  10),
         ('30 s',  30),
         ('1 min', 60),
         ('5 min', 300),
         ('30 min',1800),
         ('Nie',  0xFFFF),
     ],
     'state_field':('pd','lcd_timeout'),
     'build': lambda sec: set_lcd(PRODUCT_ID, time=int(sec)),
     'poll_after':'pd'},
]


# ---------- MQTT-Setup ----------
def make_mqtt_client():
    client = mqtt.Client(
        client_id='dp-bridge-multi',
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    # Hinweis: kein klassisches LWT, da das pro Device wäre und ein Client nur eines
    # hinterlegen kann. Bei Bridge-Crash bleiben Sensoren auf last-retained-Werten.
    # Beim regulären TCP-Disconnect setzen wir availability korrekt auf "offline".
    return client


def publish_discovery(mq: mqtt.Client):
    """Publiziert Discovery-Configs für ALLE konfigurierten Devices."""
    if not DEVICES:
        log.warning('publish_discovery: no devices configured')
        return

    for dev in DEVICES.values():
        name, label, dev_id = dev['name'], dev['label'], dev['id']
        device_block = {
            'identifiers':[dev_id], 'name':label,
            'manufacturer':'EcoFlow', 'model':'Delta Pro',
            'sw_version':f'aa02-bridge-{BRIDGE_VERSION}',
        }
        avail_topic = topic_avail(name)
        cmd_base    = topic_cmd(name)

        # Sensoren — Discovery-Topic-Hierarchie:  homeassistant/<comp>/<dev_id>/<obj>/config
        for grp in DISCOVERY_MAP:
            for field, friendly_default, unit, dev_class, state_class, icon, _conv in grp['sensors']:
                obj = f'{grp["topic"]}_{field}'
                friendly = tr('sensor', obj, friendly_default)
                cfg_topic   = f'homeassistant/sensor/{dev_id}/{obj}/config'
                state_topic = f'{topic_base(name)}/{grp["topic"]}/{field}'
                payload = {
                    'name':friendly, 'state_topic':state_topic,
                    'unique_id':f'{dev_id}_{obj}', 'object_id':f'{dev_id}_{obj}',
                    'availability_topic':avail_topic, 'device':device_block,
                }
                if unit:        payload['unit_of_measurement'] = unit
                if dev_class:   payload['device_class'] = dev_class
                if state_class: payload['state_class'] = state_class
                if icon:        payload['icon'] = icon
                mq.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)

        # Switches
        for sw in SWITCH_MAP:
            obj = sw['key']
            sw_name = tr('switch', obj, sw['name'])
            cfg_topic   = f'homeassistant/switch/{dev_id}/{obj}/config'
            sub, field  = sw['state_field']
            state_topic = f'{topic_base(name)}/{sub}/{field}'
            cmd_topic_  = f'{cmd_base}/{sw["key"]}/set'
            payload = {
                'name':sw_name, 'unique_id':f'{dev_id}_{obj}', 'object_id':f'{dev_id}_{obj}',
                'state_topic':state_topic, 'command_topic':cmd_topic_,
                'payload_on':'ON', 'payload_off':'OFF',
                'state_on':sw['on_value'], 'state_off':sw['off_value'],
                'availability_topic':avail_topic, 'icon':sw['icon'],
                'device':device_block,
            }
            mq.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)

        # Numbers
        for num in NUMBER_MAP:
            obj = num['key']
            num_name = tr('number', obj, num['name'])
            cfg_topic   = f'homeassistant/number/{dev_id}/{obj}/config'
            sub, field  = num['state_field']
            state_topic = f'{topic_base(name)}/{sub}/{field}'
            cmd_topic_  = f'{cmd_base}/{num["key"]}/set'
            payload = {
                'name':num_name, 'unique_id':f'{dev_id}_{obj}', 'object_id':f'{dev_id}_{obj}',
                'state_topic':state_topic, 'command_topic':cmd_topic_,
                'min':num['min'], 'max':num['max'], 'step':num['step'],
                'unit_of_measurement':num['unit'],
                'mode':num.get('mode','box'),
                'availability_topic':avail_topic, 'icon':num['icon'],
                'device':device_block,
            }
            mq.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)

        # Selects
        for sel in SELECT_MAP:
            obj = sel['key']
            sel_name = tr('select', obj, sel['name'])
            cfg_topic   = f'homeassistant/select/{dev_id}/{obj}/config'
            state_topic = f'{topic_base(name)}/{sel["key"]}/state_label'
            cmd_topic_  = f'{cmd_base}/{sel["key"]}/set'
            payload = {
                'name':sel_name, 'unique_id':f'{dev_id}_{obj}', 'object_id':f'{dev_id}_{obj}',
                'state_topic':state_topic, 'command_topic':cmd_topic_,
                'options':[label for label, _ in sel['options']],
                'availability_topic':avail_topic, 'icon':sel['icon'],
                'device':device_block,
            }
            mq.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)

        # Connectivity Binary Sensor — nutzt direkt den availability-Topic.
        cfg_topic = f'homeassistant/binary_sensor/{dev_id}/connected/config'
        payload = {
            'name': tr('binary_sensor', 'connected', 'Connected'),
            'unique_id': f'{dev_id}_connected', 'object_id': f'{dev_id}_connected',
            'state_topic': avail_topic,
            'payload_on': 'online', 'payload_off': 'offline',
            'device_class': 'connectivity',
            'device': device_block,
        }
        mq.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)

        # Last Seen Timestamp — wird im handle_frame() bei jedem Frame aktualisiert.
        cfg_topic = f'homeassistant/sensor/{dev_id}/last_seen/config'
        payload = {
            'name': tr('sensor', 'last_seen', 'Last Seen'),
            'unique_id': f'{dev_id}_last_seen', 'object_id': f'{dev_id}_last_seen',
            'state_topic': topic_last_seen(name),
            'device_class': 'timestamp',
            'icon': 'mdi:clock-check-outline',
            'device': device_block,
        }
        mq.publish(cfg_topic, json.dumps(payload), qos=1, retain=True)

        # Initial: noch keine TCP-Verbindung → offline
        mq.publish(avail_topic, 'offline', qos=1, retain=True)

    n_dev = len(DEVICES)
    log.info(f'HA-Discovery: {n_dev} devices × '
             f'{sum(len(g["sensors"]) for g in DISCOVERY_MAP)} sensors, '
             f'{len(SWITCH_MAP)} switches, {len(NUMBER_MAP)} numbers, {len(SELECT_MAP)} selects')


def value_to_str(v: Any) -> str:
    if isinstance(v, timedelta):
        return str(int(v.total_seconds()))
    if isinstance(v, float):
        return f'{v:.3f}'
    return str(v)


def maybe_signed32(v: Any) -> Any:
    if isinstance(v, int) and v >= (1 << 31):
        return v - (1 << 32)
    return v


# ---------- Session pro Device ----------
class Session:
    """Hält den aktuellen TCP-Socket einer DP + Sende-Synchronisation."""
    def __init__(self, name: str):
        self.name = name
        self.sock: Optional[socket.socket] = None
        self.alive = False
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.poll_now_event = threading.Event()
        self.poll_now_targets: List[Tuple[str, bytes]] = []

    def attach(self, sock):
        with self._lock:
            self.sock = sock
            self.alive = True

    def detach(self):
        with self._lock:
            self.sock = None
            self.alive = False
        self.poll_now_event.set()

    def send(self, frame: bytes, label: str = '') -> bool:
        with self._lock:
            if not self.alive or not self.sock:
                log.warning(f'[{self.name}] cannot send {label}: no active DP connection')
                return False
            sock = self.sock
        with self._send_lock:
            try:
                sock.sendall(frame)
                return True
            except Exception as e:
                log.warning(f'[{self.name}] send failed ({label}): {e}')
                with self._lock:
                    self.alive = False
                return False

    def trigger_poll(self, frames):
        with self._lock:
            self.poll_now_targets.extend(frames)
        self.poll_now_event.set()


SESSIONS: Dict[str, Session] = {name: Session(name) for name in DEVICES}


# ---------- Empfang ----------
def handle_frame(mq: mqtt.Client, dev_name: str, frame: bytes):
    try:
        a, b, c, args = decode_packet(frame)
        key = (a, b, c)
    except Exception as e:
        log.debug(f'[{dev_name}] decode_packet error: {e}')
        return

    base = topic_base(dev_name)

    # Last Seen — gedrosselt auf max. alle LAST_SEEN_THROTTLE_SEC Sekunden,
    # statt bei jedem Frame (würde ~10-20 Publishes/s erzeugen).
    now_ts = time.time()
    last_ts = _last_seen_published.get(dev_name, 0.0)
    if now_ts - last_ts >= LAST_SEEN_THROTTLE_SEC:
        now_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')
        mq.publish(topic_last_seen(dev_name), now_iso, qos=0, retain=True)
        _last_seen_published[dev_name] = now_ts

    for grp in DISCOVERY_MAP:
        if not grp['matcher'](key):
            continue
        try:
            parsed = grp['parser'](args)
        except Exception as e:
            log.debug(f'[{dev_name}] parse error for {grp["topic"]}: {e}')
            return

        # BMS-Frames: Design Capacity (konstant) + SOH (berechnet) injizieren
        if grp['topic'] == 'bms':
            parsed['battery_capacity_design'] = DESIGN_CAPACITY_MAH
            full = parsed.get('battery_capacity_full')
            if isinstance(full, (int, float)) and full > 0:
                parsed['battery_state_of_health'] = round(full / DESIGN_CAPACITY_MAH * 100, 2)

        for field, _name, unit, _dc, _sc, _icon, conv in grp['sensors']:
            if field not in parsed:
                continue
            v = parsed[field]
            if conv == 'signed32':
                v = maybe_signed32(v)
            elif conv == 'invert_bool':
                # Invertiert 0↔1 — z.B. für pd.beep, wo die Hardware die Bedeutung
                # umgedreht meldet (0=an, 1=aus). Topic zeigt danach intuitiv 1=an, 0=aus.
                if isinstance(v, (int, bool)):
                    v = 1 - int(v)
            # Plausibilitätsfilter: battery_level_max meldet beim Aufwachen der DP kurz 0.
            # 0 liegt außerhalb der HW-Range 50–100; die HA-number verwirft den State dann
            # mit einer Warnung (number.py: "Invalid value ..."). Daher diesen Ausreißer
            # gar nicht erst publishen — das Topic behält seinen letzten gültigen Wert,
            # bis der echte EMS-Wert nachkommt. (battery_level_min braucht das nicht: dort
            # ist 0 ein gültiger Wert innerhalb der Range 0–30.)
            if grp['topic'] == 'ems' and field == 'battery_level_max' \
                    and not (isinstance(v, (int, float)) and 50 <= v <= 100):
                continue
            payload = value_to_str(v)
            tolerance = TOLERANCE_BY_UNIT.get(unit) if unit else None
            publish_changed(mq, dev_name,
                            f'{base}/{grp["topic"]}/{field}', payload,
                            tolerance=tolerance, qos=0, retain=True)

        # Selects: Sekundenwert → Label (Strings → strikter Vergleich, ohne Toleranz)
        for sel in SELECT_MAP:
            sub, field = sel['state_field']
            if sub != grp['topic'] or field not in parsed:
                continue
            v = parsed[field]
            if isinstance(v, timedelta):
                v = int(v.total_seconds())
            label = next((lbl for lbl, sec in sel['options'] if sec == v), None)
            if label is None:
                label = min(sel['options'], key=lambda o: abs(o[1] - (v or 0)))[0]
            publish_changed(mq, dev_name,
                            f'{base}/{sel["key"]}/state_label', label,
                            tolerance=None, qos=0, retain=True)
        return  # nur ein Match pro Frame


# ---------- Steuerung ----------
POLL_BUILDER = {
    'inverter': get_inverter,
    'mppt':     lambda: build2(5, 32, 2),
    'pd':       get_pd,
    'dc_in':    lambda: get_dc_in_current(PRODUCT_ID),
}


def handle_command(msg):
    """Topic-Format: ecoflow/<dev_name>/cmd/<key>/set"""
    topic = msg.topic
    payload = msg.payload.decode(errors='ignore').strip()
    log.info(f'CMD recv: {topic} = {payload!r}')

    parts = topic.split('/')
    # erwartet: ['ecoflow', '<dev>', 'cmd', '<key>', 'set']
    if len(parts) != 5 or parts[0] != 'ecoflow' or parts[2] != 'cmd' or parts[4] != 'set':
        log.warning(f'cmd topic shape unexpected: {topic}')
        return
    dev_name = parts[1]
    key      = parts[3]

    sess = SESSIONS.get(dev_name)
    if sess is None:
        log.warning(f'cmd for unknown device: {dev_name!r}')
        return

    for sw in SWITCH_MAP:
        if sw['key'] == key:
            on = (payload.upper() == 'ON')
            try:
                frame = sw['build'](on)
            except Exception as e:
                log.error(f'[{dev_name}] cmd build failed for {key}: {e}')
                return
            log.info(f'[{dev_name}] switch {key} → {"ON" if on else "OFF"} ({len(frame)}B)')
            if sess.send(frame, f'cmd-{key}'):
                pa = sw['poll_after']
                if pa in POLL_BUILDER:
                    sess.trigger_poll([(pa, POLL_BUILDER[pa]())])
            return

    for num in NUMBER_MAP:
        if num['key'] == key:
            try:
                val = float(payload)
            except ValueError:
                log.error(f'[{dev_name}] invalid number for {key}: {payload!r}')
                return
            try:
                frame = num['build'](val)
            except Exception as e:
                log.error(f'[{dev_name}] cmd build failed for {key}: {e}')
                return
            log.info(f'[{dev_name}] number {key} → {val} ({len(frame)}B)')
            if sess.send(frame, f'cmd-{key}'):
                pa = num['poll_after']
                if pa in POLL_BUILDER:
                    sess.trigger_poll([(pa, POLL_BUILDER[pa]())])
            return

    for sel in SELECT_MAP:
        if sel['key'] == key:
            sec = next((s for lbl, s in sel['options'] if lbl == payload), None)
            if sec is None:
                log.error(f'[{dev_name}] unknown select label for {key}: {payload!r}')
                return
            try:
                frame = sel['build'](sec)
            except Exception as e:
                log.error(f'[{dev_name}] cmd build failed for {key}: {e}')
                return
            log.info(f'[{dev_name}] select {key} → {payload} ({sec}s, {len(frame)}B)')
            if sess.send(frame, f'cmd-{key}'):
                pa = sel['poll_after']
                if pa in POLL_BUILDER:
                    sess.trigger_poll([(pa, POLL_BUILDER[pa]())])
            return

    log.warning(f'[{dev_name}] unknown cmd key: {key!r}')


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        log.info(f'MQTT connected to {MQTT_HOST}:{MQTT_PORT}')
        client.subscribe('ecoflow/+/cmd/+/set', qos=1)
        log.info('subscribed: ecoflow/+/cmd/+/set')
    else:
        log.error(f'MQTT connect failed: {reason_code}')


def on_mqtt_message(client, userdata, msg):
    try:
        handle_command(msg)
    except Exception as e:
        log.exception(f'cmd handler crashed: {e}')


# ---------- TCP-Connection-Handler ----------
def handle_dp_connection(sock: socket.socket, addr, mq: mqtt.Client):
    src_ip = addr[0]
    dev_name = IP_TO_NAME.get(src_ip)
    if dev_name is None:
        log.warning(f'connection from unknown IP {src_ip} — closing')
        try: sock.close()
        except: pass
        return

    sess = SESSIONS[dev_name]
    log.info(f'[{dev_name}] DP connected: {src_ip}:{addr[1]}')
    # Cache leeren → erstes Frame published nach Reconnect alle Werte einmal frisch.
    # (Wichtig nach Bridge-Restart oder DP-Reconnect, sonst wäre HA "blind" bis sich
    # zufällig was ändert.)
    reset_publish_cache(dev_name)
    sess.attach(sock)
    sess.send(b'\x00', 'dummy-init')

    avail_t = topic_avail(dev_name)
    mq.publish(avail_t, 'online', qos=1, retain=True)

    def poll_loop():
        time.sleep(2)
        while sess.alive:
            # Trigger-Frames (sofortiges Repoll nach Befehl)
            with sess._lock:
                triggered = sess.poll_now_targets[:]
                sess.poll_now_targets.clear()
                sess.poll_now_event.clear()
            for n, fr in triggered:
                if not sess.alive:
                    break
                if sess.send(fr, f'trigger-{n}'):
                    log.debug(f'[{dev_name}] trigger-poll {n}')
                time.sleep(0.2)

            # Reguläres Polling — Frames bei jedem Loop neu bauen
            poll_frames = [
                ('inverter', get_inverter()),
                ('mppt',     build2(5, 32, 2)),
                ('pd',       get_pd()),
                ('dc_in',    get_dc_in_current(PRODUCT_ID)),
            ]
            for n, fr in poll_frames:
                if not sess.alive:
                    break
                if sess.send(fr, f'poll-{n}'):
                    log.debug(f'[{dev_name}] polled {n}')
                time.sleep(0.3)

            wait = POLL_INTERVAL - 0.3 * len(poll_frames)
            if wait > 0:
                sess.poll_now_event.wait(timeout=wait)
        log.debug(f'[{dev_name}] poll-loop ended')

    threading.Thread(target=poll_loop, daemon=True).start()

    buf = b''
    frame_count = 0
    is_idle = False  # Tracking: aktuelle availability-Annahme der Bridge
    try:
        while True:
            sock.settimeout(float(IDLE_TIMEOUT))
            try:
                rcv = sock.recv(8192)
            except socket.timeout:
                # Kein Byte für IDLE_TIMEOUT Sekunden → DP wahrscheinlich aus.
                # Verbindung NICHT schließen — DP könnte einfach nur ruhig sein.
                if not is_idle:
                    log.info(f'[{dev_name}] idle for {IDLE_TIMEOUT}s — marking offline (TCP kept)')
                    mq.publish(avail_t, 'offline', qos=1, retain=True)
                    is_idle = True
                continue
            if not rcv:
                break
            buf += rcv

            # Daten kommen wieder rein → falls vorher idle, jetzt online schalten
            if is_idle:
                log.info(f'[{dev_name}] traffic resumed — marking online')
                mq.publish(avail_t, 'online', qos=1, retain=True)
                is_idle = False

            while len(buf) >= 18:
                if buf[:2] != b'\xaa\x02':
                    buf = buf[1:]; continue
                size = int.from_bytes(buf[2:4], 'little')
                if 18 + size > len(buf):
                    break
                if calcCrc8(buf[:4]) != buf[4:5]:
                    buf = buf[2:]; continue
                if calcCrc16(buf[:16+size]) != buf[16+size:18+size]:
                    buf = buf[2:]; continue
                fr = buf[:18+size]
                buf = buf[18+size:]
                handle_frame(mq, dev_name, fr)
                frame_count += 1
    except Exception as e:
        log.warning(f'[{dev_name}] connection error from {src_ip}: {e}')
    finally:
        sess.detach()
        try:
            mq.publish(avail_t, 'offline', qos=1, retain=True)
        except Exception:
            pass
        try: sock.close()
        except: pass
        log.info(f'[{dev_name}] DP disconnected: {src_ip}:{addr[1]} -- {frame_count} frames processed')


# ---------- Main ----------
def main():
    if not DEVICES:
        log.error('No devices configured. Set DEVICE_MAP, e.g. '
                  '"192.168.10.150=dp1|192.168.10.160=dp2|192.168.10.170=dp3"')
        return

    log.info(f'DP-Bridge {BRIDGE_VERSION} starting:')
    log.info(f'  listen=:{LISTEN_PORT}  mqtt={MQTT_HOST}:{MQTT_PORT}')
    log.info(f'  devices ({len(DEVICES)}):')
    for dev in DEVICES.values():
        log.info(f'    {dev["name"]}  ({dev["label"]})  ip={dev["ip"]}  id={dev["id"]}')

    mq = make_mqtt_client()
    mq.on_connect = on_mqtt_connect
    mq.on_message = on_mqtt_message

    for attempt in range(20):
        try:
            mq.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            break
        except Exception as e:
            log.warning(f'mqtt connect attempt {attempt+1} failed: {e}; retry in 3s')
            time.sleep(3)
    else:
        log.error('MQTT connect failed permanently — exiting (Container will restart)')
        return

    mq.loop_start()
    time.sleep(1)
    publish_discovery(mq)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', LISTEN_PORT))
    srv.listen(8)
    log.info(f'Listening on 0.0.0.0:{LISTEN_PORT}')

    try:
        while True:
            sock, addr = srv.accept()
            t = threading.Thread(target=handle_dp_connection, args=(sock, addr, mq), daemon=True)
            t.start()
    except KeyboardInterrupt:
        log.info('shutting down')
    finally:
        for n in DEVICES:
            try:
                mq.publish(topic_avail(n), 'offline', qos=1, retain=True)
            except Exception:
                pass
        time.sleep(0.5)
        mq.loop_stop()
        srv.close()


if __name__ == '__main__':
    main()
