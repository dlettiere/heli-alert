#!/usr/bin/env python3
"""
heli-alert  –  ADS-B → Gotify aircraft proximity notifier with Web UI

Polls a local tar1090 / readsb aircraft.json feed, classifies each
aircraft by type (helicopter, plane, military variants), calculates
distance from your base station, enriches aircraft metadata (full type,
manufacturer, operator) and flight route (origin, destination), and
pushes a Gotify notification when a matching aircraft enters your alert radius.
Includes a simple web interface to adjust settings and monitor live traffic.
"""

import http.server
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# ─── Logging ────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("heli-alert")
web_log = logging.getLogger("heli-alert.web")

# ─── Configuration ──────────────────────────────────────────────────

load_dotenv()

CONFIG_DIR = os.getenv("CONFIG_DIR", "/app/data")
CONFIG_FILE = os.getenv("CONFIG_FILE", os.path.join(CONFIG_DIR, "config.json"))
_config_lock = threading.Lock()


@dataclass
class Config:
    adsb_url: str = os.getenv("ADSB_URL", "http://localhost:8078/data/aircraft.json")
    base_lat: float = float(os.getenv("BASE_LAT", "0"))
    base_lon: float = float(os.getenv("BASE_LON", "0"))
    gotify_url: str = os.getenv("GOTIFY_URL", "http://localhost:8380")
    gotify_token: str = os.getenv("GOTIFY_APP_TOKEN", "")
    max_distance_mi: float = float(os.getenv("MAX_DISTANCE_MI", "5.0"))
    alert_types: set = field(default_factory=lambda: {"helicopter", "military_helo", "military_plane"})
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "10"))
    alert_cooldown: int = int(os.getenv("ALERT_COOLDOWN", "300"))
    tracker_url: str = os.getenv("TRACKER_URL", "https://www.flightradar24.com")
    web_host: str = os.getenv("WEB_HOST", "0.0.0.0")
    web_port: int = int(os.getenv("WEB_PORT", os.getenv("PORT", "8085")))

    def __post_init__(self):
        raw = os.getenv("ALERT_TYPES", "")
        if raw:
            self.alert_types = {t.strip() for t in raw.split(",") if t.strip()}

    @property
    def tar1090_base_url(self) -> str:
        """Derive base URL (e.g. http://host:port) from adsb_url for db lookups."""
        p = urlparse(self.adsb_url)
        return f"{p.scheme}://{p.netloc}"


def load_config() -> Config:
    """Load config from environment variables and overlay saved config.json if present."""
    c = Config()
    target_files = [CONFIG_FILE, "config.json", os.path.join(os.path.dirname(__file__), "config.json")]
    for path in target_files:
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if "adsb_url" in saved: c.adsb_url = str(saved["adsb_url"]).strip()
                if "base_lat" in saved: c.base_lat = float(saved["base_lat"])
                if "base_lon" in saved: c.base_lon = float(saved["base_lon"])
                if "gotify_url" in saved: c.gotify_url = str(saved["gotify_url"]).strip()
                if "gotify_token" in saved: c.gotify_token = str(saved["gotify_token"]).strip()
                if "max_distance_mi" in saved: c.max_distance_mi = float(saved["max_distance_mi"])
                if "alert_types" in saved:
                    types = saved["alert_types"]
                    if isinstance(types, str):
                        c.alert_types = {t.strip() for t in types.split(",") if t.strip()}
                    elif isinstance(types, (list, set)):
                        c.alert_types = {str(t).strip() for t in types if str(t).strip()}
                if "poll_interval" in saved: c.poll_interval = max(2, int(saved["poll_interval"]))
                if "alert_cooldown" in saved: c.alert_cooldown = max(10, int(saved["alert_cooldown"]))
                if "tracker_url" in saved: c.tracker_url = str(saved["tracker_url"]).strip()
                if "web_port" in saved: c.web_port = int(saved["web_port"])
                log.info("Loaded persisted settings from %s", path)
                break
            except Exception as exc:
                log.warning("Could not read config file %s: %s", path, exc)
    return c


cfg = load_config()


