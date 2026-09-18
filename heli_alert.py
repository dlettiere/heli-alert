#!/usr/bin/env python3
"""
heli-alert  –  ADS-B → Gotify aircraft proximity notifier

Polls a local tar1090 / readsb aircraft.json feed, classifies each
aircraft by type (helicopter, plane, military variants), calculates
distance from your base station, enriches aircraft metadata (full type,
manufacturer, operator) and flight route (origin, destination), and
pushes a Gotify notification when a matching aircraft enters your alert radius.
"""

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
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

# ─── Configuration ──────────────────────────────────────────────────

load_dotenv()


@dataclass
class Config:
    adsb_url: str = os.getenv("ADSB_URL", "http://localhost:8078/data/aircraft.json")
    base_lat: float = float(os.getenv("BASE_LAT", "0"))
    base_lon: float = float(os.getenv("BASE_LON", "0"))
    gotify_url: str = os.getenv("GOTIFY_URL", "http://localhost:8380")
    gotify_token: str = os.getenv("GOTIFY_APP_TOKEN", "")
    max_distance_mi: float = float(os.getenv("MAX_DISTANCE_MI", "3.0"))
    alert_types: set = field(default_factory=lambda: set())
    poll_interval: int = int(os.getenv("POLL_INTERVAL", "10"))
    alert_cooldown: int = int(os.getenv("ALERT_COOLDOWN", "300"))
    tracker_url: str = os.getenv("TRACKER_URL", "")

    def __post_init__(self):
        raw = os.getenv("ALERT_TYPES", "helicopter,military_helo")
        self.alert_types = {t.strip() for t in raw.split(",")}

    @property
    def tar1090_base_url(self) -> str:
        """Derive base URL (e.g. http://host:port) from adsb_url for db lookups."""
        p = urlparse(self.adsb_url)
        return f"{p.scheme}://{p.netloc}"


cfg = Config()

# ─── ADS-B Category → Aircraft Type Mapping ────────────────────────
#
# readsb / tar1090 provide a "category" field derived from the
# ADS-B emitter category set (DO-260B §2.2.3.2.7.8.1.1).
#
# Category hex values:
#   A0-A7  =  fixed-wing / general aviation
#   B0-B7  =  misc (glider, balloon, skydiver, etc.)
#   C0-C3  =  reserved / surface / obstacle
#   A7     =  rotorcraft (helicopters)
#
# The "t" (type) field gives the ICAO type designator (e.g. "B407"
# for Bell 407, "UH60" for Black Hawk).  The "dbFlags" bitmask
# indicates military (bit 0) and interesting (bit 1) aircraft.

# Common helicopter ICAO type codes for fallback detection when
# the ADS-B category field is missing or generic.
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

# Common helicopter description substrings
HELO_DESC_KEYWORDS = [
    "helicopter", "helo", "rotorcraft", "chopper",
    "black hawk", "apache", "chinook", "osprey",
    "huey", "cobra", "kiowa", "seahawk",
    "bell ", "sikorsky", "robinson r",
    "eurocopter", "airbus h1", "airbus h2",
    "md 500", "md 600", "md 900",
    "agusta", "leonardo",
]


# ─── Aircraft classification ───────────────────────────────────────

def classify_aircraft(ac: dict) -> str:
    """
    Classify an aircraft dict into one of:
        helicopter, military_helo, plane, military_plane, unknown

    Uses category, type designator, description, and dbFlags.
    """
    category = (ac.get("category") or "").upper()
    icao_type = (ac.get("t") or "").upper()
    desc = (ac.get("desc") or "").lower()
    db_flags = ac.get("dbFlags", 0) or 0
    is_military = bool(db_flags & 1)

    is_helo = False

    # 1. ADS-B emitter category A7 = rotorcraft
    if category == "A7":
        is_helo = True

    # 2. ICAO type designator match
    if not is_helo and icao_type:
        for prefix in HELO_TYPE_PREFIXES:
            if icao_type.startswith(prefix):
                is_helo = True
                break

    # 3. Description keyword match
    if not is_helo and desc:
        for kw in HELO_DESC_KEYWORDS:
            if kw in desc:
                is_helo = True
                break

    if is_helo:
        return "military_helo" if is_military else "helicopter"
    else:
        # Anything with a position that isn't a helo we call a plane
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

# Caches for metadata, routes, and airport names
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
    """
    Look up full manufacturer, model/type name, operator, and registration.
    Checks:
      1. Memory cache
      2. adsbdb.com API
      3. hexdb.io API
      4. Local tar1090 internal db
    """
    hex_code = hex_code.strip().lower()
    if hex_code in _aircraft_meta_cache:
        return _aircraft_meta_cache[hex_code]

    meta = {
        "manufacturer": "",
        "type_name": "",
        "owner": "",
        "registration": "",
    }

    # 1. adsbdb API
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

    # 2. hexdb.io API fallback
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

    # 3. Local tar1090 internal db fallback
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
    """
    Look up flight route (origin, destination) by callsign.
    Checks:
      1. Memory cache
      2. adsbdb.com API
      3. hexdb.io route API
      4. opensky-network API
    """
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

    # 1. adsbdb flightroute API
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

    # 2. hexdb route API
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

    # 3. opensky-network route API
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


