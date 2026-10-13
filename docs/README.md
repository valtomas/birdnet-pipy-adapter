# BirdNET-PiPy -> Bird Card adapter

Makes the [HABirdDashboard](https://github.com/adamoberley/HABirdDashboard)
"Bird Card" feed from **[BirdNET-PiPy](https://github.com/Suncuss/BirdNET-PiPy)**
instead of BirdNET-Go.

The Bird Card speaks a BirdNET-Go API v2 dialect. BirdNET-PiPy has a different
API, so this add-on translates: it reads BirdNET-PiPy over the LAN and serves
the shapes the card expects. It runs as an add-on on the **HA host** and points
at your BirdNET-PiPy instance.

```
browser -> Bird Card -> this add-on (birdnet_url) -> BirdNET-PiPy (:80 on LAN)
```

## Files

```
birdnet-pipy-adapter/
├── config.json        # add-on manifest + settings
├── build.json         # build options (build_from/squash/args/labels)
├── Dockerfile         # python:3.12-slim + app (build context = repo root)
├── rootfs/
│   ├── app.py         # the adapter
│   └── requirements.txt
└── docs/README.md     # this file
```

## Install as an HA add-on

1. In Home Assistant: **Settings → Add-ons → Add-on store → ⋮ → Repositories**,
   then add **this folder** as a repository (point it at
   `birdnet-pipy-adapter/` — the folder that contains `config.json`). If you
   cloned it somewhere reachable, copy the folder into your HA config
   (e.g. `/config/birdnet-pipy-adapter/`).
2. Find **BirdNET-PiPy Adapter**, click **Install**, then **Start**.
3. In the add-on **Settings**, confirm `birdnet_host` is your Pi
   (default `192.168.86.30`) and `birdnet_port` is `80`. Adjust
   `cache_ttl`/`http_timeout`/`max_workers` if needed. **Save** (restart applied).

> If you can't add a local folder as a repository, fork/publish it to GitHub and
> add that URL instead — any add-on repository works.

## Configure the Bird Card

In the card's visual editor (**Connection & data**) there are two working
options. **Option A is recommended.**

### A. Via Home Assistant ingress (recommended)

Leave **BirdNET-Go URL** (`birdnet_url`) **blank**. The card then connects
through Home Assistant ingress — which works whether you are on your home LAN or
remotely via Nabu Casa, with no port exposure required.

That's all — the card will pull its collage species, counts, hourly buckets,
recordings, and audio from BirdNET-PiPy through the adapter.

### B. Via the raw LAN port

Set **BirdNET-Go URL** (`birdnet_url`) to `http://<HA-HOST-IP>:8099`
(the port mapped in `config.json` — `8080/tcp: 8099`; e.g.
`http://192.168.86.50:8099`). This only works while you are on the home LAN,
because the browser reaches the private port directly; it will not work over
Nabu Casa.

### Weather
The card reads Home Assistant's weather integration directly when a
`weather.*` entity exists, so no adapter endpoint is needed. If you have no
weather entity, weather falls back to BirdNET-Go's built-in service, which this
setup doesn't provide — so weather will be off until you either add a HA
weather entity or skip it.

## Settings reference (add-on Settings)

| Key | Default | Meaning |
|---|---|---|
| `birdnet_host` | `192.168.86.30` | BirdNET-PiPy host on your LAN |
| `birdnet_port` | `80` | BirdNET-PiPy HTTP port |
| `cache_ttl` | `30` | Seconds aggregates are cached (card polls ~30s) |
| `http_timeout` | `15` | Per BirdNET-PiPy request timeout (s) |
| `max_workers` | `16` | Parallelism for per-species summary fetches |
| `log_level` | `INFO` | Logging verbosity |

## How it maps (endpoints)

| Card asks | Adapter serves, from BirdNET-PiPy |
|---|---|
| `/api/v2/analytics/species/summary` | `/api/species/all` + per-species count/first/last/max-confidence |
| `/api/v2/analytics/species/daily` | `/api/activity/overview` + top-confidence page |
| `/api/v2/detections` (recordings) | `/api/detections?species=&sort=timestamp` |
| `/api/v2/audio/<id>` | proxied `/api/audio/<filename>` (with signed query) |
| `/api/v2/analytics/time/daily` | per-day `/api/detections` counts |
| `/api/v2/analytics/species/diversity` | `/api/sightings/unique` per day |
| `/api/v2/analytics/time/distribution/hourly` | summed `/api/activity/hourly` |
| `/api/v2/detections/stream` | polls latest detection (SSE refetch) |
| `/api/v2/weather/latest` | latest detection's stored weather (best-effort) |

## Notes & limitations

- **Ingress is the default path.** The card reaches the add-on via Home Assistant
  ingress when `birdnet_url` is left blank, so it works on your home LAN and
  remotely (Nubu Casa) with nothing exposed. A raw `http://<host>:8099` URL works
  too, but only on the home LAN. The adapter serves cleanly under ingress, and
  sends CORS headers so the LAN-port path works as well.
- **Counts.** The ALL/life-list count comes from BirdNET-PiPy per-species
  totals; 7-day counts are detections within that window.
- **Cached.** Aggregates are cached for `cache_ttl` seconds (matching the card's
  ~30s poll), so the per-species summary (a few hundred quick LAN calls) runs
  at most once or twice a minute.
- **`/api/bird/<name>`** is resolved by **common name**, so species with a
  distinct display name (e.g. "Western Barn Owl" vs "Barn Owl") are handled via
  the common name from `/api/species/all`.
