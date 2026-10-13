#!/usr/bin/env python3
"""
BirdNET-PiPy -> Bird Card (HABirdDashboard) adapter.

The HABirdDashboard "Bird Card" expects a BirdNET-Go API v2 backend. This
service speaks that shape on top of BirdNET-PiPy, so the card can be fed by
BirdNET-PiPy instead of BirdNET-Go. It runs as a Home Assistant add-on on the
HA host and talks to BirdNET-PiPy over the LAN.

Data-flow:  browser -> card -> this add-on (birdnet_url) -> BirdNET-PiPy (LAN)

See ../docs/README.md for install + card configuration.
"""
import os
import re
import time
import threading
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

import urllib3
import requests
from flask import Flask, request, Response, jsonify, stream_with_context

# --------------------------------------------------------------------------- #
# Config. Home Assistant add-on options are exposed as uppercase env vars,
# e.g. birdnet_host -> BIRDNET_HOST. Defaults also let this run standalone.
# --------------------------------------------------------------------------- #
BIRDNET_HOST = os.environ.get("BIRDNET_HOST", "192.168.86.30")
BIRDNET_PORT = os.environ.get("BIRDNET_PORT", "80")
BIRDNET_BASE = f"http://{BIRDNET_HOST}:{BIRDNET_PORT}".rstrip("/")
CACHE_TTL = float(os.environ.get("CACHE_TTL", "30"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "15"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("birdnet-pipy-adapter")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# BirdNET-PiPy's backend is effectively serial: concurrent connections degrade
# sharply (tested -- even 2 workers start failing). The adapter is not
# latency-sensitive (the card caches 30-60s), so all upstream traffic is
# serialized through one shared session + lock, with retries.
_UPSTREAM_LOCK = threading.RLock()
_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})
_RETRIES = urllib3.util.retry.Retry(
    total=6, backoff_factor=0.5, status_forcelist=(429, 502, 503, 504),
    allowed_methods=frozenset(["GET"]),
)
_SESSION.mount("http://", requests.adapters.HTTPAdapter(max_retries=_RETRIES))


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
_cache = {}
_cache_lock = threading.Lock()


def cached(key, builder, ttl=None):
    """Minimal single-TTL in-memory cache shared across threads."""
    ttl = ttl if ttl is not None else CACHE_TTL
    now = time.time()
    with _cache_lock:
        exp, val = _cache.get(key, (0.0, None))
        if exp > now and val is not None:
            return val
    val = builder()
    with _cache_lock:
        _cache[key] = (now + ttl, val)
    return val


