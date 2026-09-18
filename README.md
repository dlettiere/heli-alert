# 🚁 heli-alert

**ADS-B Proximity Notifier for Gotify**

`heli-alert` monitors a local ADS-B feed (from `tar1090`, `readsb`, or `ultrafeeder`), identifies approaching helicopters, military rotorcraft, and military planes, and sends real-time rich markdown push notifications to [Gotify](https://gotify.net/).

---

## Features

- **Pure Local Proximity Math:** Uses the great-circle Haversine formula to calculate real-time distance from your target coordinates without leaking location data to external geocoding APIs.
- **Smart Aircraft Classification:** Identifies rotorcraft and military aircraft using DO-260B emitter categories (A7), ICAO type prefixes (e.g., `UH60`, `B407`, `S76`, `EC35`), and ADS-B `dbFlags` bitmasks.
- **Metadata & Route Enrichment:** Enriches alerts with operator, manufacturer, registration, callsign, squawk, altitude, speed, and origin/destination flight routes.
- **Interactive Notifications:** Includes direct clickable links back to your local `tar1090` radar map focused on the detected aircraft (`?icao=<hex>`).
- **Spam Suppression:** Configurable alert radius and per-aircraft cooldown timers to avoid duplicate alerts while an aircraft remains in your area.
- **Multi-Architecture Docker Container:** Official builds published for `linux/amd64` (servers/PCs) and `linux/arm64` (Raspberry Pi, BTT Pi, Orange Pi).

---

## Quick Start (Docker Compose)

### 1. Configure Environment

Create a `.env` file from the example:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```dotenv
# ADS-B aircraft.json endpoint (tar1090 / readsb / ultrafeeder)
ADSB_URL=http://ultrafeeder/data/aircraft.json

# Your center coordinates (latitude & longitude)
BASE_LAT=40.7128
BASE_LON=-74.0060

# Gotify Server & App Token
GOTIFY_URL=http://gotify:8088
GOTIFY_APP_TOKEN=your_gotify_app_token_here

# Proximity radius in miles
MAX_DISTANCE_MI=5.0

# Alert filters (comma-separated: helicopter, military_helo, plane, military_plane)
ALERT_TYPES=helicopter,military_helo,military_plane

# Polling and cooldown (seconds)
POLL_INTERVAL=10
ALERT_COOLDOWN=300

# (Optional) Public or local radar URL for click-through map tracking
TRACKER_URL=https://sky.example.com
```

### 2. Run with Docker Compose

```yaml
services:
  heli-alert:
    image: ghcr.io/dlettiere/heli-alert:latest
    container_name: heli-alert
    restart: unless-stopped
    env_file:
      - .env
```

Start the container:
```bash
docker compose up -d
```

---

## Running with Ultrafeeder / readsb

If you run `heli-alert` on the same host as `docker-adsb-ultrafeeder` (e.g., on a Raspberry Pi or BTT Pi), you can add `heli-alert` directly into your ADS-B `docker-compose.yml`:

```yaml
services:
  ultrafeeder:
    image: ghcr.io/sdr-enthusiasts/docker-adsb-ultrafeeder:latest
    container_name: ultrafeeder
    restart: unless-stopped
    ports:
      - "8080:80"
    # ... your ultrafeeder settings ...

  heli-alert:
    image: ghcr.io/dlettiere/heli-alert:latest
    container_name: heli-alert
    restart: unless-stopped
    depends_on:
      - ultrafeeder
    environment:
      - TZ=America/New_York
      - ADSB_URL=http://ultrafeeder/data/aircraft.json
      - BASE_LAT=40.7128
      - BASE_LON=-74.0060
      - MAX_DISTANCE_MI=5.0
      - ALERT_TYPES=helicopter,military_helo,military_plane
      - GOTIFY_URL=http://your-gotify-host:8088
      - GOTIFY_APP_TOKEN=your_token_here
      - TRACKER_URL=https://sky.example.com
```

---

## Configuration Reference

| Variable | Default | Description |
| :--- | :--- | :--- |
| `ADSB_URL` | `http://localhost:8080/data/aircraft.json` | URL to `aircraft.json` exposed by readsb or tar1090 |
| `BASE_LAT` | `0.0` | Target latitude for distance calculations |
| `BASE_LON` | `0.0` | Target longitude for distance calculations |
| `MAX_DISTANCE_MI` | `3.0` | Alert threshold radius in miles |
| `GOTIFY_URL` | `http://localhost:8380` | URL of your Gotify server |
| `GOTIFY_APP_TOKEN` | *(required)* | Gotify application API token |
| `ALERT_TYPES` | `helicopter,military_helo` | Comma-separated list: `helicopter`, `military_helo`, `plane`, `military_plane` |
| `POLL_INTERVAL` | `10` | Interval between polling cycles in seconds |
| `ALERT_COOLDOWN` | `300` | Cooldown period before the same aircraft can trigger another alert |
| `TRACKER_URL` | `""` | Optional base URL for your tar1090 map |

---

## License

MIT License. Feel free to use and modify for your homelab setup.