def send_gotify(title: str, message: str, priority: int = 5, click_url: Optional[str] = None) -> bool:
    """Push a notification to Gotify with Markdown rendering and click action."""
    url = f"{cfg.gotify_url.rstrip('/')}/message"
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
            params={"token": cfg.gotify_token},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        log.info("Gotify notification sent: %s", title)
        return True
    except requests.RequestException as exc:
        log.error("Gotify push failed: %s", exc)
        return False


# ─── Formatting ─────────────────────────────────────────────────────

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

    # Clickable link to radar map focused on this aircraft
    tracker_link = ""
    if cfg.tracker_url:
        tracker_link = f"{cfg.tracker_url.rstrip('/')}/?icao={hex_code.lower()}"
        lines.append("")
        lines.append(f"[🛰️ View Live on Radar]({tracker_link})")

    body = "\n".join(lines)
    return title, body, tracker_link


# ─── Main loop ──────────────────────────────────────────────────────

# Track recently alerted aircraft to avoid spam.
# Key: ICAO hex, Value: timestamp of last alert
_alerted: dict[str, float] = {}


def poll_once() -> int:
    """Fetch aircraft, classify, enrich, filter, alert. Returns count of alerts sent."""
    try:
        resp = requests.get(cfg.adsb_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, json.JSONDecodeError) as exc:
        log.warning("Failed to fetch aircraft data: %s", exc)
        return 0

    aircraft_list = data.get("aircraft", [])
    now = time.time()
    alerts_sent = 0

    for ac in aircraft_list:
        lat = ac.get("lat")
        lon = ac.get("lon")
        if lat is None or lon is None:
            continue  # no position → skip

        distance = haversine_mi(cfg.base_lat, cfg.base_lon, lat, lon)
        if distance > cfg.max_distance_mi:
            continue

        ac_type = classify_aircraft(ac)
        if ac_type not in cfg.alert_types:
            continue

        hex_code = ac.get("hex", "unknown").lower()

        # Cooldown check
        last_alert = _alerted.get(hex_code, 0)
        if (now - last_alert) < cfg.alert_cooldown:
            continue

        flight = (ac.get("flight") or "").strip()

        # Enrich metadata & route
        meta = get_aircraft_meta(hex_code)
        route = get_route_info(flight)

        title, body, tracker_link = format_alert(ac, ac_type, distance, meta, route)
        priority = PRIORITY_MAP.get(ac_type, 5)

        if send_gotify(title, body, priority, click_url=tracker_link):
            _alerted[hex_code] = now
            alerts_sent += 1
            log.info(
                "ALERT  %-6s  %-12s  type=%-15s  dist=%.1f mi  mfr=%-15s  route=%s",
                hex_code,
                ac.get("t", "?"),
                ac_type,
                distance,
                meta.get("manufacturer") or "N/A",
                route.get("route", "VFR"),
            )

    # Evict stale entries from cooldown cache
    stale = [k for k, v in _alerted.items() if (now - v) > cfg.alert_cooldown * 2]
    for k in stale:
        del _alerted[k]

    return alerts_sent


def validate_config():
    """Fail fast on misconfiguration."""
    errors = []
    if cfg.base_lat == 0 and cfg.base_lon == 0:
        errors.append("BASE_LAT / BASE_LON are both 0 – set your receiver coordinates")
    if not cfg.gotify_token or cfg.gotify_token == "CHANGE_ME":
        errors.append("GOTIFY_APP_TOKEN is not set – create an app in Gotify and paste the token")
    if not cfg.alert_types:
        errors.append("ALERT_TYPES is empty – set at least one type")
    if errors:
        for e in errors:
            log.error("CONFIG  %s", e)
        sys.exit(1)


def main():
    log.info("heli-alert starting")
    log.info("  ADS-B source:  %s", cfg.adsb_url)
    log.info("  Base station:  %.4f, %.4f", cfg.base_lat, cfg.base_lon)
    log.info("  Gotify:        %s", cfg.gotify_url)
    log.info("  Alert radius:  %.1f mi", cfg.max_distance_mi)
    log.info("  Alert types:   %s", ", ".join(sorted(cfg.alert_types)))
    log.info("  Poll interval: %d s", cfg.poll_interval)
    log.info("  Cooldown:      %d s", cfg.alert_cooldown)
    if cfg.tracker_url:
        log.info("  Tracker URL:   %s", cfg.tracker_url)

    validate_config()

    # Startup test – verify ADS-B and Gotify are reachable
    try:
        requests.get(cfg.adsb_url, timeout=5).raise_for_status()
        log.info("ADS-B endpoint reachable ✓")
    except requests.RequestException as exc:
        log.error("Cannot reach ADS-B endpoint: %s", exc)
        sys.exit(1)

    try:
        r = requests.get(
            f"{cfg.gotify_url.rstrip('/')}/version",
            timeout=5,
        )
        r.raise_for_status()
        log.info("Gotify reachable ✓  (version %s)", r.json().get("version", "?"))
    except requests.RequestException as exc:
        log.error("Cannot reach Gotify: %s", exc)
        sys.exit(1)

    while True:
        try:
            poll_once()
        except Exception:
            log.exception("Unexpected error during poll cycle")
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