def bnp_get(path, params=None):
    """GET JSON from BirdNET-PiPy (serialized, retried)."""
    with _UPSTREAM_LOCK:
        r = _SESSION.get(BIRDNET_BASE + path, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()


def bnp_get_raw(path, params=None):
    """GET bytes from BirdNET-PiPy (audio/spectrogram) (serialized, retried)."""
    with _UPSTREAM_LOCK:
        r = _SESSION.get(BIRDNET_BASE + path, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r


def _q(name, default=None):
    v = request.args.get(name)
    return v if v not in (None, "") else default


@app.after_request
def _add_cors(resp):
    # The card is reached same-origin through HA ingress normally; these make
    # the LAN-port path (cross-origin) safe too.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok", "source": BIRDNET_BASE})


# --------------------------------------------------------------------------- #
# Audio proxying. The card builds /api/v2/audio/<id> from each detection's id,
# so we remember the BirdNET-PiPy filename (+ signed query) per id and stream
# the bytes back (proxying avoids CORS on the Pi's audio endpoint).
# --------------------------------------------------------------------------- #
_id_media = {}
_id_lock = threading.Lock()


def _store_media(detections):
    with _id_lock:
        for d in detections:
            fn = d.get("audio_filename")
            if fn:
                _id_media[int(d["id"])] = (fn, d.get("audio_sig"))


def _lookup_media(det_id):
    with _id_lock:
        hit = _id_media.get(det_id)
    if hit:
        return hit
    # Cold cache: scan a recent page for the id.
    try:
        page = bnp_get("/api/detections", params={
            "page": 1, "per_page": 200, "sort": "timestamp", "order": "desc"})
    except Exception as e:
        log.warning("recent-page audio lookup failed: %s", e)
        return None
    for d in page.get("detections", []):
        if int(d.get("id", -1)) == det_id and d.get("audio_filename"):
            media = (d["audio_filename"], d.get("audio_sig"))
            with _id_lock:
                _id_media[det_id] = media
            return media
    return None


def _sig_params(sig):
    if not sig:
        return None
    parts = dict(p.split("=", 1) for p in sig.split("&") if "=" in p)
    if "exp" in parts and "sig" in parts:
        return {"exp": parts["exp"], "sig": parts["sig"]}
    return None


# --------------------------------------------------------------------------- #
# Analytics: species summary  ->  the life list / ALL window / 7D window.
# Card expects: [{scientific_name, common_name, count, first_heard,
#                 last_heard, max_confidence}]
# --------------------------------------------------------------------------- #
@app.route("/api/v2/analytics/species/summary")
def analytics_species_summary():
    start, end = _q("start_date"), _q("end_date")
    has_window = bool(start and end)

    def build():
        species = bnp_get("/api/species/all")

        # Best confidence per species. For the ALL window a single global
        # top-confidence page covers every species whose best detection
        # clears the perch threshold; for a bounded window it is fetched per
        # species (windowed).
        max_conf = {}
        if not has_window:
            try:
                for d in bnp_get("/api/detections", params={
                        "sort": "confidence", "order": "desc", "per_page": 300}
                ).get("detections", []):
                    sci = d.get("scientific_name")
                    if sci and sci not in max_conf:
                        max_conf[sci] = d.get("confidence") or 0.0
            except Exception:
                log.exception("max-confidence page failed")

        rows = []
        for s in species:
            sci = s.get("scientific_name") or s.get("common_name")
            com = s.get("common_name")               # filter key
            disp = s.get("display_common_name") or com   # shown name
            count, last = 0, None
            try:
                p = {"species": com, "per_page": 1,
                     "sort": "timestamp", "order": "desc"}
                if start:
                    p["start_date"] = start
                if end:
                    p["end_date"] = end
                det = bnp_get("/api/detections", params=p)
                count = det.get("pagination", {}).get("total_items", 0)
                if det.get("detections"):
                    last = det["detections"][0].get("timestamp")
            except Exception:
                log.warning("summary count %s failed", com)
            if has_window and sci not in max_conf:
                try:
                    conf = bnp_get("/api/detections", params={**p,
                        "sort": "confidence", "order": "desc", "per_page": 1})
                    max_conf[sci] = (conf.get("detections") or [{}])[0].get("confidence") or 0.0
                except Exception:
                    log.warning("summary max_conf %s failed", sci)
            rows.append({"scientific_name": sci, "common_name": disp,
                         "count": count, "first_heard": None,
                         "last_heard": last,
                         "max_confidence": max_conf.get(sci, 0.0)})
        # most-heard first; tie-break on most-recently-heard.
        rows.sort(key=lambda r: (r["count"], r["last_heard"] or ""), reverse=True)
        return rows

    try:
        return jsonify(cached(f"summary:{start}:{end}", build, ttl=60))
    except Exception:
        log.exception("species/summary failed")
        return jsonify([])


# --------------------------------------------------------------------------- #
# Analytics: daily per-species  ->  1H/12H/24H windows (card buckets hourly).
# Card expects: [{scientific_name, common_name, count, hourly_counts[24],
#                 max_confidence}]
# --------------------------------------------------------------------------- #
@app.route("/api/v2/analytics/species/daily")
def analytics_species_daily():
    date = _q("date") or datetime.now().strftime("%Y-%m-%d")

    def build():
        overview = bnp_get("/api/activity/overview",
                           params={"date": date, "order": "most"})
        # best confidence per species from the day's top detections.
        top = bnp_get("/api/detections", params={
            "sort": "confidence", "order": "desc", "per_page": 100,
            "start_date": date, "end_date": date})
        max_conf = {}
        for d in top.get("detections", []):
            sci = d.get("scientific_name")
            if sci and sci not in max_conf:
                max_conf[sci] = d.get("confidence") or 0.0
        rows = []
        for item in overview:
            sci = item.get("scientific_name")
            hourly = item.get("hourlyActivity") or [0] * 24
            rows.append({
                "scientific_name": sci,
                "common_name": item.get("species") or item.get("displaySpecies"),
                "count": item.get("totalObservations", 0),
                "hourly_counts": hourly,
                "max_confidence": max_conf.get(sci, 0.0),
            })
        rows.sort(key=lambda r: -r["count"])
        return rows

    try:
        return jsonify(cached(f"daily:{date}", build, ttl=15))
    except Exception:
        log.exception("species/daily failed")
        return jsonify([])


# --------------------------------------------------------------------------- #
# Detections (per-species recordings list).
# Card expects: {data:[{id, date, time, scientificName, commonName,
#                     confidence}], total}
# --------------------------------------------------------------------------- #
@app.route("/api/v2/detections")
def v2_detections():
    search = _q("search", "")
    num = min(max(int(_q("numResults", "100") or 100), 1), 500)
    start, end = _q("start_date"), _q("end_date")

    params = {"sort": "timestamp", "order": "desc", "per_page": num}
    if search:
        params["species"] = search
    if start:
        params["start_date"] = start
    if end:
        params["end_date"] = end

    try:
        det = bnp_get("/api/detections", params=params)
        rows = det.get("detections", [])
        _store_media(rows)
        data = [{
            "id": d.get("id"),
            "date": (d.get("timestamp") or "")[:10],
            "time": (d.get("timestamp") or "")[11:19],
            "scientificName": d.get("scientific_name"),
            "commonName": d.get("display_common_name") or d.get("common_name"),
            "confidence": d.get("confidence"),
        } for d in rows]
        return jsonify({"data": data,
                        "total": det.get("pagination", {}).get("total_items", len(data))})
    except Exception:
        log.exception("detections failed")
        return jsonify({"data": [], "total": 0})


# --------------------------------------------------------------------------- #
# Audio playback (proxied from BirdNET-PiPy).
# --------------------------------------------------------------------------- #
@app.route("/api/v2/audio/<int:det_id>")
def v2_audio(det_id):
    media = _lookup_media(det_id)
    if not media:
        return jsonify({"error": "audio not found"}), 404
    filename, sig = media
    try:
        r = bnp_get_raw("/api/audio/" + quote(filename), params=_sig_params(sig))
    except Exception:
        log.exception("audio fetch failed for %s", filename)
        return jsonify({"error": "audio fetch failed"}), 502
    ct = r.headers.get("Content-Type", "audio/mpeg")
    return Response(r.content, status=200, content_type=ct)


# --------------------------------------------------------------------------- #
# Weather. The card reads Home Assistant's weather integration directly when a
# weather.* entity exists and only falls back here otherwise. Best-effort from
# the latest detection's stored weather; empty if unavailable.
# --------------------------------------------------------------------------- #
@app.route("/api/v2/weather/latest")
def v2_weather():
    try:
        det = bnp_get("/api/detections", params={
            "per_page": 1, "sort": "timestamp", "order": "desc"})
        w = (det.get("detections", [{}])[0].get("extra") or {}).get("weather")
        if not w:
            return jsonify({})
        return jsonify({
            "condition": w.get("code"),
            "description": w.get("code"),
            "temperature": w.get("temp"),
            "humidity": w.get("humidity"),
            "wind_speed": w.get("wind"),
            "pressure": w.get("pressure"),
        })
    except Exception:
        return jsonify({})


# --------------------------------------------------------------------------- #
# Stats-view endpoints (card lazy-loads these; degrade to empty gracefully).
# --------------------------------------------------------------------------- #
def _date_range(start, end):
    if not start:
        start = (datetime.now() - timedelta(days=13)).strftime("%Y-%m-%d")
    if not end:
        end = datetime.now().strftime("%Y-%m-%d")
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    out, cur, guard = [], s, 0
    while cur <= e and guard < 400:
        out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
        guard += 1
    return out


@app.route("/api/v2/analytics/time/daily")
def analytics_time_daily():
    start, end = _q("start_date"), _q("end_date")

    def build():
        data, total = [], 0
        for d in _date_range(start, end):
            try:
                n = bnp_get("/api/detections",
                            params={"per_page": 1, "start_date": d,
                                    "end_date": d}).get("pagination", {}).get("total_items", 0)
            except Exception:
                n = 0
            total += n
            data.append({"date": d, "count": n})
        return {"data": data, "total": total}

    try:
        return jsonify(cached(f"timeDaily:{start}:{end}", build))
    except Exception:
        log.exception("time/daily failed")
        return jsonify({"data": [], "total": 0})


@app.route("/api/v2/analytics/species/diversity")
def analytics_species_diversity():
    start, end = _q("start_date"), _q("end_date")

    def build():
        data, maxd = [], 0
        for d in _date_range(start, end):
            try:
                u = bnp_get("/api/sightings/unique", params={"date": d})
                n = len(u) if isinstance(u, list) else 0
            except Exception:
                n = 0
            maxd = max(maxd, n)
            data.append({"date": d, "unique_species": n})
        return {"data": data, "max_diversity": maxd}

    try:
        return jsonify(cached(f"diversity:{start}:{end}", build))
    except Exception:
        log.exception("species/diversity failed")
        return jsonify({"data": [], "max_diversity": 0})


@app.route("/api/v2/analytics/time/distribution/hourly")
def analytics_distribution_hourly():
    start, end = _q("start_date"), _q("end_date")

    def build():
        buckets = [0] * 24
        for d in _date_range(start, end):
            try:
                for h in bnp_get("/api/activity/hourly", params={"date": d}):
                    hh = int(str(h.get("hour", "0:00")).split(":")[0])
                    buckets[hh] += h.get("count", 0)
            except Exception:
                pass
        return [{"hour": h, "count": buckets[h]} for h in range(24)]

    try:
        return jsonify(cached(f"distHourly:{start}:{end}", build))
    except Exception:
        log.exception("distribution/hourly failed")
        return jsonify([{"hour": h, "count": 0} for h in range(24)])


# --------------------------------------------------------------------------- #
# Live stream (SSE). The card treats each 'detection' event as a "refetch"
# signal. Optional; the card also works with live: false.
# --------------------------------------------------------------------------- #
@app.route("/api/v2/detections/stream")
def v2_stream():
    interval = int(os.environ.get("STREAM_INTERVAL", "5"))

    def generate():
        last_id = None
        while True:
            try:
                det = bnp_get("/api/detections", params={
                    "per_page": 1, "sort": "timestamp", "order": "desc"})
                cur = det.get("detections", [{}])[0].get("id")
                if cur != last_id:
                    last_id = cur
                    yield "event: detection\n\n"
            except Exception:
                pass
            time.sleep(interval)

    return stream_with_context(generate()), {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    }


def main():
    log.info("BirdNET-PiPy adapter up -> %s", BIRDNET_BASE)
    app.run(host="0.0.0.0", port=8080, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