def save_config_data(new_settings: dict) -> tuple[bool, str]:
    """Save updated settings to config.json and update running in-memory config."""
    global cfg
    with _config_lock:
        try:
            if "adsb_url" in new_settings:
                val = str(new_settings["adsb_url"]).strip()
                if not val.startswith("http://") and not val.startswith("https://"):
                    return False, "ADS-B URL must start with http:// or https://"
                cfg.adsb_url = val
            if "base_lat" in new_settings:
                lat = float(new_settings["base_lat"])
                if not (-90 <= lat <= 90):
                    return False, "Latitude must be between -90 and 90"
                cfg.base_lat = lat
            if "base_lon" in new_settings:
                lon = float(new_settings["base_lon"])
                if not (-180 <= lon <= 180):
                    return False, "Longitude must be between -180 and 180"
                cfg.base_lon = lon
            if "gotify_url" in new_settings:
                val = str(new_settings["gotify_url"]).strip().rstrip("/")
                if val and not (val.startswith("http://") or val.startswith("https://")):
                    return False, "Gotify URL must start with http:// or https://"
                cfg.gotify_url = val
            if "gotify_token" in new_settings:
                cfg.gotify_token = str(new_settings["gotify_token"]).strip()
            if "max_distance_mi" in new_settings:
                cfg.max_distance_mi = max(0.1, float(new_settings["max_distance_mi"]))
            if "alert_types" in new_settings:
                types = new_settings["alert_types"]
                if isinstance(types, str):
                    cfg.alert_types = {t.strip() for t in types.split(",") if t.strip()}
                elif isinstance(types, (list, set)):
                    cfg.alert_types = {str(t).strip() for t in types if str(t).strip()}
            if "poll_interval" in new_settings:
                cfg.poll_interval = max(2, int(new_settings["poll_interval"]))
            if "alert_cooldown" in new_settings:
                cfg.alert_cooldown = max(10, int(new_settings["alert_cooldown"]))
            if "tracker_url" in new_settings:
                cfg.tracker_url = str(new_settings["tracker_url"]).strip()

            dump_data = {
                "adsb_url": cfg.adsb_url,
                "base_lat": cfg.base_lat,
                "base_lon": cfg.base_lon,
                "gotify_url": cfg.gotify_url,
                "gotify_token": cfg.gotify_token,
                "max_distance_mi": cfg.max_distance_mi,
                "alert_types": sorted(list(cfg.alert_types)),
                "poll_interval": cfg.poll_interval,
                "alert_cooldown": cfg.alert_cooldown,
                "tracker_url": cfg.tracker_url,
                "web_port": cfg.web_port,
            }

            # Try saving to CONFIG_FILE, fallback to local config.json
            save_path = CONFIG_FILE
            try:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    json.dump(dump_data, f, indent=2)
            except Exception:
                save_path = os.path.join(os.path.dirname(__file__), "config.json")
                with open(save_path, "w", encoding="utf-8") as f:
                    json.dump(dump_data, f, indent=2)

            log.info("Settings saved to %s and applied live", save_path)
            return True, f"Saved to {save_path}"
        except Exception as exc:
            log.exception("Error saving settings")
            return False, str(exc)


# ─── Live Telemetry & Statistics ────────────────────────────────────

_stats_lock = threading.Lock()
_start_time = time.time()
_total_polls = 0
_total_alerts = 0
_adsb_connected = False
_gotify_connected = False
_last_poll_time: Optional[float] = None
_receiver_aircraft_count = 0
_aircraft_in_radius: list[dict] = []
_recent_alerts: deque = deque(maxlen=50)


# ─── ADS-B Category → Aircraft Type Mapping ────────────────────────

