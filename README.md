# BirdNET-PiPy Adapter

A tiny Home Assistant add-on that lets the
[HABirdDashboard](https://github.com/adamoberley/HABirdDashboard) "**Bird
Card**" feed from **[BirdNET-PiPy](https://github.com/Suncuss/BirdNET-PiPy)**
instead of BirdNET-Go.

The Bird Card expects a BirdNET-Go API v2 backend. BirdNET-PiPy has a different
API, so this add-on reads BirdNET-PiPy over the LAN and speaks the BirdNET-Go
shapes the card uses. It runs on the HA host and points at your BirdNET-PiPy
instance.

```
browser -> Bird Card -> this add-on (birdnet_url) -> BirdNET-PiPy (LAN)
```

See [`docs/README.md`](docs/README.md) for full install + card configuration.

## What it serves (on top of BirdNET-PiPy)

| Card asks | Adapter serves, from BirdNET-PiPy |
|---|---|
| `/api/v2/analytics/species/summary` | `/api/species/all` + per-species count / best-confidence |
| `/api/v2/analytics/species/daily` | `/api/activity/overview` + best-confidence page (1H/12H/24H buckets) |
| `/api/v2/detections` (recordings) | `/api/detections?species=&sort=timestamp` |
| `/api/v2/audio/<id>` | proxied `/api/audio/<filename>` (with signed query) |
| `/api/v2/analytics/time/daily` | per-day `/api/detections` counts |
| `/api/v2/analytics/species/diversity` | `/api/sightings/unique` per day |
| `/api/v2/analytics/time/distribution/hourly` | summed `/api/activity/hourly` |
| `/api/v2/detections/stream` | polls latest detection (SSE refetch) |
| `/api/v2/weather/latest` | latest detection's stored weather (best-effort) |

## Install

1. **Settings → Add-ons → ⋮ → Repositories → Add** this repo URL:
   `https://github.com/valtomas/birdnet-pipy-adapter`
2. Find **BirdNET-PiPy Adapter** → **Install** → **Start**.
3. In the add-on **Settings**, confirm `birdnet_host` is your BirdNET-PiPy Pi
   (default `192.168.86.30`). **Save**.
4. In the **Bird Card** editor → **Connection & data** → **BirdNET-Go URL**
   (`birdnet_url`): `http://<HA-HOST-IP>:8099`

## Notes

- **Public repo required** — HA fetches add-on metadata anonymously, so the repo
  cannot be private.
- **Home LAN primary.** The card reaches the add-on via its LAN port (`:8099`).
  For remote (Nabu Casa) viewing, expose that port or point `birdnet_url` at the
  add-on's ingress URL (admin users).
- **Counts** come from BirdNET-PiPy per-species totals; 7-day counts are
  detections within that window.

## Layout

```
config.json        # add-on manifest + settings
build.json         # build context = rootfs/
Dockerfile         # python:3.12-slim
rootfs/app.py      # the adapter
rootfs/requirements.txt
docs/README.md     # detailed install + card config
```
