# 🚁 heli-alert

**ADS-B Proximity Notifier for Gotify with Web Settings Dashboard**

`heli-alert` monitors a local ADS-B feed (from `tar1090`, `readsb`, or `ultrafeeder`), identifies approaching helicopters, military rotorcraft, and military planes, and sends real-time rich markdown push notifications to [Gotify](https://gotify.net/). Includes an integrated web-based front end to easily adjust proximity settings, test alerts, and view live aircraft in your area.

---

## Features

- **Web Settings & Status Dashboard:** Simple, lightweight web UI accessible from your browser (port `8085`) to tune alert radius, aircraft filters, Gotify tokens, and receiver settings on the fly.
- **Flightradar24 Click-Through:** Tapping Gotify notifications immediately opens Flightradar24 centered directly on the aircraft's live coordinates, with direct links to aircraft registration and flight details.
- **Pure Local Proximity Math:** Uses the great-circle Haversine formula to calculate real-time distance from your target coordinates without leaking location data to external geocoding APIs.
- **Smart Aircraft Classification:** Identifies rotorcraft and military aircraft using DO-260B emitter categories (A7), ICAO type prefixes (e.g., `UH60`, `B407`, `S76`, `EC35`), and ADS-B `dbFlags` bitmasks.
- **Metadata & Route Enrichment:** Enriches alerts with operator, manufacturer, registration, callsign, squawk, altitude, speed, and origin/destination flight routes.
- **Spam Suppression:** Configurable alert radius and per-aircraft cooldown timers to avoid duplicate alerts while an aircraft remains in your area.
- **Multi-Architecture Docker Container:** Official builds published for `linux/amd64` (servers/PCs) and `linux/arm64` (Raspberry Pi, BTT Pi, Orange Pi).

---

## Web Front End

Access the web interface at `http://<your-server-ip>:8085`:

- **Settings Management:** Adjust coordinates, radius, polling intervals, and alert filters without restarting containers.
- **One-Click Testing:** Send test notifications to Gotify and ping your ADS-B feed directly from the UI.
- **Browser GPS Integration:** Populate base coordinates automatically using your browser's location.
- **Live Traffic in Radius:** Real-time table of all aircraft currently detected within your proximity radius.
- **Alert History:** Log of recent alerts pushed to Gotify during the session.

---

## Quick Start (Docker Compose)

### 1. Configure Environment

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```dotenv
# Web UI Port
WEB_PORT=8085

# ADS-B aircraft.json endpoint (tar1090 / readsb / ultrafeeder)
ADSB_URL=http://ultrafeeder/data/aircraft.json

# Your center coordinates (latitude & longitude)
BASE_LAT=42.2797
BASE_LON=-71.8208

# Gotify Server & App Token
GOTIFY_URL=http://gotify:8088
GOTIFY_APP_TOKEN=your_gotify_app_token_here

# Proximity radius in miles
MAX_DISTANCE_MI=5.0

# Alert filters (comma-separated: helicopter, military_helo, plane, military_plane, medflight)
ALERT_TYPES=helicopter,military_helo,military_plane,medflight
# Custom MedFlight tail numbers (comma-separated, optional)
MEDFLIGHT_TAILS=N982LF,N136KY,N141NE,N143NE,N144NE,N145NE

# Polling and cooldown (seconds)
POLL_INTERVAL=10
ALERT_COOLDOWN=300

# Live Tracker URL (Default: Flightradar24)
TRACKER_URL=https://www.flightradar24.com
```

### 2. Run with Docker Compose

```yaml
services:
  heli-alert:
    image: ghcr.io/dlettiere/heli-alert:latest
    container_name: heli-alert
    restart: unless-stopped
    ports:
      - "8085:8085"
    volumes:
      - ./data:/app/data
    env_file:
      - .env
```

Start the container:
```bash
docker compose up -d
```

Open `http://localhost:8085` in your browser.

---

## Running alongside ADS-B Receivers (e.g. tar1090 / readsb / ultrafeeder)

Add `heli-alert` directly into your ADS-B `docker-compose.yml`:

```yaml
services:
  tar1090:
    image: ghcr.io/sdr-enthusiasts/docker-tar1090:latest
    # ... tar1090 settings ...

  heli-alert:
    image: ghcr.io/dlettiere/heli-alert:latest
    container_name: heli-alert
    restart: unless-stopped
    depends_on:
      - tar1090
    ports:
      - "8085:8085"
    volumes:
      - ./data/heli-alert:/app/data
    environment:
      - TZ=America/New_York
      - ADSB_URL=http://tar1090/data/aircraft.json
      - BASE_LAT=42.2797
      - BASE_LON=-71.8208
      - MAX_DISTANCE_MI=5.0
      - ALERT_TYPES=helicopter,military_helo,military_plane,medflight
      - MEDFLIGHT_TAILS=N982LF,N136KY,N141NE,N143NE,N144NE,N145NE
      - TRACKER_URL=https://www.flightradar24.com
      - GOTIFY_URL=http://your-gotify-host:8088
      - GOTIFY_APP_TOKEN=your_token_here
```

---

## Configuration Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `WEB_PORT` | `8085` | Port for the web-based settings dashboard |
| `ADSB_URL` | `http://localhost:8080/data/aircraft.json` | URL to `aircraft.json` exposed by readsb or tar1090 |
| `BASE_LAT` | `0.0` | Target latitude for distance calculations |
| `BASE_LON` | `0.0` | Target longitude for distance calculations |
| `MAX_DISTANCE_MI` | `5.0` | Alert threshold radius in miles |
| `GOTIFY_URL` | `http://localhost:8380` | URL of your Gotify server |
| `GOTIFY_APP_TOKEN` | *(required)* | Gotify application API token |
| `ALERT_TYPES` | `helicopter,military_helo,military_plane,medflight` | Comma-separated list: `helicopter`, `military_helo`, `plane`, `military_plane`, `medflight` |
| `MEDFLIGHT_TAILS` | *(empty)* | Comma-separated tail numbers to alert as MedFlight (Air Methods, Life Flight, etc.) |
| `POLL_INTERVAL` | `10` | Interval between polling cycles in seconds |
| `ALERT_COOLDOWN` | `300` | Cooldown period before the same aircraft can trigger another alert |
| `TRACKER_URL` | `https://www.flightradar24.com` | Base URL for notification click-through and map links |

---

## License

MIT License. Feel free to use and modify for your homelab setup.