HELO_TYPE_PREFIXES = {
    # Bell
    "B06", "B105", "B206", "B212", "B222", "B230", "B407", "B412",
    "B429", "B430", "B47G", "B47J", "BK17",
    # Sikorsky
    "S55", "S58", "S61", "S64", "S65", "S70", "S76", "S76B", "S76C",
    "S76D", "S92", "UH60", "H60",
    # Airbus / Eurocopter
    "AS32", "AS33", "AS35", "AS36", "AS50", "AS55", "AS65", "EC20",
    "EC25", "EC30", "EC35", "EC45", "EC55", "EC75", "EC13", "EC15",
    "EC63", "H135", "H145", "H155", "H160", "H175", "H215", "H225",
    "SA31", "SA32", "SA33", "SA34", "SA36", "SA34", "SA36", "SA34",
    "LAMA", "PUMA", "GAZL", "GAZE", "GAMA",
    # Robinson
    "R22", "R44", "R66",
    # MD Helicopters
    "MD52", "MD60", "MD90", "EXPL",
    # Boeing / military
    "CH47", "AH1", "AH64", "V22",
    # Leonardo / AgustaWestland
    "A109", "A119", "A139", "A149", "A169", "AW10", "AW13", "AW16",
    "AW18", "AW60",
    # Mil / Kamov (Russian)
    "MI2", "MI8", "MI17", "MI24", "MI26", "MI28", "KA27", "KA32",
    "KA50", "KA52",
    # Hughes / Schweizer
    "H269", "H500", "S300", "S330",
}

HELO_DESC_KEYWORDS = [
    "helicopter", "helo", "rotorcraft", "chopper",
    "black hawk", "apache", "chinook", "osprey",
    "huey", "cobra", "kiowa", "seahawk",
    "bell ", "sikorsky", "robinson r",
    "eurocopter", "airbus h1", "airbus h2",
    "md 500", "md 600", "md 900",
    "agusta", "leonardo",
]


def classify_aircraft(ac: dict) -> str:
    """
    Classify an aircraft dict into one of:
        helicopter, military_helo, plane, military_plane, unknown
    """
    category = (ac.get("category") or "").upper()
    icao_type = (ac.get("t") or "").upper()
    desc = (ac.get("desc") or "").lower()
    db_flags = ac.get("dbFlags", 0) or 0
    is_military = bool(db_flags & 1)

    is_helo = False

    if category == "A7":
        is_helo = True

    if not is_helo and icao_type:
        for prefix in HELO_TYPE_PREFIXES:
            if icao_type.startswith(prefix):
                is_helo = True
                break

    if not is_helo and desc:
        for kw in HELO_DESC_KEYWORDS:
            if kw in desc:
                is_helo = True
                break

    if is_helo:
        return "military_helo" if is_military else "helicopter"
    else:
        return "military_plane" if is_military else "plane"


# ─── Distance calculation ──────────────────────────────────────────

EARTH_RADIUS_MI = 3958.8  # mean radius in statute miles


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in statute miles."""
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(a))


# ─── Enrichment Caching & Lookups ──────────────────────────────────

_aircraft_meta_cache: dict[str, dict] = {}
_route_cache: dict[str, dict] = {}
_airport_cache: dict[str, str] = {}
_tar1090_db_ver: Optional[str] = None


def get_airport_info(icao: str) -> str:
    """Lookup airport name/city for an ICAO code, cached."""
    icao = icao.strip().upper()
    if not icao:
        return ""
    if icao in _airport_cache:
        return _airport_cache[icao]

    try:
        r = requests.get(f"https://hexdb.io/api/v1/airport/icao/{icao}", timeout=2.0)
        if r.status_code == 200:
            d = r.json()
            name = d.get("airport", "")
            iata = d.get("iata", "")
            label = iata or icao
            formatted = f"{label} ({name})" if name else icao
            _airport_cache[icao] = formatted
            return formatted
    except Exception:
        pass

    _airport_cache[icao] = icao
    return icao


def get_aircraft_meta(hex_code: str) -> dict:
    """Look up full manufacturer, model/type name, operator, and registration."""
    hex_code = hex_code.strip().lower()
    if hex_code in _aircraft_meta_cache:
        return _aircraft_meta_cache[hex_code]

    meta = {
        "manufacturer": "",
        "type_name": "",
        "owner": "",
        "registration": "",
    }

    try:
        r = requests.get(f"https://api.adsbdb.com/v0/aircraft/{hex_code}", timeout=2.5)
        if r.status_code == 200:
            d = r.json().get("response", {}).get("aircraft", {})
            meta["manufacturer"] = d.get("manufacturer") or ""
            meta["type_name"] = d.get("type") or ""
            meta["owner"] = d.get("registered_owner") or ""
            meta["registration"] = d.get("registration") or ""
    except Exception:
        pass

    if not (meta["manufacturer"] and meta["type_name"]):
        try:
            r = requests.get(f"https://hexdb.io/api/v1/aircraft/{hex_code}", timeout=2.5)
            if r.status_code == 200:
                d = r.json()
                meta["manufacturer"] = meta["manufacturer"] or d.get("Manufacturer") or ""
                meta["type_name"] = meta["type_name"] or d.get("Type") or ""
                meta["owner"] = meta["owner"] or d.get("RegisteredOwners") or ""
                meta["registration"] = meta["registration"] or d.get("Registration") or ""
        except Exception:
            pass

    if not (meta["manufacturer"] and meta["type_name"]):
        global _tar1090_db_ver
        try:
            base = cfg.tar1090_base_url
            if not _tar1090_db_ver:
                vr = requests.get(f"{base}/version.json", timeout=1.5)
                if vr.status_code == 200:
                    _tar1090_db_ver = vr.json().get("databaseVersion", "")
            if _tar1090_db_ver:
                prefix = hex_code[:2].upper()
                suffix = hex_code[2:].upper()
                dr = requests.get(f"{base}/db-{_tar1090_db_ver}/{prefix}.js", timeout=2.0)
                if dr.status_code == 200:
                    entry = dr.json().get(suffix)
                    if entry:
                        meta["registration"] = meta["registration"] or (entry[0] or "")
                        desc = entry[3] or ""
                        if desc and not meta["type_name"]:
                            meta["type_name"] = desc
        except Exception:
            pass

    _aircraft_meta_cache[hex_code] = meta
    return meta


def get_route_info(callsign: str) -> dict:
    """Look up flight route (origin, destination) by callsign."""
    callsign = callsign.strip().upper()
    if not callsign or callsign == "N/A":
        return {
            "origin": "Unfiled / Local",
            "destination": "Unfiled / Local",
            "route": "Local / Unfiled (VFR)",
        }

    if callsign in _route_cache:
        return _route_cache[callsign]

    res = {
        "origin": "Unfiled / Local",
        "destination": "Unfiled / Local",
        "route": "Local / Unfiled (VFR)",
    }

    try:
        r = requests.get(f"https://api.adsbdb.com/v0/callsign/{callsign}", timeout=2.5)
        if r.status_code == 200:
            fr = r.json().get("response", {}).get("flightroute", {})
            orig = fr.get("origin", {})
            dest = fr.get("destination", {})
            orig_code = orig.get("iata_code") or orig.get("icao_code") or ""
            dest_code = dest.get("iata_code") or dest.get("icao_code") or ""
            orig_city = orig.get("municipality") or orig.get("name") or ""
            dest_city = dest.get("municipality") or dest.get("name") or ""

            if orig_code and dest_code:
                orig_label = f"{orig_code} ({orig_city})" if orig_city else orig_code
                dest_label = f"{dest_code} ({dest_city})" if dest_city else dest_code
                res = {
                    "origin": orig_label,
                    "destination": dest_label,
                    "route": f"{orig_code} ➔ {dest_code}",
                }
                _route_cache[callsign] = res
                return res
    except Exception:
        pass

    try:
        r = requests.get(f"https://hexdb.io/api/v1/route/icao/{callsign}", timeout=2.0)
        if r.status_code == 200:
            route_str = r.json().get("route", "")
            if "-" in route_str:
                p1, p2 = route_str.split("-", 1)
                orig_info = get_airport_info(p1)
                dest_info = get_airport_info(p2)
                res = {
                    "origin": orig_info,
                    "destination": dest_info,
                    "route": f"{p1} ➔ {p2}",
                }
                _route_cache[callsign] = res
                return res
    except Exception:
        pass

    try:
        r = requests.get(f"https://opensky-network.org/api/routes?callsign={callsign}", timeout=2.0)
        if r.status_code == 200:
            rt = r.json().get("route", [])
            if len(rt) >= 2:
                p1, p2 = rt[0], rt[-1]
                orig_info = get_airport_info(p1)
                dest_info = get_airport_info(p2)
                res = {
                    "origin": orig_info,
                    "destination": dest_info,
                    "route": f"{p1} ➔ {p2}",
                }
                _route_cache[callsign] = res
                return res
    except Exception:
        pass

    _route_cache[callsign] = res
    return res


# ─── Gotify notification ───────────────────────────────────────────

TYPE_EMOJI = {
    "helicopter": "🚁",
    "military_helo": "🪖🚁",
    "plane": "✈️",
    "military_plane": "🪖✈️",
}

PRIORITY_MAP = {
    "helicopter": 5,
    "military_helo": 8,
    "plane": 3,
    "military_plane": 7,
}


def send_gotify(title: str, message: str, priority: int = 5, click_url: Optional[str] = None,
                override_url: Optional[str] = None, override_token: Optional[str] = None) -> tuple[bool, str]:
    """Push a notification to Gotify with Markdown rendering and click action."""
    base_gotify = (override_url or cfg.gotify_url).strip().rstrip("/")
    token = (override_token or cfg.gotify_token).strip()

    if not base_gotify or not token:
        return False, "Gotify URL or Token not configured"

    url = f"{base_gotify}/message"
    payload = {
        "title": title,
        "message": message,
        "priority": priority,
        "extras": {
            "client::display": {"contentType": "text/markdown"},
        },
    }

    target_url = click_url or cfg.tracker_url
    if target_url:
        payload["extras"]["client::notification"] = {"click": {"url": target_url}}

    try:
        resp = requests.post(
            url,
            params={"token": token},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Gotify notification sent: %s", title)
        return True, "Notification sent successfully"
    except requests.RequestException as exc:
        log.error("Gotify push failed: %s", exc)
        return False, str(exc)


# ─── Formatting ─────────────────────────────────────────────────────

def build_tracker_url(ac: dict, hex_code: str, registration: str, tracker_base: Optional[str] = None) -> tuple[str, list[tuple[str, str]]]:
    """
    Construct click-through URL and markdown links based on the configured tracker.
    Returns (primary_click_url, [(label, url), ...])
    """
    base = tracker_base if tracker_base is not None else cfg.tracker_url
    base = (base or "").strip()
    if not base:
        return "", []

    lat = ac.get("lat")
    lon = ac.get("lon")
    clean_reg = (registration or "").strip()
    is_fr24 = "flightradar24.com" in base.lower()
    is_adsbx = "adsbexchange.com" in base.lower()

    if is_fr24:
        # Flightradar24: live map view centered at the aircraft coordinates
        if lat is not None and lon is not None:
            primary_url = f"https://www.flightradar24.com/{lat:.4f},{lon:.4f}/13"
        else:
            primary_url = "https://www.flightradar24.com"

        links = [("🛰️ View Live on Flightradar24", primary_url)]
        if clean_reg and clean_reg != "N/A":
            fr24_reg_url = f"https://www.flightradar24.com/data/aircraft/{clean_reg.lower()}"
            links.append((f"📋 Aircraft Details ({clean_reg})", fr24_reg_url))
        return primary_url, links

    elif is_adsbx:
        primary_url = f"https://globe.adsbexchange.com/?icao={hex_code.lower()}"
        return primary_url, [("🛰️ View on ADSBexchange", primary_url)]

    else:
        # tar1090 / SkyAware or custom tracker URL
        primary_url = f"{base.rstrip('/')}/?icao={hex_code.lower()}"
        return primary_url, [("🛰️ View Live on Radar", primary_url)]


def format_alert(ac: dict, ac_type: str, distance: float, meta: dict, route: dict) -> tuple[str, str, str]:
    """Return (title, body, tracker_url) for a Gotify notification."""
    emoji = TYPE_EMOJI.get(ac_type, "❓")
    hex_code = ac.get("hex", "unknown").upper()
    flight = (ac.get("flight") or "").strip() or "N/A"
    icao_model = ac.get("t") or "Unknown"

    manufacturer = meta.get("manufacturer") or ""
    full_type = meta.get("type_name") or ac.get("desc") or icao_model
    registration = meta.get("registration") or ac.get("r") or "N/A"
    operator = meta.get("owner") or ""

    alt = ac.get("alt_baro")
    speed = ac.get("gs")
    squawk = ac.get("squawk", "")

    # Build concise, descriptive title
    type_label = ac_type.replace("_", " ").title()
    if manufacturer and full_type:
        if manufacturer.lower() in full_type.lower():
            title_desc = full_type
        else:
            title_desc = f"{manufacturer} {full_type}"
        title = f"{emoji} {type_label} – {title_desc}"
    elif icao_model and icao_model != "Unknown":
        title = f"{emoji} {type_label} – {icao_model}"
    else:
        title = f"{emoji} {type_label} – {hex_code}"

    # Build markdown details
    lines = []
    if manufacturer:
        lines.append(f"**Manufacturer:** {manufacturer}")
    lines.append(f"**Model / Type:** {full_type} (`{icao_model}`)")
    if registration and registration != "N/A":
        lines.append(f"**Registration:** {registration}")
    if operator:
        lines.append(f"**Operator:** {operator}")
    if flight and flight != "N/A" and flight != registration:
        lines.append(f"**Callsign:** {flight}")
    lines.append(f"**ICAO Hex:** `{hex_code}`")
    lines.append(f"**Distance:** {distance:.1f} mi")

    if alt is not None:
        lines.append(f"**Altitude:** {alt:,} ft" if isinstance(alt, int) else f"**Altitude:** {alt} ft")
    if speed is not None:
        lines.append(f"**Speed:** {speed:.0f} kt")
    if squawk:
        lines.append(f"**Squawk:** `{squawk}`")

    # Route details
    lines.append("---")
    route_summary = route.get("route", "Local / Unfiled (VFR)")
    lines.append(f"**Route:** {route_summary}")
    orig = route.get("origin", "")
    dest = route.get("destination", "")
    if (orig and orig != "Unfiled / Local") or (dest and dest != "Unfiled / Local"):
        lines.append(f"**Origin:** {orig or 'N/A'}")
        lines.append(f"**Destination:** {dest or 'N/A'}")

    # Build tracking links (defaulting to Flightradar24)
    primary_url, tracker_links = build_tracker_url(ac, hex_code, registration)
    if tracker_links:
        lines.append("")
        for label, url in tracker_links:
            lines.append(f"[{label}]({url})")

    body = "\n".join(lines)
    return title, body, primary_url


# ─── Main polling loop ──────────────────────────────────────────────

_alerted: dict[str, float] = {}


def poll_once() -> int:
    """Fetch aircraft, classify, enrich, filter, alert. Returns count of alerts sent."""
    global _total_polls, _total_alerts, _adsb_connected, _last_poll_time
    global _receiver_aircraft_count, _aircraft_in_radius

    now = time.time()
    with _stats_lock:
        _total_polls += 1
        _last_poll_time = now

    try:
        resp = requests.get(cfg.adsb_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        with _stats_lock:
            _adsb_connected = True
    except Exception as exc:
        with _stats_lock:
            _adsb_connected = False
            _aircraft_in_radius = []
        log.warning("Failed to fetch aircraft data from %s: %s", cfg.adsb_url, exc)
        return 0

    aircraft_list = data.get("aircraft", [])
    with _stats_lock:
        _receiver_aircraft_count = len(aircraft_list)

    alerts_sent = 0
    current_in_radius = []

    for ac in aircraft_list:
        lat = ac.get("lat")
        lon = ac.get("lon")
        if lat is None or lon is None:
            continue

        distance = haversine_mi(cfg.base_lat, cfg.base_lon, lat, lon)
        if distance > cfg.max_distance_mi:
            continue

        ac_type = classify_aircraft(ac)
        hex_code = ac.get("hex", "unknown").lower()
        flight = (ac.get("flight") or "").strip()
        reg = ac.get("r", "")

        primary_url, _ = build_tracker_url(ac, hex_code, reg)

        current_in_radius.append({
            "hex": hex_code,
            "type": ac_type,
            "flight": flight,
            "registration": reg,
            "model": ac.get("t", ""),
            "desc": ac.get("desc", ""),
            "distance": round(distance, 2),
            "altitude": ac.get("alt_baro"),
            "speed": ac.get("gs"),
            "squawk": ac.get("squawk", ""),
            "lat": lat,
            "lon": lon,
            "click_url": primary_url,
        })

        if ac_type not in cfg.alert_types:
            continue

        # Cooldown check
        last_alert = _alerted.get(hex_code, 0)
        if (now - last_alert) < cfg.alert_cooldown:
            continue

        # Enrich metadata & route
        meta = get_aircraft_meta(hex_code)
        route = get_route_info(flight)

        title, body, tracker_link = format_alert(ac, ac_type, distance, meta, route)
        priority = PRIORITY_MAP.get(ac_type, 5)

        success, msg = send_gotify(title, body, priority, click_url=tracker_link)
        if success:
            _alerted[hex_code] = now
            alerts_sent += 1
            with _stats_lock:
                _total_alerts += 1
                _recent_alerts.appendleft({
                    "timestamp": now,
                    "hex": hex_code,
                    "type": ac_type,
                    "aircraft_label": title.split("–")[-1].strip() if "–" in title else hex_code,
                    "distance": round(distance, 1),
                    "route": route.get("route", "VFR"),
                    "click_url": tracker_link,
                })
            log.info(
                "ALERT  %-6s  %-12s  type=%-15s  dist=%.1f mi  mfr=%-15s  route=%s",
                hex_code,
                ac.get("t", "?"),
                ac_type,
                distance,
                meta.get("manufacturer") or "N/A",
                route.get("route", "VFR"),
            )

    # Sort in-radius list by closest distance
    current_in_radius.sort(key=lambda x: x["distance"])
    with _stats_lock:
        _aircraft_in_radius = current_in_radius

    # Evict stale entries from cooldown cache
    stale = [k for k, v in _alerted.items() if (now - v) > cfg.alert_cooldown * 2]
    for k in stale:
        del _alerted[k]

    return alerts_sent


# ─── Web Request Handler ───────────────────────────────────────────

HTML_FILE_PATH = os.path.join(os.path.dirname(__file__), "web", "index.html")


class WebHandler(http.server.BaseHTTPRequestHandler):
    """Simple JSON & HTML HTTP handler for Settings and Dashboard."""

    def log_message(self, format, *args):
        web_log.debug("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            if os.path.isfile(HTML_FILE_PATH):
                with open(HTML_FILE_PATH, "rb") as f:
                    content = f.read()
            else:
                content = b"<!DOCTYPE html><html><body><h1>heli-alert</h1><p>Web UI file not found.</p></body></html>"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/api/settings":
            with _config_lock:
                settings_dict = {
                    "adsb_url": cfg.adsb_url,
                    "base_lat": cfg.base_lat,
                    "base_lon": cfg.base_lon,
                    "gotify_url": cfg.gotify_url,
                    "gotify_token": cfg.gotify_token,
                    "max_distance_mi": cfg.max_distance_mi,
                    "alert_types": sorted(list(cfg.alert_types)),
                    "poll_interval": cfg.poll_interval,
                    "alert_cooldown": cfg.alert_cooldown,
                    "tracker_url": cfg.tracker_url,
                    "web_port": cfg.web_port,
                }
            self._send_json(200, settings_dict)
            return

        if path == "/api/status":
            with _stats_lock:
                status_payload = {
                    "poller_running": True,
                    "uptime_seconds": int(time.time() - _start_time),
                    "total_polls": _total_polls,
                    "total_alerts_sent": _total_alerts,
                    "adsb_connected": _adsb_connected,
                    "gotify_reachable": bool(cfg.gotify_token),
                    "gotify_configured": bool(cfg.gotify_token),
                    "poll_interval": cfg.poll_interval,
                    "alert_cooldown": cfg.alert_cooldown,
                    "last_poll_time": _last_poll_time,
                    "receiver_aircraft_count": _receiver_aircraft_count,
                    "in_radius_count": len(_aircraft_in_radius),
                    "aircraft_in_radius": list(_aircraft_in_radius),
                    "recent_alerts": list(_recent_alerts),
                }
            self._send_json(200, status_payload)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(post_data.decode("utf-8")) if post_data else {}
        except json.JSONDecodeError:
            self._send_json(400, {"success": False, "error": "Invalid JSON body"})
            return

        if path == "/api/settings":
            success, msg = save_config_data(payload)
            if success:
                self._send_json(200, {"success": True, "message": msg})
            else:
                self._send_json(400, {"success": False, "error": msg})
            return

        if path == "/api/test-adsb":
            test_url = payload.get("adsb_url") or cfg.adsb_url
            try:
                r = requests.get(test_url, timeout=5)
                r.raise_for_status()
                data = r.json()
                count = len(data.get("aircraft", []))
                self._send_json(200, {"success": True, "aircraft_count": count})
            except Exception as exc:
                self._send_json(200, {"success": False, "error": str(exc)})
            return

        if path == "/api/test-gotify":
            test_url = payload.get("gotify_url") or cfg.gotify_url
            test_token = payload.get("gotify_token") or cfg.gotify_token
            tracker = payload.get("tracker_url") or cfg.tracker_url
            lat = payload.get("base_lat") or cfg.base_lat
            lon = payload.get("base_lon") or cfg.base_lon

            test_ac = {
                "hex": "A1B2C3",
                "lat": lat,
                "lon": lon,
                "alt_baro": 1200,
                "gs": 115,
                "squawk": "1200",
                "t": "B407",
                "r": "N12345",
                "desc": "Bell 407",
            }
            primary_url, links = build_tracker_url(test_ac, "A1B2C3", "N12345", tracker_base=tracker)

            title = "🚁 Helicopter – Bell 407 (N12345) [TEST]"
            lines = [
                "**Manufacturer:** Bell",
                "**Model / Type:** Bell 407 (`B407`)",
                "**Registration:** N12345",
                "**Operator:** Flight Test Service",
                "**ICAO Hex:** `A1B2C3`",
                "**Distance:** 1.5 mi",
                "**Altitude:** 1,200 ft",
                "**Speed:** 115 kt",
                "---",
                "**Route:** Local / Unfiled (VFR)",
            ]
            if links:
                lines.append("")
                for lbl, u in links:
                    lines.append(f"[{lbl}]({u})")

            body = "\n".join(lines)
            success, msg = send_gotify(title, body, priority=5, click_url=primary_url,
                                       override_url=test_url, override_token=test_token)
            if success:
                self._send_json(200, {"success": True, "message": "Notification sent successfully"})
            else:
                self._send_json(200, {"success": False, "error": msg})
            return

        self.send_error(404, "Not Found")


# ─── Server & Worker Lifecycle ──────────────────────────────────────

_stop_event = threading.Event()


def poller_worker():
    """Background polling worker."""
    log.info("Poller worker started (interval: %ds, radius: %.1f mi)", cfg.poll_interval, cfg.max_distance_mi)
    while not _stop_event.is_set():
        if cfg.base_lat == 0 and cfg.base_lon == 0:
            log.debug("Waiting for base coordinates to be configured...")
        elif not cfg.gotify_token:
            log.debug("Waiting for Gotify token to be configured...")
        else:
            try:
                poll_once()
            except Exception:
                log.exception("Unexpected error in poll cycle")

        # Sleep in short increments to respond promptly to stop event or interval changes
        interval = max(2, cfg.poll_interval)
        for _ in range(interval * 2):
            if _stop_event.is_set():
                break
            time.sleep(0.5)

    log.info("Poller worker stopped")


def main():
    log.info("==================================================")
    log.info("🚁 heli-alert starting")
    log.info("  Web UI:        http://%s:%d", cfg.web_host, cfg.web_port)
    log.info("  ADS-B source:  %s", cfg.adsb_url)
    log.info("  Base station:  %.4f, %.4f", cfg.base_lat, cfg.base_lon)
    log.info("  Gotify:        %s", cfg.gotify_url)
    log.info("  Alert radius:  %.1f mi", cfg.max_distance_mi)
    log.info("  Alert types:   %s", ", ".join(sorted(cfg.alert_types)))
    log.info("  Tracker URL:   %s", cfg.tracker_url)
    log.info("  Poll interval: %d s", cfg.poll_interval)
    log.info("  Cooldown:      %d s", cfg.alert_cooldown)
    log.info("==================================================")

    # Start poller thread
    t = threading.Thread(target=poller_worker, name="PollerWorker", daemon=True)
    t.start()

    # Start Web UI server
    server_address = (cfg.web_host, cfg.web_port)
    httpd = http.server.ThreadingHTTPServer(server_address, WebHandler)
    log.info("Web dashboard active at http://%s:%d", cfg.web_host, cfg.web_port)

    def shutdown(signum, frame):
        log.info("Shutdown signal received (%s), terminating...", signum)
        _stop_event.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        httpd.server_close()
        log.info("heli-alert shutdown complete")


if __name__ == "__main__":
    main()
