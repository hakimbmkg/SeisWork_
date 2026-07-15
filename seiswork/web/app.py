"""
SeisWork Web GUI - by HakimBMKG

Routes:
  GET  /                          - home page (Leaflet map + config panel)
  GET  /api/configs               - list all saved configs
  POST /api/configs               - save new config  {name, region, stations, fdsn_url}
  GET  /api/configs/<id>          - load a config by ID
  DEL  /api/configs/<id>          - delete config
  POST /api/stations/upload       - parse uploaded station CSV/TXT
  GET  /api/stations/fdsn?url=..  - fetch & parse FDSN station response
  GET  /api/base-config           - read region defaults from config/config.yaml
  GET  /api/footages              - list available GeoJSON overlay files
  GET  /footages/<filename>       - serve GeoJSON file from web/footages/
"""

import csv
import base64
import io
import json
import math
import os
import re
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path

import requests
import yaml
from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

from seiswork.web._seedlink_live import _LIVE_SESSION
from seiswork.web._seiscomp_detect import detect_seiscomp
from seiswork.web._online_stations import (get_stations, get_stations_multi,
                                            find_default_inventory)
from seiswork.web._realtime_pipeline import get_realtime_picker, get_realtime_associator
from seiswork.utils.channels import read_best_waveform, channel_search_order, BAND_PRIORITY

# ── Paths ──────────────────────────────────────────────────────────────────────
# app.py lives at seiswork/web/app.py, parent x3 = project root
BASE_DIR     = Path(__file__).resolve().parent.parent.parent
WORK_DIR     = BASE_DIR / "work"
CFG_FILE     = BASE_DIR / "config" / "config.yaml"
CONFIGS_DIR  = WORK_DIR / "gui_configs"
FOOTAGES_DIR = Path(__file__).resolve().parent / "footages"
DEM_DIR      = FOOTAGES_DIR / "dem"
DEM_DIR.mkdir(exist_ok=True)
TMP_DIR      = Path(__file__).resolve().parent / "tmp"
TMP_DIR.mkdir(exist_ok=True)
(TMP_DIR / "jobs").mkdir(exist_ok=True)
(TMP_DIR / "ql_jobs").mkdir(exist_ok=True)

# In-memory job registry keyed by job_id
_jobs: dict[str, dict] = {}
_job_threads: dict[str, threading.Thread] = {}

# QuakeLink / FDSN-Event catalog download jobs
_ql_jobs: dict[str, dict] = {}

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # 256 MB (full StationXML w/ response can be tens of MB)
app.config["JSON_SORT_KEYS"] = False

# Trust X-Forwarded-* only when we're actually behind a reverse proxy (nginx
# etc.) — opt-in via SEISWORK_TRUST_PROXY, never on by default. Several
# endpoints (server-info, server-info/token, server/restart) gate on
# `request.remote_addr == 127.0.0.1` to detect "operator on this machine";
# without ProxyFix, a naive reverse proxy makes THAT check true for every
# remote client (the proxy's own loopback hop), leaking the bearer token /
# allowing a remote restart to anyone. Set this env var only once the proxy
# is confirmed to forward X-Forwarded-For/Proto/Host correctly.
if os.environ.get("SEISWORK_TRUST_PROXY", "").strip().lower() in ("1", "true", "yes"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# Ensure uploads exceeding the limit return JSON (not HTML 413),
# so the frontend can show a clear message instead of "Invalid response".
from werkzeug.exceptions import RequestEntityTooLarge


@app.errorhandler(RequestEntityTooLarge)
def _handle_too_large(e):
    limit_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return jsonify({"error": f"File terlalu besar — maksimal {limit_mb} MB"}), 413

# gzip all responses (large JSON + JS 200KB+); important for slow LAN
try:
    from flask_compress import Compress
    app.config["COMPRESS_MIMETYPES"] = [
        "text/html", "text/css", "application/json",
        "application/javascript", "text/javascript", "text/csv",
    ]
    app.config["COMPRESS_STREAMS"] = True   # also compress static files (send_file)
    Compress(app)
except ImportError:
    pass  # optional; server still works without compression


# ── Federation: server identity, bearer-token auth, CORS, health ────────────────
# SeisWork runs standalone by default, or as a server that remote clients (CLI
# or Web GUI mirror) connect to. Each server has a stable server_id so job/config
# UUIDs sync across client and server. Auth is an optional bearer token
# (SEISWORK_TOKEN); empty token disables auth (LAN use).
import secrets
import socket

SEISWORK_HOME = Path(os.environ.get("SEISWORK_HOME", Path.home() / ".seiswork"))
SEISWORK_HOME.mkdir(parents=True, exist_ok=True)
_SERVER_ID_FILE = SEISWORK_HOME / "server_id"


def _load_server_id() -> str:
    try:
        if _SERVER_ID_FILE.exists():
            sid = _SERVER_ID_FILE.read_text().strip()
            if sid:
                return sid
    except Exception:
        pass
    sid = uuid.uuid4().hex
    try:
        _SERVER_ID_FILE.write_text(sid)
    except Exception:
        pass
    return sid


def _server_version() -> str:
    try:
        from seiswork import __version__ as v
        return v
    except Exception:
        return "unknown"


SERVER_ID      = _load_server_id()
SERVER_VERSION = _server_version()
# Token precedence: env var, then runtime token file (~/.seiswork/token, set from
# the GUI and survives restarts), then config federation.token; empty = auth off.
_TOKEN_FILE = SEISWORK_HOME / "token"
AUTH_TOKEN = (os.environ.get("SEISWORK_TOKEN") or "").strip()
if not AUTH_TOKEN:
    try:
        if _TOKEN_FILE.exists():
            AUTH_TOKEN = _TOKEN_FILE.read_text().strip()
    except Exception:
        AUTH_TOKEN = ""
if not AUTH_TOKEN:
    try:
        AUTH_TOKEN = str((_load_base_config().get("federation", {}) or {})
                         .get("token", "") or "").strip()
    except Exception:
        AUTH_TOKEN = ""

# /api paths that never require a token (discovery + CORS preflight + livereload).
_PUBLIC_API = {"/api/health", "/api/livereload", "/api/server-info",
               "/api/server-info/token", "/api/remote/connect",
               "/api/remote/disconnect", "/api/remote/status"}

# Cross-origin browser access is opt-in only (comma-separated origins, e.g.
# "https://gui.example.org"). Empty by default — see _cors_headers().
_CORS_ALLOWED_ORIGINS = {o.strip() for o in
                         os.environ.get("SEISWORK_CORS_ORIGINS", "").split(",")
                         if o.strip()}


# ── Online Viewer (read-only mirror) ───────────────────────────────────────────
# A second, identical GUI can run on a separate port (default 3346) that mirrors
# the main monitor read-only: same template, but every /api/online/* read is
# proxied to the upstream main server (default 127.0.0.1:5000) instead of this
# process' own empty _LIVE_SESSION. Mutating calls (connect/disconnect, realtime
# start/stop, inject, config edits, ...) are intercepted locally and never
# forwarded, so the mirror can never alter the upstream. Auto-spawned by the main
# server's run(); self-restarts on code edits via _watch_py_and_restart.
VIEWER_MODE     = os.environ.get("SEISWORK_VIEWER_MODE") == "1"
VIEWER_PORT     = int(os.environ.get("SEISWORK_VIEWER_PORT", "3346"))
VIEWER_UPSTREAM = os.environ.get("SEISWORK_VIEWER_UPSTREAM",
                                 "http://127.0.0.1:5000").rstrip("/")

# /api/online/* POSTs that are read-only (carry a cursor/query, return data) and
# therefore *safe* to forward upstream from the mirror.
_VIEWER_PROXY_POST = {"/api/online/waveform/delta"}
# POSTs whose state the mirror fakes from the upstream's *current* status instead
# of forwarding (forwarding would start a duplicate/real session on the upstream).
_VIEWER_FAKE_OK = {
    "/api/online/connect":         "/api/online/status",
    "/api/online/realtime/start":  "/api/online/realtime/status",
}


def _viewer_intercept():
    """In viewer mode, serve /api/online/* from the upstream server and block
    anything that would mutate it. Returns a response to short-circuit the
    request, or None to let normal local handling proceed."""
    if not VIEWER_MODE:
        return None
    p = request.path
    if not p.startswith("/api/online/"):
        return None
    # Reads: transparent proxy to the upstream main monitor.
    if request.method == "GET" or p in _VIEWER_PROXY_POST:
        return _viewer_proxy(p)
    # connect / realtime-start: don't forward, just report the upstream's live
    # state so the frontend flow lights up the dashboard against the mirror.
    if p in _VIEWER_FAKE_OK and request.method == "POST":
        try:
            up = requests.get(VIEWER_UPSTREAM + _VIEWER_FAKE_OK[p],
                              timeout=8, headers=_viewer_fwd_headers())
            data = up.json() if up.ok else {}
        except Exception:
            data = {}
        data["viewer"] = True
        data.setdefault("ok", True)
        data.setdefault("started", True)
        data.setdefault("running", True)
        return jsonify(data)
    # disconnect / realtime-stop / inject / config edits / uploads / etc: local
    # no-op so the read-only mirror never disturbs the upstream monitor.
    return jsonify({"viewer": True, "ok": True, "read_only": True})


def _viewer_fwd_headers() -> dict:
    """Forward the caller's bearer token (falling back to this server's own) so
    upstream auth, if enabled, still passes through the mirror."""
    h = {}
    tok = _request_token() or AUTH_TOKEN
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def _viewer_proxy(path: str):
    """Proxy one /api/online/* read to the upstream main server, preserving
    method, query string, body and content-type. SSE streams are forwarded as
    streaming responses so the viewer gets real-time push without buffering."""
    url = VIEWER_UPSTREAM + path
    if request.query_string:
        url += "?" + request.query_string.decode("utf-8", "ignore")
    headers = _viewer_fwd_headers()
    if request.content_type:
        headers["Content-Type"] = request.content_type

    # SSE endpoint: stream-proxy as chunked transfer so the viewer gets push
    # events without the 30s timeout killing the connection.
    if path == "/api/online/waveform/stream":
        try:
            up = requests.request(request.method, url, headers=headers,
                                  stream=True, timeout=None)
        except Exception as e:
            return jsonify({"error": "viewer upstream unreachable",
                            "detail": str(e), "upstream": VIEWER_UPSTREAM}), 502

        def _iter():
            try:
                for chunk in up.iter_content(chunk_size=None):
                    yield chunk
            finally:
                up.close()

        resp = Response(stream_with_context(_iter()),
                        status=up.status_code,
                        mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Connection"] = "keep-alive"
        return resp

    try:
        up = requests.request(request.method, url,
                              data=request.get_data() if request.method != "GET" else None,
                              headers=headers, timeout=30)
    except Exception as e:
        return jsonify({"error": "viewer upstream unreachable",
                        "detail": str(e), "upstream": VIEWER_UPSTREAM}), 502
    return Response(up.content, status=up.status_code,
                    content_type=up.headers.get("Content-Type",
                                                "application/json"))


def _request_token() -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Seiswork-Token", "")
            or request.args.get("token", "")).strip()


@app.before_request
def _enforce_auth():
    if request.method == "OPTIONS":
        return ("", 204)                       # CORS preflight, no auth
    # Online Viewer mirror: serve /api/online/* from the upstream main server.
    _v = _viewer_intercept()
    if _v is not None:
        return _v
    p = request.path
    if not p.startswith("/api/") or p in _PUBLIC_API:
        return None                            # UI/static/health are open
    if p.startswith("/api/sync/"):
        return None                            # these enforce their own per-cfg_id sync token
    if not AUTH_TOKEN:
        return None                            # auth disabled (standalone/LAN)
    if not secrets.compare_digest(_request_token(), AUTH_TOKEN):
        return jsonify({"error": "unauthorized",
                        "detail": "missing or invalid bearer token"}), 401
    return None


# ── Per-project federation tokens ───────────────────────────────────────────
# AUTH_TOKEN above is server-wide and guards the operator-facing API. Federation
# sync (/api/sync/*) is how a subscriber pulls one project's data, so it must
# not accept that same server-wide token, otherwise any subscriber with access
# to one project could read every project on this server. Each cfg_id gets its
# own token, generated on first use and persisted next to its meta.json.
def _cfg_sync_token(cfg_id: str) -> str:
    tok_file = CONFIGS_DIR / cfg_id / ".sync_token"
    if tok_file.is_file():
        existing = tok_file.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(18)
    tok_file.parent.mkdir(parents=True, exist_ok=True)
    tok_file.write_text(token)
    return token


def _check_sync_token(cfg_id: str) -> bool:
    return secrets.compare_digest(_request_token(), _cfg_sync_token(cfg_id))


@app.after_request
def _cors_headers(resp):
    # No page in this app's own frontend needs cross-origin access — every
    # fetch() in static/js/** targets location.origin, and non-browser
    # clients (curl, the federation Python client) are never subject to CORS
    # in the first place. So instead of a blanket "*" (which lets ANY web
    # page's script read this server's public/no-token endpoints, e.g.
    # /api/server-info), only echo back an Origin that's been explicitly
    # allowlisted via SEISWORK_CORS_ORIGINS (comma-separated). Unset = no
    # cross-origin browser access, which matches what's actually used today.
    origin = request.headers.get("Origin", "")
    if origin and origin in _CORS_ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, X-Seiswork-Token")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    # Baseline security headers — additive, don't change any existing
    # behavior. SAMEORIGIN (not DENY) because tomography.js embeds a
    # same-origin Plotly HTML file in an <iframe>.
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.is_secure:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # The HTML shell must stay fresh (carries ?v= cache-bust tokens), but assets
    # are cacheable: app files bump their ?v= token when edited, and vendor files
    # are immutable, so caching them keeps normal reloads fast (esp. Plotly).
    if request.path.startswith("/static/"):
        # Versioned assets (?v=<mtime>) can cache long; the token flips on change.
        # Un-versioned assets (favicons, native-bridge.js, plain images) must
        # always revalidate, or an edit gets masked for days by the disk cache.
        if request.args.get("v"):
            resp.headers["Cache-Control"] = "public, max-age=604800"   # 7 days
        else:
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    elif "text/html" in resp.headers.get("Content-Type", ""):
        # Every rendered page (main shell and full-page views like
        # waveform-fullpage, station-map, catalog-map, pipeline-flow) must be
        # fresh: they carry ?v= tokens and inline theme CSS, and a cached copy
        # (esp. in WKWebView) can mask edits or show a stale stylesheet.
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"]  = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ── Offline-friendly map tile cache proxy ─────────────────────────────────────
# Every Leaflet basemap routes through /tiles/<provider>/z/x/y.png so that:
#   1. tiles are cached to ~/.seiswork/tiles on first fetch (real offline cache);
#   2. when offline, or the upstream 404s/times out, we return a transparent
#      placeholder with HTTP 200 instead of a broken tile.
# The provider table lives server-side, which also hides axis-order quirks
# (Esri serves {z}/{y}/{x}); the frontend always passes plain {z}/{x}/{y}.
import urllib.request as _urlreq
import urllib.error as _urlerr

_TILE_CACHE_DIR = SEISWORK_HOME / "tiles"
_TILE_PROVIDERS = {
    "carto_dark":   "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
    "osm":          "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
    "opentopomap":  "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
    "esri_ocean":   "https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}",
    "esri_imagery": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
}
# 1x1 fully-transparent PNG; the browser scales it to the 256px tile slot, so an
# unavailable tile just shows the map container background (no broken image).
_PLACEHOLDER_TILE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _placeholder_tile_response():
    return Response(_PLACEHOLDER_TILE, mimetype="image/png",
                    headers={"Cache-Control": "no-store"})


@app.route("/tiles/<provider>/<int:z>/<int:x>/<int:y>.png")
def map_tile(provider, z, x, y):
    tmpl = _TILE_PROVIDERS.get(provider)
    if not tmpl:
        return Response("unknown tile provider", status=404)
    # Reject absurd coordinates (defensive; keeps the cache tree bounded).
    if not (0 <= z <= 22 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
        return _placeholder_tile_response()

    cache_path = _TILE_CACHE_DIR / provider / str(z) / str(x) / f"{y}.png"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return send_file(cache_path, mimetype="image/png", max_age=604800)

    url = tmpl.format(z=z, x=x, y=y)
    try:
        req = _urlreq.Request(url, headers={"User-Agent": "SeisWork/tile-cache"})
        with _urlreq.urlopen(req, timeout=5) as r:
            data = r.read()
        if data:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(cache_path)   # atomic, no half-written tiles served
            return Response(data, mimetype="image/png",
                            headers={"Cache-Control": "public, max-age=604800"})
    except Exception:
        pass
    # Offline or upstream failure: clean placeholder, HTTP 200 (no 404 noise).
    return _placeholder_tile_response()


@app.route("/help", methods=["GET"])
def help_docs():
    """Full function/API reference (docs/generate_full_doc.py output), opened
    from the Help menu. English by default; ?lang=id serves the Indonesian
    build. The file is gitignored (a local build artifact), so a fresh
    checkout may not have it yet; that path explains how to build it."""
    lang = request.args.get("lang", "en")
    docs_html = (_DOCS_HTML.parent / "SeisWork_Dokumentasi_Lengkap.id.html"
                 if lang == "id" else _DOCS_HTML)
    if docs_html.exists():
        return send_file(str(docs_html), mimetype="text/html")
    return Response(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<title>SeisWork — Help</title></head><body "
        "style='font-family:system-ui,sans-serif;max-width:640px;margin:3rem auto;"
        "line-height:1.6;color:#1e293b'>"
        "<h2>Documentation not built yet</h2>"
        "<p>The full function/API reference is generated on demand and isn't "
        "checked into git. Build it once from the repo root:</p>"
        "<pre style='background:#f0f4f8;padding:.8rem 1rem;border-radius:6px'>"
        "python docs/generate_full_doc.py</pre>"
        "<p>Then reopen this Help page.</p></body></html>",
        mimetype="text/html", status=404,
    )


@app.route("/api/changelog", methods=["GET"])
def api_changelog():
    """Version history for the native GUI's Changelog menu / web Help panel."""
    try:
        return jsonify(json.loads(_CHANGELOG_JSON.read_text()))
    except Exception:
        return jsonify({"current": SERVER_VERSION, "entries": []})


@app.route("/api/health", methods=["GET"])
def api_health():
    """Public discovery endpoint: lets a client verify connectivity, learn the
    server identity, and check whether a token is required before sending one."""
    return jsonify({
        "ok"           : True,
        "service"      : "seiswork",
        "server_id"    : SERVER_ID,
        "version"      : SERVER_VERSION,
        "hostname"     : socket.gethostname(),
        "auth_required": bool(AUTH_TOKEN),
        "time"         : datetime.now().isoformat(timespec="seconds"),
    })


@app.route("/api/server/restart", methods=["POST"])
def api_server_restart():
    """Restart this server in place (os.execv, same port). Lets an operator
    recover a wedged server from the GUI without SSH. Allowed only from
    localhost or with the current token. Refuses while a job is running unless
    force=true, so a restart never silently kills a running pipeline. The reply
    is sent first; re-exec fires ~0.6s later (frontend polls /api/health, then
    reloads). The frontend call targets location.origin, so it always restarts
    the local server, never a connected remote."""
    remote   = (request.remote_addr or "")
    is_local = remote in ("127.0.0.1", "::1") or remote.startswith("127.")
    has_tok  = bool(AUTH_TOKEN) and secrets.compare_digest(_request_token(), AUTH_TOKEN)
    if not (is_local or has_tok or not AUTH_TOKEN):
        return jsonify({"error": "only from the server machine or with the current token"}), 403
    data  = request.get_json(silent=True) or {}
    force = bool(data.get("force")) or request.args.get("force") == "1"
    if _server_busy() and not force:
        return jsonify({"busy": True,
                        "error": "a job is running — pass force=true to restart anyway"}), 409

    def _later():
        import time as _t
        _t.sleep(0.6)
        print("[restart] manual restart requested via API — re-exec.", flush=True)
        try:
            _reexec_server()
        except Exception as e:
            print(f"[restart] re-exec failed: {e}", flush=True)
    threading.Thread(target=_later, daemon=True).start()
    return jsonify({"ok": True, "restarting": True})


# ── Online Project - SeisComP auto-detect + live seedlink ──────────────────────
# Handles detection+connection+live waveform. Realtime detection (PhaseNet,
# REAL, ML magnitude, focal mechanism) lives in the endpoints further below.

@app.route("/api/online/detect", methods=["GET"])
def api_online_detect():
    result = detect_seiscomp(CFG_FILE)
    # Include the default inventory that was found
    result["default_inventory"] = find_default_inventory()
    return jsonify(result)


@app.route("/api/online/stations", methods=["GET"])
def api_online_stations():
    """Parse a StationXML inventory into a station list [{net,sta,lat,lon,name,channels}]."""
    inventory = (request.args.get("inventory") or "").strip()
    if not inventory:
        inventory = find_default_inventory()
    # Multiple XMLs may be passed, separated by ',' or ';' (multi-seedlink)
    inv_list = [p.strip() for p in inventory.replace(";", ",").split(",") if p.strip()]
    if not inv_list or not any(Path(p).exists() for p in inv_list):
        return jsonify({"error": "inventory not found", "stations": [], "inventory": inventory}), 404
    lat_min = request.args.get("lat_min", type=float)
    lat_max = request.args.get("lat_max", type=float)
    lon_min = request.args.get("lon_min", type=float)
    lon_max = request.args.get("lon_max", type=float)
    try:
        stations = get_stations_multi(inv_list, lat_min, lat_max, lon_min, lon_max)
        return jsonify({"ok": True, "count": len(stations), "stations": stations, "inventory": inventory})
    except Exception as e:
        return jsonify({"error": str(e), "stations": []}), 500


@app.route("/api/online/connect", methods=["POST"])
def api_online_connect():
    data = request.get_json(force=True) or {}
    host = (data.get("host") or "").strip()
    port = int(data.get("port") or 0)
    if not host or not port:
        return jsonify({"error": "host and port required"}), 400

    inventory = (data.get("inventory") or "").strip() or find_default_inventory() or ""
    streams = data.get("streams")

    # If no streams but an inventory exists, take them from the inventory
    if not streams and inventory and Path(inventory).exists():
        try:
            stas = get_stations(inventory)
            streams = [
                [s["net"], s["sta"], "", s["default_channel"]]
                for s in stas
            ]
        except Exception:
            pass

    if not streams:
        streams = [["IA", "SBSSI", "", "SHZ"]]  # Sigi station fallback

    from seiswork.utils.slinktool_verify import resolve_live_channels
    cfg_id = (data.get("cfg_id") or "").strip() or None

    # ── Monitoring-area limit ─────────────────────────────────────────────────
    # A region-limited config (created via "Set Area") subscribes only to
    # stations inside the drawn box: fewer streams, focused monitoring.
    _area = _config_region(cfg_id)
    if _area and streams:
        streams = _streams_in_region(streams, inventory, _area)
        if not streams:
            return jsonify({"error": "No stations inside the monitoring area — "
                                     "widen the area or check the inventory"}), 400

    # ── Extra SeedLink sources (multi-server sessions) ────────────────────────
    # data["sources"] = [{host, port, inventory?, streams?}]; each server gets
    # its own client thread, and each source may bring its own sensor XML, so
    # the merged station set (and incoming data) grows beyond a single server.
    extra_sources: list[dict] = []
    sources_skipped: list[dict] = []
    inventory_paths = [inventory] if inventory else []
    for src in (data.get("sources") or data.get("extra_sources") or []):
        shost = (src.get("host") or "").strip()
        try:
            sport = int(src.get("port") or 0)
        except (TypeError, ValueError):
            sport = 0
        if not shost or not sport:
            sources_skipped.append({"host": shost, "port": sport,
                                    "reason": "missing host or port"})
            continue
        if shost == host and sport == port:
            continue   # duplicate of the main source, not an error
        sinv = (src.get("inventory") or "").strip()
        sstreams = src.get("streams")
        if not sstreams and sinv and Path(sinv).exists():
            try:
                sstreams = [[s["net"], s["sta"], "", s["default_channel"]]
                            for s in get_stations(sinv)]
            except Exception:
                sstreams = None
        if not sstreams:
            reason = ("inventory has no stations" if sinv else
                      "no inventory XML uploaded and no streams given")
            sources_skipped.append({"host": shost, "port": sport, "reason": reason})
            continue
        if sinv and Path(sinv).exists() and sinv not in inventory_paths:
            inventory_paths.append(sinv)
        # Region-limited config: keep only in-box stations from this extra source.
        if _area:
            sstreams = _streams_in_region(sstreams, sinv, _area)
            if not sstreams:
                sources_skipped.append({"host": shost, "port": sport,
                                        "reason": "no stations inside the monitoring area"})
                continue
        sstreams = [tuple(s) for s in sstreams]
        # Resolve each station to its single best LIVE band, otherwise a
        # station with several bands live at once (e.g. BH + HH + SH) would
        # get all of them subscribed at connect time (see the
        # orientation-wildcard fallback in _seedlink_live.py).
        try:
            sstreams = resolve_live_channels(sstreams, shost, sport, cfg_id=cfg_id)
        except Exception:
            pass
        extra_sources.append({"host": shost, "port": sport, "streams": sstreams})

    streams = [tuple(s) for s in streams]
    # Same single-best-live-band resolution for the primary source, skipped on
    # a resume (already connected to this exact host:port) so a slinktool
    # query (several seconds) only runs on a connection that's actually
    # changing, not on every idle reconnect.
    _is_resume = (_LIVE_SESSION.connected and _LIVE_SESSION.host == host
                 and _LIVE_SESSION.port == port
                 and (_LIVE_SESSION.cfg_id or None) == (cfg_id or None))
    if not _is_resume:
        try:
            streams = resolve_live_channels(streams, host, port, cfg_id=cfg_id)
        except Exception:
            pass
    # On-disk SDS roots to refill the waveform ring buffer from history on
    # (re)start, so the panel resumes from slarchive instead of starting empty
    # when the SeedLink server serves no time-window. Prefer SeisComP's SDS
    # (if fresh), then SeisWork's own slarchive output (work/online_sds).
    from seiswork.web._realtime_pipeline import online_sds_path as _online_sds
    backfill_sds_paths = []
    try:
        _sc = detect_seiscomp(CFG_FILE).get("sds_dir") or ""
        if _sc and Path(_sc).is_dir():
            backfill_sds_paths.append(_sc)
    except Exception:
        pass
    _own_sds = _online_sds(str(BASE_DIR))
    if Path(_own_sds).is_dir() and _own_sds not in backfill_sds_paths:
        backfill_sds_paths.append(_own_sds)

    started = _LIVE_SESSION.start(host, port, streams, inventory_path=inventory,
                                  cfg_id=cfg_id,
                                  extra_sources=extra_sources or None,
                                  inventory_paths=inventory_paths or None,
                                  include_accel=bool(data.get("include_accel", False)),
                                  backfill_sds_paths=backfill_sds_paths or None)
    resumed = not started  # True = same target & already connected, buffer preserved

    # Mark the active cfg_id for the per-config catalog
    if cfg_id:
        from seiswork.web._realtime_pipeline import set_active_cfg_id
        set_active_cfg_id(str(BASE_DIR), cfg_id)

    # Register the scautopick->trigger pick bridge when an agent is provided
    # (online config already synced to SeisComP). Best-effort; a failure does
    # not fail the connect.
    agent_url   = (data.get("agent_url") or "").strip().rstrip("/")
    agent_token = data.get("agent_token") or ""
    bridge_registered = False
    if agent_url:
        try:
            import urllib.request as _urlreq
            seiswork_url = request.host_url.rstrip("/")
            req = _urlreq.Request(
                f"{agent_url}/register",
                data=json.dumps({
                    "seiswork_url": seiswork_url,
                    "seiswork_token": agent_token,
                }).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {agent_token}"},
                method="POST",
            )
            with _urlreq.urlopen(req, timeout=5) as resp:
                json.loads(resp.read())
            bridge_registered = True
        except Exception:
            pass

    return jsonify({
        "ok": True,
        "host": host,
        "port": port,
        "streams": [list(s) for s in streams],
        "n_streams": len(streams) + sum(len(s["streams"]) for s in extra_sources),
        "inventory": inventory,
        "inventory_paths": inventory_paths,
        "sources": [{"host": s["host"], "port": s["port"],
                     "n_streams": len(s["streams"])} for s in extra_sources],
        "sources_skipped": sources_skipped,
        "bridge_registered": bridge_registered,
        "resumed": resumed,
    })


@app.route("/api/online/disconnect", methods=["POST"])
def api_online_disconnect():
    _LIVE_SESSION.stop()
    return jsonify({"ok": True})


@app.route("/api/online/status", methods=["GET"])
def api_online_status():
    cfg_id = (request.args.get("cfg_id") or "").strip() or None
    return jsonify(_LIVE_SESSION.status(cfg_id))


@app.route("/api/online/waveform", methods=["GET"])
def api_online_waveform():
    station = (request.args.get("station") or "").strip()
    cfg_id  = (request.args.get("cfg_id") or "").strip() or None
    parts = station.split(".")
    if len(parts) != 4:
        return jsonify({"error": "station must be NET.STA.LOC.CHA"}), 400
    net, sta, loc, cha = parts
    return jsonify({"station": station,
                    "points": _LIVE_SESSION.get_buffer(net, sta, loc, cha, cfg_id=cfg_id)})


@app.route("/api/online/waveform/all", methods=["GET"])
def api_online_waveform_all():
    """Every active stream at once, for the live waveform panel stacked per station."""
    cfg_id = (request.args.get("cfg_id") or "").strip() or None
    return jsonify({"streams": _LIVE_SESSION.get_all_buffers(cfg_id=cfg_id)})


@app.route("/api/online/waveform/snapshot", methods=["GET"])
def api_online_waveform_snapshot():
    """Initial snapshot (contiguous per-stream segment format), called once on
    connect. Afterwards the frontend uses POST /delta with a per-stream cursor
    to fetch only new data. session_epoch lets the client detect a server
    restart. ?cfg_id= targets a specific config's session; omitted uses the
    active one."""
    import time as _time
    cfg_id = (request.args.get("cfg_id") or "").strip() or None
    return jsonify({
        "streams"      : _LIVE_SESSION.get_snapshot(bin_s=1.0, win_s=1900.0, cfg_id=cfg_id),
        "server_time"  : _time.time(),
        "session_epoch": _LIVE_SESSION.status(cfg_id).get("session_epoch"),
    })


@app.route("/api/online/waveform/delta", methods=["POST"])
def api_online_waveform_delta():
    """Only new samples since the per-stream cursor (data-time based, not wall
    clock), a small payload. Body: {"cursors": {...}, "cfg_id": "..."}. A
    stream missing from cursors is treated as new and sent the full window.
    The client compares session_epoch to detect a server restart (re-snapshot).
    cfg_id targets a specific config's session; omitted uses the active one."""
    body    = request.get_json(silent=True) or {}
    cursors = body.get("cursors") or {}
    cfg_id  = (body.get("cfg_id") or "").strip() or None
    streams, srv_time = _LIVE_SESSION.get_delta(cursors, cfg_id=cfg_id)
    return jsonify({
        "streams"      : streams,
        "server_time"  : srv_time,
        "session_epoch": _LIVE_SESSION.status(cfg_id).get("session_epoch"),
    })


@app.route("/api/online/waveform/denoised", methods=["GET"])
def api_online_waveform_denoised():
    """DeepDenoiser-cleaned version of the currently-visible waveform rows, for
    the GUI 'denoise view' toggle. On-demand inference (visible keys only; the
    frontend throttles polling). The model loads lazily in the background;
    until ready the response carries ready=False so the frontend keeps the raw
    trace on screen and shows a 'loading denoiser...' hint."""
    import numpy as np
    from obspy import Stream, Trace, UTCDateTime
    keys_param = (request.args.get("keys") or "").strip()
    window_s   = float(request.args.get("window", 1800))
    cfg_id     = (request.args.get("cfg_id") or "").strip() or None
    keys = [k for k in keys_param.split(",") if k]
    picker = get_realtime_picker()
    if picker.denoiser_model is None:
        loading = not picker.ensure_denoiser_async()   # kicks off a background load
        return jsonify({"denoised": {}, "ready": False, "loading": loading})
    out = {}
    for key in keys[:80]:               # hard cap: one request can't blow up
        parts = key.split(".")
        if len(parts) < 2:
            continue
        net = parts[0]; sta = parts[1]
        loc = parts[2] if len(parts) >= 3 else ""
        cha = parts[3] if len(parts) >= 4 else ""
        try:
            t, v = _LIVE_SESSION.get_window(net, sta, loc, cha, seconds=window_s, cfg_id=cfg_id)
            sr   = _LIVE_SESSION.get_sampling_rate(net, sta, loc, cha, cfg_id=cfg_id)
        except Exception:
            continue
        if len(t) < 200 or not sr:
            continue
        tr = Trace(data=v.astype(np.float32))
        tr.stats.network = net; tr.stats.station = sta
        tr.stats.channel = cha or "HHZ"
        tr.stats.sampling_rate = float(sr)
        tr.stats.starttime = UTCDateTime(float(t[0]))
        st_dn = picker.denoise_for_view(Stream([tr]))
        if not st_dn or len(st_dn) == 0:
            continue
        d   = st_dn[0]
        t0  = float(d.stats.starttime.timestamp)
        dt  = 1.0 / float(d.stats.sampling_rate)
        arr = d.data
        step = max(1, len(arr) // 1500)
        out[key] = [{"t": t0 + i * dt, "v": float(arr[i])}
                    for i in range(0, len(arr), step)]
    return jsonify({"denoised": out, "ready": True})


@app.route("/api/online/waveform/stream", methods=["GET"])
def api_online_waveform_stream():
    """SSE waveform push stream: server sends a delta every ~2s, no client
    polling needed. The client opens one EventSource; the connection stays
    alive (long-lived HTTP) so there's no client-side timeout. This makes the
    waveform view independent of the user's internet speed: a slow connection
    still gets data, just later, never disconnected.

    Flow:
      1. Initial snapshot (last 30 minutes) -> type='snapshot'
      2. Delta every 2s                     -> type='delta'
      3. Heartbeat when there is no data    -> type='heartbeat'
      4. Server restart                     -> type='reset' + a fresh snapshot

    ?cfg_id= binds this connection to one config for its whole lifetime, so
    two operators watching different configs each get their own isolated
    feed. Omitted follows whichever config is currently active, re-resolved
    every loop iteration.
    """
    import json as _json
    import time as _time

    cfg_id = (request.args.get("cfg_id") or "").strip() or None

    def _generate():
        cursors: dict[str, float] = {}
        sent_epoch = None

        # ── Initial snapshot ─────────────────────────────────────────────────────
        try:
            snap = _LIVE_SESSION.get_snapshot(bin_s=1.0, win_s=1900.0, cfg_id=cfg_id)
            for s in snap:
                if "last_t" in s:
                    cursors[s["key"]] = s["last_t"]
            sent_epoch = _LIVE_SESSION.status(cfg_id).get("session_epoch")
            payload = _json.dumps({
                "type": "snapshot",
                "streams": snap,
                "server_time": _time.time(),
                "session_epoch": sent_epoch,
            })
            yield f"data: {payload}\n\n"
        except GeneratorExit:
            return
        except Exception as exc:
            yield f"data: {_json.dumps({'type':'error','error':str(exc)})}\n\n"

        # ── Delta loop ────────────────────────────────────────────────────────────
        while True:
            _time.sleep(2)
            try:
                # Server restarted: send a fresh snapshot (previous buffer is invalid)
                cur_status = _LIVE_SESSION.status(cfg_id)
                cur_epoch = cur_status.get("session_epoch")
                if cur_epoch != sent_epoch:
                    cursors.clear()
                    sent_epoch = cur_epoch
                    snap = _LIVE_SESSION.get_snapshot(bin_s=1.0, win_s=900.0, cfg_id=cfg_id)
                    for s in snap:
                        if "last_t" in s:
                            cursors[s["key"]] = s["last_t"]
                    payload = _json.dumps({
                        "type": "snapshot",
                        "streams": snap,
                        "server_time": _time.time(),
                        "session_epoch": sent_epoch,
                    })
                    yield f"data: {payload}\n\n"
                    continue

                if not cur_status.get("connected"):
                    yield f"data: {_json.dumps({'type':'heartbeat','connected':False,'server_time':_time.time()})}\n\n"
                    continue

                streams, srv_time = _LIVE_SESSION.get_delta(cursors, cfg_id=cfg_id)
                for s in streams:
                    lt = s.get("last_t")
                    if lt is not None:
                        cursors[s["key"]] = lt

                payload = _json.dumps({
                    "type": "delta",
                    "streams": streams,
                    "server_time": srv_time,
                    "session_epoch": cur_epoch,
                    "sl_connected": cur_status.get("connected"),
                })
                yield f"data: {payload}\n\n"

            except GeneratorExit:
                return
            except Exception:
                # Do not stop the stream on transient errors, keep looping
                pass

    resp = Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
    )
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"   # disable nginx buffering so pushes stay real-time
    resp.headers["Connection"] = "keep-alive"
    return resp


@app.route("/api/online/spectrogram/all", methods=["GET"])
def api_online_spectrogram_all():
    """Active spectrogram streams: the strip below each waveform row, spanning
    the display window (<=30 minutes). Lazy-loading via `keys` (visible rows
    only) keeps the compute cost low."""
    import time as _time
    t0 = _time.time()
    window_s = float(request.args.get("window", 1800))
    keys_param = (request.args.get("keys") or "").strip()
    cfg_id = (request.args.get("cfg_id") or "").strip() or None
    only_keys = set(k for k in keys_param.split(",") if k) if keys_param else None
    specs = _LIVE_SESSION.get_all_spectrograms(window_s=window_s, only_keys=only_keys, cfg_id=cfg_id)
    elapsed_ms = int((_time.time() - t0) * 1000)
    resp = jsonify({"specs": specs})
    resp.headers["X-Compute-Ms"] = str(elapsed_ms)
    return resp


@app.route("/online/waveform-fullpage")
def online_waveform_fullpage():
    """New full-page zoom tab; only reads the already-running _LIVE_SESSION
    (never calls /api/online/connect), so there's no risk of wiping the buffer."""
    return render_template("waveform_fullpage.html", asset_version=_asset_version(), version=SERVER_VERSION)


@app.route("/online/catalog-map")
def online_catalog_map():
    """Full-screen earthquake catalog map: every detected event, icon sized by
    magnitude, colored by depth, full detail + waveform. Opened in a new tab from the dashboard."""
    return render_template("catalog_map.html", asset_version=_asset_version())


@app.route("/online/station-map-fullpage")
def online_station_map_fullpage():
    """New full-page zoom tab for the Station Map panel; only reads the
    already-running _LIVE_SESSION (never calls /api/online/connect), same
    pattern as /online/waveform-fullpage."""
    return render_template("station_map_fullpage.html", asset_version=_asset_version(), version=SERVER_VERSION)


# ── Phase 2: Real-time PhaseNet + Association + Magnitude ──────────────────────
@app.route("/api/online/realtime/start", methods=["POST"])
def api_realtime_start():
    """Start the picker (PhaseNet, one model batched across all stations) +
    associator (GaMMA, self-contained, no NLLoc grid). Opt-in, never
    auto-enabled on a plain connect since it's CPU/GPU-intensive."""
    if not _LIVE_SESSION.connected:
        return jsonify({"error": "No active live session — connect first"}), 400

    _inv_req = (request.get_json(silent=True) or {}).get("inventory")
    # Merge every inventory of the session (multi-seedlink), so the picker and
    # associator see stations from all sources, not just the primary one.
    inv_paths = [p for p in ([_inv_req] if _inv_req else
                             (_LIVE_SESSION.inventory_paths
                              or [_LIVE_SESSION.inventory_path]))
                 if p and Path(p).exists()]
    if not inv_paths:
        _d = find_default_inventory()
        inv_paths = [_d] if _d else []
    if not inv_paths:
        return jsonify({"error": "Inventory not found — needed for region & magnitude"}), 400
    inventory_path = inv_paths[0]

    try:
        stations = get_stations_multi(inv_paths)
    except Exception as exc:
        return jsonify({"error": f"Failed to parse the inventory: {exc}"}), 500

    # Monitoring-area limit: restrict the picker/associator/grid/REAL to stations
    # inside the drawn box. Also pass the box region to the associator so its
    # center (REAL lat_center + NLLoc TRANS origin) = the box midpoint, not the
    # station centroid. Region-override travels via gamma_cfg["region"] below.
    _area = _config_region(_LIVE_SESSION.cfg_id if _LIVE_SESSION else None)
    if _area:
        stations = _stations_in_bbox(stations, _area)
    if len(stations) < 4:
        msg = ("At least 4 stations required inside the monitoring area"
               if _area else "At least 4 stations required in the inventory")
        return jsonify({"error": msg}), 400

    # Every inventory station is handed to the picker/associator up front, not
    # just whichever ones already had a live packet buffered at this exact
    # moment. station_channels/station_rows are captured once here and never
    # refreshed afterwards (start() is a no-op while already running).
    # Filtering to "active right now" used to permanently freeze the picker to
    # however many of 470+ SeedLink streams sent their first packet within the
    # first few seconds after Connect (often as few as ~10), silently ignoring
    # stations that came online later. _run_cycle()/get_window() already skip
    # a station gracefully when it has no data yet, so including all of them
    # up front is safe; each joins in on whatever cycle its first packet
    # actually arrives.
    #
    # The channel picked per station must not come from the inventory's
    # `default_channel` alone: that's the XML's declared band, which for many
    # stations (e.g. IA.* declared BHZ) doesn't match what's actually
    # transmitting live (e.g. SHZ). resolve_live_channels() already corrects
    # this mismatch at connect() time and persists it in config_station.json,
    # so reusing that cache here ensures a station whose live band differs
    # from its XML default still gets picked (fixed a bug where only ~111/397
    # active streams matched their default_channel, leaving the rest, mostly
    # IA BHZ-vs-SHZ, invisible to the picker).
    from seiswork.utils.slinktool_verify import load_channel_cache
    _cache = load_channel_cache(_LIVE_SESSION.cfg_id) if _LIVE_SESSION.cfg_id else {}
    _live_now = {}
    for k in _LIVE_SESSION.list_stream_keys():
        p = k.split(".")
        if len(p) == 4:
            _live_now.setdefault((p[0], p[1]), p[3])
    station_channels = {
        (s["net"], s["sta"]): (_live_now.get((s["net"], s["sta"]))
                               or _cache.get((s["net"], s["sta"]))
                               or s.get("default_channel", ""))
        for s in stations
    }
    station_rows = stations

    # SeisComP SDS archive: from the request, else auto-detect the local install.
    # Used by the associator to read magnitude waveforms more reliably than the ring buffer.
    req = request.get_json(silent=True) or {}
    sds_path = (req.get("sds_path") or "").strip()
    if not sds_path:
        try:
            det = detect_seiscomp(CFG_FILE)
            sds_path = det.get("sds_dir") or ""
        except Exception:
            sds_path = ""

    p_thr              = float(req.get("p_threshold",        0.3))
    s_thr              = float(req.get("s_threshold",        0.6))
    denoise            = bool(req.get("denoise",             False))
    denoise_pretrained = str(req.get("denoise_pretrained",   "original"))

    # GaMMA parameters from the UI form (all optional; defaults in RealtimeAssociator)
    _graw = req.get("gamma") or {}
    gamma_cfg: dict = {}
    if "min_picks_per_eq" in _graw:
        gamma_cfg["min_picks_per_eq"] = max(2, int(_graw["min_picks_per_eq"]))
    if "min_stations" in _graw:
        gamma_cfg["min_stations"] = max(1, int(_graw["min_stations"]))
    if "max_sigma" in _graw:
        gamma_cfg["max_sigma"] = max(0.1, float(_graw["max_sigma"]))
    if "depth_max" in _graw:
        gamma_cfg["depth_max"] = max(5.0, float(_graw["depth_max"]))
    if "method" in _graw and str(_graw["method"]).upper() in ("BGMM", "GMM"):
        gamma_cfg["method"] = str(_graw["method"]).upper()
    if "use_amplitude" in _graw:
        gamma_cfg["use_amplitude"] = bool(_graw["use_amplitude"])

    # Monitoring-area region sets the associator center (REAL lat_center + NLLoc
    # TRANS origin = box midpoint). RealtimeAssociator._region_cfg honors this override.
    if _area:
        gamma_cfg["region"] = _area
        if "depth_max" not in gamma_cfg and _area.get("depth_max"):
            gamma_cfg["depth_max"] = float(_area["depth_max"])

    # Association backend: default "real" (travel-time grid, stable); the UI may send
    # assoc_backend="gamma"|"pyocto"|"glass3" to use another engine (see
    # RealtimeAssociator._backend_label() for the full set).
    assoc_backend = (req.get("assoc_backend") or "").strip().lower() or None

    picker = get_realtime_picker()
    assoc  = get_realtime_associator()
    started_picker = picker.start(str(BASE_DIR), station_channels,
                                  p_threshold=p_thr, s_threshold=s_thr,
                                  denoise=denoise, denoise_pretrained=denoise_pretrained)
    started_assoc  = assoc.start(str(BASE_DIR), station_rows, inv_paths,
                                 sds_path=sds_path, gamma_cfg=gamma_cfg or None,
                                 assoc_backend=assoc_backend)

    # Start slarchive, only when the SeisComP SDS is not fresh (avoids redundancy)
    from seiswork.web._realtime_pipeline import (
        start_slarchive, online_sds_path as _osp)
    archive_days = None
    if _LIVE_SESSION.cfg_id:
        try:
            _acfg = json.loads((CONFIGS_DIR / _LIVE_SESSION.cfg_id / "meta.json").read_text())
            archive_days = _acfg.get("archive_days")
        except Exception:
            pass
    sl_ok = start_slarchive(
        str(BASE_DIR), _LIVE_SESSION.host or "localhost",
        int(_LIVE_SESSION.port or 18000),
        sc_sds=sds_path,   # pass SC SDS so an active scarchive can be detected
        streams=[(s["net"], s["sta"]) for s in station_rows],  # -S selector (required)
        retain_days=archive_days)

    return jsonify({
        "ok": True,
        "picker_started": started_picker,
        "associator_started": started_assoc,
        "slarchive_started": sl_ok,
        "n_stations": len(station_rows),
        "sds_path": sds_path,
        "online_sds": _osp(str(BASE_DIR)),
    })


@app.route("/api/online/realtime/config", methods=["GET"])
def api_realtime_config():
    """Read the currently active picker+associator config (or the defaults)."""
    picker = get_realtime_picker()
    assoc  = get_realtime_associator()
    gcfg   = dict(getattr(assoc, "_gamma_cfg_override", {}))
    return jsonify({
        "backend": getattr(assoc, "_assoc_backend", "real"),
        "picker": {
            "p_threshold"       : float(getattr(picker, "_p_threshold",        0.3)),
            "s_threshold"       : float(getattr(picker, "_s_threshold",        0.6)),
            "denoise"           : bool( getattr(picker, "_denoise",            False)),
            "denoise_pretrained": str(  getattr(picker, "_denoise_pretrained", "original")),
        },
        "gamma": {
            "min_picks_per_eq": int(gcfg.get("min_picks_per_eq", 4)),
            "min_stations"    : int(gcfg.get("min_stations", 4)),
            "max_sigma"       : float(gcfg.get("max_sigma", 2.0)),
            "depth_max"       : float(gcfg.get("depth_max", 60.0)),
            "method"          : str(gcfg.get("method", "BGMM")),
            "use_amplitude"   : bool(gcfg.get("use_amplitude", True)),
        },
    })


@app.route("/api/online/realtime/stop", methods=["POST"])
def api_realtime_stop():
    get_realtime_picker().stop()
    get_realtime_associator().stop()
    from seiswork.web._realtime_pipeline import stop_slarchive
    stop_slarchive()
    return jsonify({"ok": True})


@app.route("/api/online/realtime/status", methods=["GET"])
def api_realtime_status():
    from seiswork.web._realtime_pipeline import (
        get_engine_log, slarchive_running, online_sds_path as _osp,
        _seiscomp_sds_is_fresh)
    _sc = detect_seiscomp(CFG_FILE)
    _sc_sds_path = _sc.get("sds_dir") or ""
    _sl_running = slarchive_running()
    return jsonify({
        "picker"      : get_realtime_picker().status(),
        "associator"  : get_realtime_associator().status(),
        "engine_log"  : get_engine_log(80),
        "live_session": _LIVE_SESSION.status(),
        "slarchive"   : {
            "running"   : _sl_running,
            "skipped"   : not _sl_running and bool(_sc_sds_path) and
                          _seiscomp_sds_is_fresh(_sc_sds_path),
            "online_sds": _osp(str(BASE_DIR)),
            "sc_sds"    : _sc_sds_path,
        },
    })


@app.route("/api/online/inject/seiscomp", methods=["POST"])
def api_inject_seiscomp():
    """Inject SeisWork (PhaseNet) picks into SeisComP via scdispatch SCML.
    Agency ID = 'SeisWork', methodID = 'PhaseNet-AI'; shows up in the
    SeisComP picker list. Body JSON: { agency_id, method_id, since }
    """
    import tempfile
    import subprocess as _sp
    from seiswork.web._seiscomp_detect import detect_seiscomp
    req = request.get_json(force=True) or {}
    agency_id = req.get("agency_id", "SeisWork")
    method_id = req.get("method_id", "PhaseNet-AI")
    since     = float(req.get("since", 0))

    picks = _LIVE_SESSION.get_pick_log(since=since, n=5000) if _LIVE_SESSION.connected else []
    if not picks:
        return jsonify({"ok": True, "n_picks": 0,
                        "message": "No new picks since the last inject"})

    # Locate SC_ROOT for scdispatch + the db host
    sc_info = detect_seiscomp(CFG_FILE)
    sc_root  = sc_info.get("root") or os.environ.get("SEISCOMP_ROOT") \
               or str(Path.home() / "seiscomp")
    sc_host  = sc_info.get("seedlink_host") or "localhost"
    scdispatch_bin = str(Path(sc_root) / "bin" / "scdispatch")
    if not Path(scdispatch_bin).exists():
        scdispatch_bin = "scdispatch"

    # Build SCML picks, with SeisWork as a separate agency in SeisComP
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<seiscomp xmlns="http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/0.11" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'xsi:schemaLocation="http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/0.11 '
             'http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/0.11/seiscomp3.xsd">',
             '<EventParameters>']
    n = 0
    injected_until = since
    for p in picks:
        t_epoch = float(p.get("t", 0))
        if t_epoch <= since:
            continue
        try:
            iso = datetime.utcfromtimestamp(t_epoch).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
        except Exception:
            continue
        net   = p.get("net", "IA")
        sta   = p.get("sta", "")
        cha   = p.get("cha") or p.get("channel", "SHZ")
        phase = p.get("phase", "P")
        pid   = f"SW.{agency_id}.{net}.{sta}.{phase}.{int(t_epoch*100)}"
        lines += [
            f'  <pick publicID="{pid}">',
            f'    <time><value>{iso}</value></time>',
            f'    <waveformID networkCode="{net}" stationCode="{sta}" '
            f'locationCode="" channelCode="{cha}"/>',
            f'    <methodID>{method_id}</methodID>',
            f'    <phaseHint>{phase}</phaseHint>',
            f'    <agencyID>{agency_id}</agencyID>',
            f'    <author>SeisWork-AI</author>',
            '    <evaluationMode>automatic</evaluationMode>',
            '  </pick>',
        ]
        n += 1
        injected_until = max(injected_until, t_epoch)
    lines += ['</EventParameters>', '</seiscomp>']

    if n == 0:
        return jsonify({"ok": True, "n_picks": 0, "injected_until": injected_until,
                        "message": "No new picks"})

    with tempfile.NamedTemporaryFile(suffix=".xml", mode="w", delete=False) as tf:
        tf.write("\n".join(lines))
        tmp_path = tf.name

    try:
        result = _sp.run(
            [scdispatch_bin, "-H", sc_host, "-i", tmp_path,
             "--input-type", "XML", "--operation", "add"],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "SEISCOMP_ROOT": sc_root,
                 "LD_LIBRARY_PATH": f"{sc_root}/lib:" + os.environ.get("LD_LIBRARY_PATH", "")})
        ok = result.returncode == 0
        return jsonify({
            "ok": ok,
            "n_picks": n,
            "injected_until": injected_until,
            "agency_id": agency_id,
            "stdout": result.stdout[:500] if result.stdout else "",
            "stderr": result.stderr[:500] if result.stderr else "",
            "error": None if ok else f"scdispatch exit {result.returncode}: {result.stderr[:200]}",
        })
    except _sp.TimeoutExpired:
        return jsonify({"ok": False, "n_picks": n, "error": "scdispatch timeout"})
    except FileNotFoundError:
        return jsonify({"ok": False, "n_picks": n,
                        "error": "scdispatch not found — make sure SeisComP is installed"})
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass


# ── Direct event injection into scevent (SeisWork as its own agency) ──────────
# Unlike api_inject_seiscomp (picks only, scautoloc must re-associate/locate),
# this pushes SeisWork's own located events (a full <origin> with <arrival>s and
# their <pick>s, complete waveformID net.sta.loc.cha) straight into SeisComP's
# messaging via scdispatch, so scevent groups them into events under agencyID
# "SeisWork". Works local (SeisComP on this host) or remote (POST the SCML to a
# SeisWork Agent's /dispatch on the SeisComP host/public IP, token-authed).
# SCML schema 0.13 (accepted by SeisComP 3.x-8.x); agencyID/author live inside
# <creationInfo> (the correct place; a bare <agencyID> child is ignored by SC).
_SCML_NS = "http://geofon.gfz-potsdam.de/ns/seiscomp3-schema/0.13"


def _xml_esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _iso_z(dt_val) -> str | None:
    """Normalize a datetime/epoch/ISO string to UTC 'YYYY-mm-ddTHH:MM:SS.ffffffZ'.
    Timezone-correct: a tz-aware value (e.g. '...+07:00') is converted to UTC,
    not just stripped, so every time SeisWork emits (SCML, catalog, waveform
    windows) is genuinely UTC even when the host runs in a local zone like WIB."""
    from datetime import datetime as _dt, timezone as _tz
    try:
        if isinstance(dt_val, (int, float)):
            d = _dt.fromtimestamp(float(dt_val), _tz.utc)
        else:
            s = str(dt_val).strip().replace(" ", "T")
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            try:
                d = _dt.fromisoformat(s)   # strict on fractional digits in 3.10
            except ValueError:
                # Fallback for odd fractional seconds/formats; obspy parses as UTC.
                from obspy import UTCDateTime as _U
                return _U(str(dt_val)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            # naive: assume UTC; aware: convert to UTC
            d = d.replace(tzinfo=_tz.utc) if d.tzinfo is None else d.astimezone(_tz.utc)
    except Exception:
        return None
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _station_wf_map(inventory_path: str) -> dict:
    """(net,sta) -> {loc, band} from the inventory, for building waveformIDs."""
    out = {}
    try:
        for s in get_stations(inventory_path):
            band = (s.get("default_channel") or "SHZ")[:2]   # e.g. 'EH','HH','BH'
            out[(s["net"], s["sta"])] = {"loc": s.get("default_loc", "") or "",
                                         "band": band}
    except Exception:
        pass
    return out


def _event_picks(ev: dict, cfg_id: str | None) -> list[dict]:
    """Collect this event's picks as [{net,sta,phase,time}]. Prefers the
    associated picks CSV (P AND S) written next to the catalog; falls back to the
    event's embedded p_picks (P only). Channel/loc are resolved later from the
    inventory so the SCML waveformID is complete."""
    picks: list[dict] = []
    eid = str(ev.get("event_id", ""))
    # 1) associated picks CSV (has S too): per-cfg catalog dir, then the shared one
    import csv as _csv
    cand = []
    if cfg_id:
        cand.append(CONFIGS_DIR / cfg_id / "catalog" / "picks_associated.csv")
    cand += [WORK_DIR / "catalog" / "picks_associated.csv"]
    for p in cand:
        if not p.exists():
            continue
        try:
            with p.open() as f:
                for row in _csv.DictReader(f):
                    if str(row.get("event_id", "")) != eid:
                        continue
                    t = _iso_z(row.get("pick_time") or row.get("phase_time"))
                    if not t:
                        continue
                    phase = (row.get("phase") or row.get("phase_hint") or "P").strip() or "P"
                    picks.append({"net": row.get("network", "") or row.get("net", ""),
                                  "sta": row.get("station", "") or row.get("sta", ""),
                                  "phase": phase, "time": t})
            if picks:
                return picks
        except Exception:
            continue
    # 2) fallback: embedded P picks
    for pp in (ev.get("p_picks") or []):
        t = _iso_z(pp.get("pick_time"))
        if t:
            picks.append({"net": pp.get("network", ""), "sta": pp.get("station", ""),
                          "phase": "P", "time": t})
    return picks


def _build_events_scml(events: list[dict], wf_map: dict,
                       agency_id: str, author: str, method_id: str,
                       cfg_id: str | None) -> tuple[str, int, int]:
    """Build one SCML EventParameters document for a batch of located events.
    Returns (scml, n_events, n_picks)."""
    L = ['<?xml version="1.0" encoding="UTF-8"?>',
         f'<seiscomp xmlns="{_SCML_NS}" version="0.13">', '<EventParameters>']
    n_ev = n_pick = 0
    for ev in events:
        ot = _iso_z(ev.get("datetime"))
        if ot is None or ev.get("lat") is None or ev.get("lon") is None:
            continue
        eid = str(ev.get("event_id", f"ev{n_ev}"))
        oid = f"{agency_id}/Origin/{eid}"
        picks = _event_picks(ev, cfg_id)
        # ── picks (emit first; origin arrivals reference them) ──
        arrivals = []
        for i, pk in enumerate(picks):
            key = (pk["net"], pk["sta"])
            wf = wf_map.get(key, {"loc": "", "band": "SH"})
            phase = (pk["phase"] or "P").upper()
            comp = "Z" if phase.startswith("P") else "N"   # P on Z, S on horizontal
            cha = f"{wf['band']}{comp}"
            pid = f"{agency_id}/Pick/{eid}/{pk['net']}.{pk['sta']}.{phase}.{i}"
            L += [
                f'  <pick publicID="{_xml_esc(pid)}">',
                f'    <time><value>{pk["time"]}</value></time>',
                f'    <waveformID networkCode="{_xml_esc(pk["net"])}" '
                f'stationCode="{_xml_esc(pk["sta"])}" locationCode="{_xml_esc(wf["loc"])}" '
                f'channelCode="{_xml_esc(cha)}"/>',
                f'    <phaseHint>{_xml_esc(phase)}</phaseHint>',
                '    <evaluationMode>automatic</evaluationMode>',
                f'    <methodID>{_xml_esc(method_id)}</methodID>',
                f'    <creationInfo><agencyID>{_xml_esc(agency_id)}</agencyID>'
                f'<author>{_xml_esc(author)}</author></creationInfo>',
                '  </pick>',
            ]
            arrivals.append((pid, phase))
            n_pick += 1
        # ── origin ──
        try:
            lat, lon = float(ev["lat"]), float(ev["lon"])
            dep = float(ev.get("depth_km") or 10.0)
        except Exception:
            continue
        used = len(arrivals) or int(ev.get("nsta") or 0)
        L += [
            f'  <origin publicID="{_xml_esc(oid)}">',
            f'    <time><value>{ot}</value></time>',
            f'    <latitude><value>{lat:.4f}</value></latitude>',
            f'    <longitude><value>{lon:.4f}</value></longitude>',
            f'    <depth><value>{dep:.1f}</value></depth>',
            f'    <methodID>{_xml_esc(method_id)}</methodID>',
            '    <evaluationMode>automatic</evaluationMode>',
            f'    <quality><usedPhaseCount>{used}</usedPhaseCount>'
            f'<associatedPhaseCount>{used}</associatedPhaseCount>'
            + (f'<standardError>{float(ev["rms"]):.3f}</standardError>' if ev.get("rms") not in (None, "") else "")
            + (f'<azimuthalGap>{float(ev["gap"]):.1f}</azimuthalGap>' if ev.get("gap") not in (None, "") else "")
            + '</quality>',
        ]
        for pid, phase in arrivals:
            # SeisComP SCML Arrival.phase has simple content: <phase>P</phase>
            # (a nested <code> is silently dropped, giving an empty phase_code in the DB).
            L += [f'    <arrival><pickID>{_xml_esc(pid)}</pickID>'
                  f'<phase>{_xml_esc(phase)}</phase></arrival>']
        L += [f'    <creationInfo><agencyID>{_xml_esc(agency_id)}</agencyID>'
              f'<author>{_xml_esc(author)}</author></creationInfo>',
              '  </origin>']
        # ── network magnitude (ML) when available ──
        mag = ev.get("mag")
        if mag not in (None, "", 0) and used:
            mid = f"{agency_id}/Mag/{eid}"
            L += [
                f'  <magnitude publicID="{_xml_esc(mid)}">',
                f'    <magnitude><value>{float(mag):.2f}</value></magnitude>',
                '    <type>ML</type>',
                f'    <originID>{_xml_esc(oid)}</originID>',
                f'    <stationCount>{used}</stationCount>',
                f'    <creationInfo><agencyID>{_xml_esc(agency_id)}</agencyID>'
                f'<author>{_xml_esc(author)}</author></creationInfo>',
                '  </magnitude>',
            ]
        n_ev += 1
    L += ['</EventParameters>', '</seiscomp>']
    return "\n".join(L), n_ev, n_pick


@app.route("/api/online/inject/events", methods=["POST"])
def api_inject_events():
    """Inject SeisWork's OWN located events (origin + arrivals + picks) directly
    into SeisComP's scevent, under agencyID 'SeisWork'.

    Body JSON:
      agency_id  (default 'SeisWork'), author (default 'SeisWork-AI'),
      method_id  (default 'PhaseNet-REAL-NLLoc'),
      since      (epoch or ISO; only events detected/occurring after it),
      limit      (max events, default 50),
      mode       'local' | 'remote' | 'auto'  (auto = remote when agent_url set),
      agent_url, agent_token  (for remote dispatch via the SeisWork Agent),
      messaging  (SeisComP messaging host/queue, default 'localhost/production'),
      operation  'add' | 'update' (default 'add').
    """
    import subprocess as _sp
    from datetime import datetime as _dt
    from seiswork.web._realtime_pipeline import load_catalog
    req = request.get_json(force=True) or {}
    agency_id = (req.get("agency_id") or "SeisWork").strip()
    author    = (req.get("author") or "SeisWork-AI").strip()
    method_id = (req.get("method_id") or "PhaseNet-REAL-NLLoc").strip()
    messaging = (req.get("messaging") or "localhost/production").strip()
    operation = (req.get("operation") or "add").strip()
    limit     = int(req.get("limit") or 50)
    mode      = (req.get("mode") or "auto").strip()
    agent_url = (req.get("agent_url") or "").strip().rstrip("/")
    agent_tok = req.get("agent_token") or ""

    cfg_id = _LIVE_SESSION.cfg_id if _LIVE_SESSION else None
    events = load_catalog(str(BASE_DIR), 0, cfg_id=cfg_id)
    # since filter (by detected_at, else datetime)
    since_raw = req.get("since")
    if since_raw not in (None, "", 0):
        since_iso = _iso_z(since_raw) or ""
        if since_iso:
            events = [e for e in events
                      if (_iso_z(e.get("detected_at")) or _iso_z(e.get("datetime")) or "") > since_iso]
    events = events[:limit]
    if not events:
        return jsonify({"ok": True, "n_events": 0, "n_picks": 0,
                        "message": "No events to inject"})

    # inventory -> waveformID map (loc + band per station)
    inv = (_LIVE_SESSION.inventory_path if _LIVE_SESSION else "") or find_default_inventory() or ""
    wf_map = _station_wf_map(inv) if inv else {}

    scml, n_ev, n_pk = _build_events_scml(events, wf_map, agency_id, author, method_id, cfg_id)
    if n_ev == 0:
        return jsonify({"ok": True, "n_events": 0, "n_picks": 0,
                        "message": "No valid origins built (missing lat/lon/time)"})

    use_remote = (mode == "remote") or (mode == "auto" and agent_url)

    if use_remote:
        if not agent_url:
            return jsonify({"ok": False, "error": "remote mode needs agent_url"}), 400
        try:
            import urllib.request as _u
            r = _u.Request(f"{agent_url}/dispatch",
                           data=json.dumps({"scml": scml, "operation": operation,
                                            "messaging": messaging}).encode(),
                           headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {agent_tok}"},
                           method="POST")
            with _u.urlopen(r, timeout=90) as resp:
                ar = json.loads(resp.read())
            return jsonify({"ok": bool(ar.get("ok")), "via": "agent", "n_events": n_ev,
                            "n_picks": n_pk, "agent": ar})
        except Exception as e:
            return jsonify({"ok": False, "via": "agent", "n_events": n_ev,
                            "n_picks": n_pk, "error": f"agent dispatch failed: {e}"}), 502

    # local scdispatch
    from seiswork.web._seiscomp_detect import detect_seiscomp
    sc = detect_seiscomp(CFG_FILE)
    sc_root = sc.get("root") or os.environ.get("SEISCOMP_ROOT") or str(Path.home() / "seiscomp")
    scd = str(Path(sc_root) / "bin" / "scdispatch")
    scd = scd if Path(scd).exists() else "scdispatch"
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as tf:
        tf.write(scml); tmp = tf.name
    try:
        run = _sp.run([str(Path(sc_root) / "bin" / "seiscomp"), "exec", "scdispatch",
                       "-O", operation, "-i", tmp, "-H", messaging],
                      capture_output=True, text=True, timeout=90,
                      env={**os.environ, "SEISCOMP_ROOT": sc_root})
        ok = run.returncode == 0
        return jsonify({"ok": ok, "via": "local", "n_events": n_ev, "n_picks": n_pk,
                        "returncode": run.returncode,
                        "stderr": (run.stderr or "")[-800:],
                        "error": None if ok else f"scdispatch exit {run.returncode}"})
    except Exception as e:
        return jsonify({"ok": False, "via": "local", "n_events": n_ev,
                        "n_picks": n_pk, "error": str(e)}), 500
    finally:
        try: Path(tmp).unlink()
        except OSError: pass


@app.route("/api/online/realtime/dedup", methods=["POST"])
def api_realtime_dedup():
    """Remove duplicates from the disk catalog (can be called manually from the GUI)."""
    from seiswork.web._realtime_pipeline import dedup_catalog, load_catalog, MAX_EVENTS_KEPT
    cid = _LIVE_SESSION.cfg_id if _LIVE_SESSION else None
    n = dedup_catalog(str(BASE_DIR), cfg_id=cid)
    assoc = get_realtime_associator()
    with assoc._lock:
        assoc.events = load_catalog(str(BASE_DIR), MAX_EVENTS_KEPT, cfg_id=cid)
    return jsonify({"ok": True, "removed": n,
                    "remaining": len(assoc.events)})


@app.route("/api/online/realtime/pipeline_flow", methods=["GET"])
def api_realtime_pipeline_flow():
    """Full pipeline snapshot for the Pipeline Flow Monitor page.
    Combines picker + associator status + the engine log + the latest events."""
    import time as _time
    from seiswork.web._realtime_pipeline import (
        get_engine_log, slarchive_running, online_sds_path as _osp,
        _seiscomp_sds_is_fresh, get_realtime_picker, get_realtime_associator,
    )
    _now  = _time.time()
    _pk   = get_realtime_picker()
    _as   = get_realtime_associator()
    _pst  = _pk.status()
    _ast  = _as.status()

    # Compute the pick rate (picks/minute) from the last 60 s of picks in rolling_picks
    _roll = getattr(_pk, "rolling_picks", [])
    _recent_picks = [p for p in _roll if _now - p.get("t", 0) <= 60]
    _picks_pm = len(_recent_picks)            # picks in the last minute

    # Latest events (max 5) for the output node
    _evs = _as.events[:5]
    _ev_list = []
    for _e in _evs:
        _ev_list.append({
            "id"      : _e.get("event_id", ""),
            "time"    : str(_e.get("datetime", "")),
            "lat"     : _e.get("latitude"),
            "lon"     : _e.get("longitude"),
            "depth"   : _e.get("depth_km"),
            "mag"     : _e.get("magnitude"),
            "n_picks" : len(_e.get("p_picks", [])),
        })

    # Slarchive/SDS info
    _sc     = detect_seiscomp(CFG_FILE)
    _sc_sds = _sc.get("sds_dir") or ""
    _sl_run = slarchive_running()
    _osd    = _osp(str(BASE_DIR))
    _sds    = _osd if _osd else _sc_sds

    # Idle time per node (None = never ran)
    def _idle(t): return round(_now - t, 1) if t else None

    return jsonify({
        "now"   : _now,
        "sds"   : {
            "running"    : _sl_run or bool(_sds),
            "path"       : _sds,
            "n_stations" : _pst.get("n_stations", 0),
            "src"        : "online_sds" if _osd else ("seiscomp" if _sc_sds else "none"),
        },
        "picker": {
            "running"       : _pst.get("running", False),
            "n_cycles"      : _pst.get("n_cycles", 0),
            "n_picks_total" : _pst.get("n_picks_total", 0),
            "picks_per_min" : _picks_pm,
            "idle_sec"      : _idle(_pst.get("last_cycle_time")),
            "error"         : _pst.get("error"),
        },
        "associator": {
            "running"         : _ast.get("running", False),
            "backend"         : _ast.get("backend", "real"),   # "real" | "gamma"
            "n_cycles"        : _ast.get("n_cycles", 0),
            "n_events_total"  : _ast.get("n_events_total", 0),
            "idle_sec"        : _idle(_ast.get("last_cycle_time")),
            "error"           : _ast.get("error"),
            "nlloc_active"    : _ast.get("nlloc", {}).get("active", False),
            "assoc_log_last"  : (_ast.get("assoc_log") or [{}])[-1],
        },
        "events": {
            "n_total"  : _ast.get("n_events_total", 0),
            "list"     : _ev_list,
        },
        "engine_log": get_engine_log(60),
    })


@app.route("/online/pipeline-flow")
def online_pipeline_flow():
    """Pipeline Flow Monitor page: visualizes SeisWork's realtime pipeline."""
    return render_template("pipeline_flow.html", asset_version=_asset_version())


@app.route("/api/online/realtime/picker_stations", methods=["GET"])
def api_realtime_picker_stations():
    """Per-station status for the Pipeline Flow Monitor.
    Returns the list of active stations along with picks from the last 5 minutes.
    ?sta=NET.STA adds the last 60 seconds of waveform for that station."""
    import time as _time, numpy as np
    from seiswork.web._realtime_pipeline import get_realtime_picker
    from seiswork.web._seedlink_live import _LIVE_SESSION

    import math as _math
    _now = _time.time()
    _pk  = get_realtime_picker()
    _as  = get_realtime_associator()
    _pst = _pk.status()

    # Thresholds from the picker
    _p_thr = getattr(_pk, "_p_threshold", 0.3)
    _s_thr = getattr(_pk, "_s_threshold", 0.6)

    def _p_t(p):
        try:
            from obspy import UTCDateTime as _U
            return float(_U(str(p["phase_time"])))
        except Exception:
            return 0.0

    # Group rolling_picks per station (last 10 minutes, enough for the simulation)
    _roll    = getattr(_pk, "rolling_picks", [])
    _by_sta2 : dict = {}
    for _p in _roll:
        _pt = _p_t(_p)
        if _now - _pt > 600:
            continue
        _key = f"{_p['network']}.{_p['station']}"
        _by_sta2.setdefault(_key, []).append({**_p, "_t": _pt})

    # List all configured stations (from the associator's _station_rows).
    # More complete than the picker's _station_channels, which only holds stations with data
    _sta_rows = getattr(_as, "_station_rows", [])   # [{net,sta,lat,lon,elev}]
    _sta_chs  = dict(getattr(_pk, "_station_channels", {}))   # {(net,sta): cha}

    # Merge: configured rows + stations that have picks
    _all_keys: dict = {}   # key -> {net, sta, lat, lon, cha}
    for _r in _sta_rows:
        _k = f"{_r['net']}.{_r['sta']}"
        _all_keys[_k] = {
            "net": _r["net"], "sta": _r["sta"],
            "lat": _r.get("lat"), "lon": _r.get("lon"),
            "cha": _sta_chs.get((_r["net"], _r["sta"]), ""),
        }
    # Add stations that have picks but may be missing from rows (bridge/scautopick)
    for _k in _by_sta2:
        if _k not in _all_keys:
            _n, _s = _k.split(".", 1)
            _all_keys[_k] = {"net": _n, "sta": _s, "lat": None, "lon": None,
                              "cha": _sta_chs.get((_n, _s), "")}

    _stations = []
    for _k, _info in sorted(_all_keys.items()):
        _picks_k = _by_sta2.get(_k, [])
        _p_picks = [x for x in _picks_k if str(x.get("phase_hint","")).upper().startswith("P")]
        _s_picks = [x for x in _picks_k if str(x.get("phase_hint","")).upper().startswith("S")]
        _last_p  = max((_x["_t"] for _x in _p_picks), default=None)
        _last_s  = max((_x["_t"] for _x in _s_picks), default=None)
        _lp_score = next((x["phase_score"] for x in _p_picks if x["_t"] == _last_p), None) if _last_p else None
        _ls_score = next((x["phase_score"] for x in _s_picks if x["_t"] == _last_s), None) if _last_s else None
        _stations.append({
            "key"    : _k,
            "net"    : _info["net"], "sta": _info["sta"], "cha": _info["cha"],
            "lat"    : _info["lat"], "lon": _info["lon"],
            "n_picks": len(_picks_k),
            "n_p"    : len(_p_picks), "n_s": len(_s_picks),
            "last_p" : _last_p,  "last_s": _last_s,
            "score_p": round(float(_lp_score), 3) if _lp_score is not None else None,
            "score_s": round(float(_ls_score), 3) if _ls_score is not None else None,
            "has_data": _info["cha"] != "",
        })

    # Sort: newest picks first, then has-data, then alphabetical
    _stations.sort(key=lambda x: (
        -(max(x["last_p"] or 0, x["last_s"] or 0)),
        not x["has_data"], x["key"],
    ))

    # Waveform + picks for the probability simulation
    _wv = None
    _sta_req = request.args.get("sta", "")
    if _sta_req:
        _sn, _ss = (_sta_req.split(".", 1) + [""])[:2]
        _cha_req = _sta_chs.get((_sn, _ss), "")
        if not _cha_req:
            for (_bn, _bs), _bc in _sta_chs.items():
                if _bs == _ss:
                    _sn, _cha_req = _bn, _bc; break
        _t_arr, _v_arr = _LIVE_SESSION.get_window(_sn, _ss, "", _cha_req, seconds=120)
        if len(_t_arr) > 0:
            _step = max(1, len(_t_arr) // 500)
            _t_ds = _t_arr[::_step].tolist()
            _v_ds = _v_arr[::_step].tolist()
            _sr   = _LIVE_SESSION.get_sampling_rate(_sn, _ss, "", _cha_req) or 100.0
            _pk_sta = _by_sta2.get(_sta_req, [])
            _t0_wv = _t_ds[0]
            _picks_wv = [
                {"t": x["_t"], "phase": x["phase_hint"],
                 "score": round(float(x["phase_score"]), 3),
                 "amp"  : round(float(x.get("phase_amp", 1)), 4)}
                for x in _pk_sta if _t0_wv <= x["_t"] <= _t_ds[-1]
            ]
            # Probability simulation: Gaussian peaks from the picks, evaluated
            # on a 200-point grid to render the curve.
            _N_PROB = 200
            _t_prob = [_t_ds[0] + i * (_t_ds[-1] - _t_ds[0]) / (_N_PROB - 1)
                       for i in range(_N_PROB)]
            _SIGMA  = 0.4   # Gaussian width ~ PhaseNet resolution
            def _gauss_curve(phase_prefix):
                picks_ph = [x for x in _picks_wv
                            if str(x["phase"]).upper().startswith(phase_prefix)]
                curve = [0.0] * _N_PROB
                for pk_ in picks_ph:
                    for j, tj in enumerate(_t_prob):
                        v_ = float(pk_["score"]) * _math.exp(
                            -0.5 * ((tj - pk_["t"]) / _SIGMA) ** 2)
                        if v_ > curve[j]:
                            curve[j] = v_
                return [round(v_, 4) for v_ in curve]

            _wv = {
                "sta": _sta_req, "cha": _cha_req, "sr": _sr,
                "t": _t_ds, "v": _v_ds,
                "picks": _picks_wv,
                "t0": float(_t_ds[0]), "t1": float(_t_ds[-1]),
                "prob": {
                    "t": _t_prob,
                    "p": _gauss_curve("P"),
                    "s": _gauss_curve("S"),
                },
                "thresholds": {"p": _p_thr, "s": _s_thr},
                "window_s": 30,   # PhaseNet sliding window 30s
                "wv_seconds": 120,
            }

    return jsonify({
        "now"        : _now,
        "running"    : _pst.get("running", False),
        "n_configured": len(_all_keys),
        "thresholds" : {"p": _p_thr, "s": _s_thr},
        "stations"   : _stations,
        "waveform"   : _wv,
    })


@app.route("/api/online/realtime/log", methods=["GET"])
def api_realtime_log():
    """Unified engine log (PhaseNet, GaMMA, scautopick): proof each component runs.
    ?n=INT max entries (default 120)."""
    from seiswork.web._realtime_pipeline import get_engine_log
    try:
        n = int(request.args.get("n", 120))
    except ValueError:
        n = 120
    return jsonify({"log": get_engine_log(max(1, min(n, 300)))})


@app.route("/api/online/realtime/backfill", methods=["POST"])
def api_realtime_backfill():
    """Start re-picking + re-associating from SDS for a missed period.

    Body JSON (all optional):
      start_time : ISO string or epoch (default: 24 hours ago)
      end_time   : ISO string or epoch (default: now)
      sds_path   : SDS archive path (default: work/online_sds)

    Only 1 job runs at a time. Check status via GET /api/online/realtime/backfill/status.
    """
    from seiswork.web._realtime_pipeline import (
        run_backfill, get_backfill_state, online_sds_path as _osp
    )
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    state = get_backfill_state()
    if state["running"]:
        return jsonify({"error": "A backfill is already running — wait for it to finish",
                        "state": state}), 409

    if not _LIVE_SESSION.connected:
        return jsonify({"error": "No active live session — connect first"}), 400

    req = request.get_json(silent=True) or {}

    # Resolve the time window
    now = _time.time()
    def _to_epoch(val, default):
        if not val:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
        try:
            from obspy import UTCDateTime
            return UTCDateTime(val).timestamp
        except Exception:
            return default

    start_t = _to_epoch(req.get("start_time"), now - 86400)
    end_t   = _to_epoch(req.get("end_time"),   now)
    if end_t <= start_t:
        return jsonify({"error": "end_time must be after start_time"}), 400
    if end_t - start_t > 7 * 86400:
        return jsonify({"error": "Maximum period is 7 days"}), 400

    # SDS path
    sds_path = (req.get("sds_path") or "").strip()
    if not sds_path:
        sds_path = _osp(str(BASE_DIR))   # work/online_sds
    if not Path(sds_path).exists():
        # fallback to SeisComP SDS
        try:
            det = detect_seiscomp(CFG_FILE)
            sds_path = det.get("sds_dir") or sds_path
        except Exception:
            pass
    if not Path(sds_path).exists():
        return jsonify({"error": f"SDS not found: {sds_path}"}), 400

    # Station rows from the live session + inventory
    inventory_path = (_LIVE_SESSION.inventory_path or req.get("inventory")
                      or find_default_inventory())
    if not inventory_path or not Path(inventory_path).exists():
        return jsonify({"error": "Inventory not found"}), 400
    try:
        stations = get_stations(inventory_path)
    except Exception as exc:
        return jsonify({"error": f"Failed to read the inventory: {exc}"}), 500

    # Use every station in the inventory (not just those currently live)
    active_keys = _LIVE_SESSION.list_stream_keys()
    station_rows = []
    for s in stations:
        cha = s.get("default_channel", "")
        prefix, suffix = f"{s['net']}.{s['sta']}.", f".{cha}"
        if active_keys and not any(k.startswith(prefix) and k.endswith(suffix)
                                    for k in active_keys):
            continue
        station_rows.append(s)

    if len(station_rows) < 4:
        # fallback: use every station in the inventory
        station_rows = stations

    run_backfill(start_t, end_t, sds_path, station_rows,
                 inventory_path, str(BASE_DIR))

    from datetime import datetime as _dt2
    return jsonify({
        "ok"        : True,
        "start_time": _dt.utcfromtimestamp(start_t).isoformat(timespec="seconds"),
        "end_time"  : _dt.utcfromtimestamp(end_t).isoformat(timespec="seconds"),
        "sds_path"  : sds_path,
        "n_stations": len(station_rows),
        "msg"       : "Backfill started in the background — monitor via /api/online/realtime/backfill/status",
    })


@app.route("/api/online/realtime/backfill/status", methods=["GET"])
def api_realtime_backfill_status():
    """Status of the currently/most recently running backfill job."""
    from seiswork.web._realtime_pipeline import get_backfill_state
    state = get_backfill_state()
    return jsonify(state)


def _resolve_online_event(eid):
    """Resolve one live/online event by id: in-memory associator first, then the
    on-disk catalog (covers events from a previous session / server restart)."""
    assoc = get_realtime_associator()
    ev = next((e for e in assoc.events if e["event_id"] == eid), None)
    if ev is None:
        from seiswork.web._realtime_pipeline import load_catalog
        _cid = _LIVE_SESSION.cfg_id if _LIVE_SESSION else None
        catalog = load_catalog(str(BASE_DIR), cfg_id=_cid)
        ev = next((e for e in catalog if e["event_id"] == eid), None)
    return ev


def _resolve_online_sds_path(assoc):
    """SDS path priority for live/online event lookups: fresh SeisComP archive,
    then SeisWork's own slarchive (online_sds), then stale SeisComP archive,
    then whatever the associator was configured with."""
    from seiswork.web._seiscomp_detect import detect_seiscomp as _dsc
    from seiswork.web._realtime_pipeline import (
        online_sds_path as _osp, _seiscomp_sds_is_fresh)
    _sc_info    = _dsc(CFG_FILE)
    _sc_sds     = _sc_info.get("sds_dir") or ""
    _online_sds = _osp(str(BASE_DIR))
    _sc_fresh   = bool(_sc_sds) and Path(_sc_sds).exists() and _seiscomp_sds_is_fresh(_sc_sds)
    if _sc_fresh:
        return _sc_sds
    if Path(_online_sds).exists() and any(Path(_online_sds).iterdir()):
        return _online_sds
    if _sc_sds and Path(_sc_sds).exists():
        return _sc_sds
    return getattr(assoc, "_sds_path", "") or ""


@app.route("/api/online/realtime/event_waveform", methods=["GET"])
def api_realtime_event_waveform():
    """Waveform of the nearest stations around the event time + their picks, for
    the 'view pick results' modal. ?id=<event_id>. Window [t0-15s, t0+75s], the 12
    nearest stations with data, demeaned + downsampled to <=600 points, picks from the pick log."""
    import time as _time
    from math import radians, sin, cos, asin, sqrt
    import numpy as _np
    eid = (request.args.get("id") or "").strip()
    assoc = get_realtime_associator()
    ev = _resolve_online_event(eid)
    if ev is None:
        return jsonify({"error": "event not found in memory or the disk catalog"}), 404
    try:
        from obspy import UTCDateTime
        # Parse the event OT as UTC. UTCDateTime treats a naive string as UTC and
        # honors an explicit offset (+07:00 etc); both normalize to the same UTC
        # epoch the ring buffer/SDS use (trace.stats.starttime is always UTC).
        t0 = UTCDateTime(_iso_z(ev["datetime"]) or ev["datetime"]).timestamp
    except Exception:
        return jsonify({"error": "datetime event invalid"}), 400

    pre, post = 180.0, 600.0  # 13-minute window: 3 min before + 10 min after OT

    def _hav(la1, lo1, la2, lo2):
        r = 6371.0
        dla, dlo = radians(la2 - la1), radians(lo2 - lo1)
        a = sin(dla / 2) ** 2 + cos(radians(la1)) * cos(radians(la2)) * sin(dlo / 2) ** 2
        return 2 * r * asin(min(1.0, sqrt(a)))

    station_rows = assoc._station_rows
    # If station_rows is empty (session not started / server restarted), try the inventory
    if not station_rows:
        inv_path = getattr(assoc, "_inventory_path", None) or \
                   (_LIVE_SESSION.inventory_path if _LIVE_SESSION.connected else None) or \
                   find_default_inventory()
        if inv_path and Path(inv_path).exists():
            try:
                station_rows = get_stations(inv_path)
            except Exception:
                station_rows = []

    rows = sorted(station_rows,
                  key=lambda s: _hav(ev["lat"], ev["lon"], s["lat"], s["lon"]))
    # Fixed window: the ring buffer is a fixed ~1h span, so scaling the window
    # by (now - t0) gained nothing and, for an event hours old (or with a
    # mis-parsed future OT), produced an absurd span. The event slice is
    # always [t0-pre, t0+post]; older data comes from the SDS below.
    win = pre + post + 10

    # ── Only stations associated with this event ─────────────────────────────
    # Authoritative sources: ev["stations"] (NET.STA from GaMMA/association) +
    # ev["p_picks"] (stored P picks). Displayed picks are only from associated
    # stations, not every pick inside the time window. If ev["stations"] is
    # empty (old event without the field), fall back to 12 stations.
    rec_keys = set(ev.get("stations") or [])
    for _pp in (ev.get("p_picks") or []):
        rec_keys.add(f'{_pp["network"]}.{_pp["station"]}')
    if rec_keys:
        rec_rows = [s for s in rows if f'{s["net"]}.{s["sta"]}' in rec_keys]
        if rec_rows:
            rows = rec_rows

    # Displayed picks are only from ev["p_picks"] (stored during GaMMA association).
    # The pick log is not used; it holds every pick in the window, not just associated ones.

    streams = []
    sds_path = _resolve_online_sds_path(assoc)
    use_sds  = bool(sds_path) and Path(sds_path).exists()

    # Show ALL recording stations (when filtered by rec_keys); with the
    # nearest-station fallback, cap at 12 so the canvas does not get too tall.
    max_streams = len(rows) if rec_keys else 12

    # Pre-build the event p_picks dict: epoch per station (NET.STA -> float epoch)
    _ev_ppick = {}
    from datetime import datetime as _dt_pp
    for _pp in (ev.get("p_picks") or []):
        _k = f'{_pp["network"]}.{_pp["station"]}'
        try:
            _ts = _pp["pick_time"]
            try:
                _ev_ppick[_k] = float(UTCDateTime(_ts))
            except Exception:
                _ev_ppick[_k] = float(UTCDateTime(_dt_pp.fromisoformat(str(_ts))))
        except Exception:
            pass

    # The channel actually being recorded often differs from the inventory XML's
    # declared `default_channel` (e.g. IA.* declared BHZ while actually streaming
    # SHZ/HNZ), the same mismatch RealtimeAssociator.start() corrects for the
    # picker via the live-channel cache. Apply the same correction here,
    # otherwise most stations resolve to a channel never archived, yielding 0 streams.
    from seiswork.utils.slinktool_verify import load_channel_cache as _lcc
    _chan_cache = _lcc(_LIVE_SESSION.cfg_id) if _LIVE_SESSION.cfg_id else {}
    _live_chan = {}
    for _k in _LIVE_SESSION.list_stream_keys():
        _kp = _k.split(".")
        if len(_kp) == 4:
            _live_chan.setdefault((_kp[0], _kp[1]), _kp[3])

    for s in rows:
        if len(streams) >= max_streams:
            break
        cha = (_live_chan.get((s["net"], s["sta"]))
               or _chan_cache.get((s["net"], s["sta"]))
               or s.get("default_channel", ""))
        tt, vv = _np.array([]), _np.array([])

        # Try the ring buffer first; fall back to SDS when data is insufficient
        if _LIVE_SESSION.connected:
            t_buf, v_buf = _LIVE_SESSION.get_window(s["net"], s["sta"], "", cha, seconds=win)
            if len(t_buf) >= 5:
                mask = (t_buf >= t0 - pre) & (t_buf <= t0 + post)
                tt, vv = t_buf[mask], v_buf[mask]

        if len(tt) < 5 and use_sds:
            try:
                from obspy.clients.filesystem.sds import Client as _SDS
                from obspy import UTCDateTime as _U
                _cli = _SDS(sds_path)
                _st = _cli.get_waveforms(s["net"], s["sta"], "*", cha + "Z" if not cha.endswith("Z") else cha,
                                         _U(t0 - pre - 1), _U(t0 + post + 1))
                if not _st:
                    _st = _cli.get_waveforms(s["net"], s["sta"], "*", cha,
                                             _U(t0 - pre - 1), _U(t0 + post + 1))
                if _st:
                    _tr = _st[0].copy().detrend("demean")
                    t_sds = _np.arange(_tr.stats.npts) / _tr.stats.sampling_rate + float(_tr.stats.starttime)
                    v_sds = _tr.data.astype(float)
                    mask = (t_sds >= t0 - pre) & (t_sds <= t0 + post)
                    tt, vv = t_sds[mask], v_sds[mask]
            except Exception:
                pass

        if len(tt) < 5:
            continue
        vv = vv - _np.mean(vv)

        # Compute the spectrogram from full-resolution data (before downsampling)
        spec = None
        if len(vv) >= 32:
            try:
                from scipy.signal import spectrogram as _sp_spec
                _fs = (len(tt) - 1) / (tt[-1] - tt[0]) if len(tt) > 1 and tt[-1] > tt[0] else 100.0
                _nperseg = min(128, len(vv) // 4)
                if _nperseg >= 8:
                    _noverlap = _nperseg * 3 // 4
                    _sf, _st_sp, _Sxx = _sp_spec(vv, fs=_fs, nperseg=_nperseg, noverlap=_noverlap, scaling='density')
                    _fmax = min(25.0, _fs / 2)
                    _fidx = _sf <= _fmax
                    _sf = _sf[_fidx]; _Sxx = _Sxx[_fidx, :]
                    _Sdb = 10 * _np.log10(_Sxx + 1e-12)
                    _vm, _vM = float(_np.percentile(_Sdb, 5)), float(_np.percentile(_Sdb, 95))
                    # downsample for a compact payload: max 80 time bins x 60 freq bins
                    _ts_step = max(1, len(_st_sp) // 80)
                    _ff_step = max(1, len(_sf) // 60)
                    _Sdb_d = _Sdb[::_ff_step, ::_ts_step]
                    _st_d  = _st_sp[::_ts_step]
                    _sf_d  = _sf[::_ff_step]
                    spec = {
                        "db": _Sdb_d.tolist(),
                        "t" : [float(tt[0] + tv) for tv in _st_d.tolist()],
                        "f" : _sf_d.tolist(),
                        "vmin": _vm, "vmax": _vM,
                    }
            except Exception:
                pass

        # Picks: only from ev["p_picks"] associated with this event
        pk = []
        _sta_key = f'{s["net"]}.{s["sta"]}'
        if _sta_key in _ev_ppick:
            _pt = _ev_ppick[_sta_key]
            if t0 - pre <= _pt <= t0 + post:
                pk.append({"t": _pt, "phase": "P", "source": "assoc"})

        # first_pick_t for sorting by arrival order
        _dist_km = round(_hav(ev["lat"], ev["lon"], s["lat"], s["lon"]), 1)
        _first_t  = min((p["t"] for p in pk), default=t0 + _dist_km / 6.0)

        # Downsample the trace for display (max 1200 points)
        step = max(1, len(tt) // 1200)
        streams.append({
            "key": f'{s["net"]}.{s["sta"]}..{cha}',
            "net": s["net"], "sta": s["sta"], "cha": cha,
            "dist_km": _dist_km,
            "_first_t": _first_t,
            "points": [{"t": float(a), "v": float(b)}
                       for a, b in zip(tt[::step], vv[::step])],
            "picks": pk,
            **({"spec": spec} if spec else {}),
        })

    # Sort by first arrival time (pick/estimate)
    streams.sort(key=lambda s: s.pop("_first_t", t0 + 9999))
    msg = None
    if not streams:
        # Waveforms may be missing because (a) the event predates the
        # live-session start, so it's absent from the ring buffer, and/or (b)
        # it's missing/gappy in SDS. Distinguish the two rather than always
        # blaming SDS.
        session_ep = _LIVE_SESSION.session_epoch
        ev_str = datetime.utcfromtimestamp(t0).strftime('%Y-%m-%d %H:%M:%S')
        before_session = bool(session_ep) and t0 < session_ep
        parts = [f"Waveform unavailable for this event ({ev_str} UTC)."]
        if before_session:
            parts.append(
                f"The event occurred before the live session started "
                f"({datetime.utcfromtimestamp(session_ep).strftime('%Y-%m-%d %H:%M:%S')} UTC), "
                f"so it isn't in the ring buffer.")
        else:
            parts.append("Not in the live ring buffer (it may already be past the buffer window).")
        if use_sds:
            parts.append(f"Also not found in the SDS archive ({sds_path}) — there may be an archive gap on that date.")
        else:
            parts.append("The SDS archive isn't configured/found, so older data can't be recovered.")
        msg = " ".join(parts)
    return jsonify({"event": ev, "t0": t0, "pre": pre, "post": post,
                    "streams": streams,
                    "source": "sds" if use_sds else "ring",
                    **({"error": msg} if msg else {})})


_CORR_STACK_CACHE: dict = {}   # (eid, phase) -> response dict; FIFO-capped
_CORR_STACK_CACHE_MAX = 64


@app.route("/api/online/realtime/event_waveform/corr_stack", methods=["GET"])
def api_realtime_event_corr_stack():
    """Inter-station cross-correlation matrix + aligned/stacked waveform for one
    live/online-detected event, same QC as the offline Result Viewer's CC & Stack
    panel and catalog_filter.py's min_cc ghost-event filter (?id=<event_id>).

    Reads from SDS only (not the ring buffer) since standard ObsPy Trace
    processing (bandpass/resample) is needed; for a very fresh event whose data
    hasn't been archived to SDS yet, this may return no usable waveform even
    though the live waveform view above already shows it from the ring buffer.

    Deliberately not a pipeline-pool job: it's an interactive per-event QC that
    completes in well under a second for typical live events (<=20 stations), and
    job-dir/status/polling plumbing would only add latency. Successful results
    are cached in-process so reopening an event costs nothing."""
    from obspy import UTCDateTime
    from obspy.clients.filesystem.sds import Client as SDSClient

    from seiswork.utils.catalog_filter import station_cc_matrix, station_stack

    eid = (request.args.get("id") or "").strip()
    if not eid:
        return jsonify({"error": "id required"}), 400
    cache_key = (eid, "P")
    if cache_key in _CORR_STACK_CACHE:
        return jsonify(_CORR_STACK_CACHE[cache_key])
    ev = _resolve_online_event(eid)
    if ev is None:
        return jsonify({"error": "event not found in memory or the disk catalog"}), 404

    stations_picks = []
    for pp in (ev.get("p_picks") or []):
        try:
            t_pick = UTCDateTime(_iso_z(pp["pick_time"]) or pp["pick_time"])
            stations_picks.append((pp["station"], pp["network"], t_pick))
        except Exception:
            continue
    if len(stations_picks) < 2:
        return jsonify({"error": "Fewer than 2 P picks stored for this event"}), 200

    assoc = get_realtime_associator()
    sds_path = _resolve_online_sds_path(assoc)
    if not sds_path or not Path(sds_path).exists():
        return jsonify({"error": "No SDS archive available for this event"}), 200

    try:
        sds = SDSClient(sds_path)
    except Exception as ex:
        return jsonify({"error": f"SDS init failed: {ex}"}), 500

    cc = station_cc_matrix(stations_picks, sds)
    stack = station_stack(stations_picks, sds)
    if cc is None and stack is None:
        # Not cached: the waveform may simply not be archived yet, retry later.
        return jsonify({"error": "No usable waveform in SDS yet for this event "
                                  "(may not be archived from the ring buffer yet)"}), 200

    result = {"event_id": eid, "phase": "P", "cc": cc, "stack": stack}
    if len(_CORR_STACK_CACHE) >= _CORR_STACK_CACHE_MAX:
        _CORR_STACK_CACHE.pop(next(iter(_CORR_STACK_CACHE)))
    _CORR_STACK_CACHE[cache_key] = result
    return jsonify(result)


def _latest_sds_day(p: Path, cap: int = 8000) -> str | None:
    """Last day (YYYY.DDD) of an SDS archive; searches only the latest year
    folder (cheap, does not sweep the whole archive)."""
    max_year = datetime.now().year + 1  # guard against bogus future dirs (bad station clock/GPS lock)
    try:
        years = sorted((d.name for d in p.iterdir()
                        if d.is_dir() and d.name.isdigit() and int(d.name) <= max_year), reverse=True)
    except OSError:
        return None
    if not years:
        return None
    y = years[0]
    best = None
    for i, f in enumerate((p / y).rglob("*.D.*")):
        if i >= cap:
            break
        bits = f.name.rsplit(".", 2)
        if len(bits) == 3 and bits[2].isdigit() and (best is None or bits[2] > best):
            best = bits[2]
    return f"{y}.{best}" if best else y


def _sds_summary(sds_dir: str, fresh_hours: float = 2.0,
                 cap: int = 30000, full: bool = True) -> dict:
    """Summary of one SDS archive: exists/not, last file, last day, & 'fresh'.
    full=True  computes n_files+size (for small archives like online_sds).
    full=False is for a large archive (SeisComP, hundreds of GB): never sweep
    every file; 'fresh' uses _seiscomp_sds_is_fresh (checks *.D folders,
    early-exit, accurate & cheap), n_files/size are skipped."""
    import time as _t
    from seiswork.web._realtime_pipeline import _seiscomp_sds_is_fresh
    info = {"path": sds_dir or None, "exists": False, "n_files": None,
            "size_mb": None, "last_mtime": None, "last_day": None, "fresh": False}
    if not sds_dir:
        return info
    p = Path(sds_dir)
    if not p.exists():
        return info
    info["exists"] = True
    if not full:
        # Large archive: freshness via a .D folder check (reliable, unaffected by caps)
        info["fresh"] = _seiscomp_sds_is_fresh(sds_dir, fresh_hours)
        info["last_day"] = _latest_sds_day(p)
        return info
    newest, total, nfiles, lastday = 0.0, 0, 0, None
    cutoff = _t.time() - fresh_hours * 3600
    max_year = datetime.now().year + 1  # guard against bogus future dirs (bad station clock/GPS lock)
    for i, f in enumerate(p.rglob("*.D.*")):   # NET.STA.LOC.CHA.D.YYYY.DDD
        if i >= cap:
            break
        try:
            st = f.stat()
        except OSError:
            continue
        nfiles += 1
        total += st.st_size
        if st.st_mtime > newest:
            newest = st.st_mtime
        bits = f.name.rsplit(".", 2)
        if len(bits) == 3 and bits[1].isdigit() and bits[2].isdigit() and int(bits[1]) <= max_year:
            if lastday is None or (bits[1], bits[2]) > lastday:
                lastday = (bits[1], bits[2])
    info["n_files"] = nfiles
    info["size_mb"] = round(total / 1e6, 2)
    if newest:
        info["last_mtime"] = datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")
        info["fresh"] = newest > cutoff
    if lastday:
        info["last_day"] = f"{lastday[0]}.{lastday[1]}"
    return info


@app.route("/api/online/sds_status", methods=["GET"])
def api_online_sds_status():
    """Waveform storage status & location for the GUI 'SDS' panel. Explains
    SeisWork's automatic decision: if the SeisComP SDS is still fresh, an
    external scarchive is writing it (SeisWork only reads, slarchive is
    skipped); otherwise SeisWork archives it itself via slarchive into
    work/online_sds; if detection isn't running yet, ring buffer only (RAM)."""
    from seiswork.web._realtime_pipeline import (
        online_sds_path as _osp, slarchive_running, SDS_RETAIN_DAYS)
    det = detect_seiscomp(CFG_FILE)
    sc_sds = det.get("sds_dir") or ""
    sc = _sds_summary(sc_sds, full=False)   # main archive can be hundreds of GB, not swept
    sc["root"] = det.get("root")
    ow_path = _osp(str(BASE_DIR))
    ow = _sds_summary(ow_path, full=True)   # online_sds is small (3-day retention)
    sl_running = slarchive_running()

    if sc["exists"] and sc["fresh"]:
        target, target_path = "seiscomp", sc["path"]
        reason = ("The SeisComP SDS is still fresh (data <2h old) → an external scarchive "
                  "is active. SeisWork reads from here & slarchive is SKIPPED (avoids duplication).")
    elif sl_running:
        target, target_path = "online_sds", ow["path"]
        reason = ("The SeisComP SDS isn't fresh/doesn't exist → SeisWork archives it itself "
                  f"via slarchive into this folder ({SDS_RETAIN_DAYS}-day retention).")
    else:
        target, target_path = "ring_only", None
        reason = ("Real-time detection isn't running yet → nothing archived to disk; waveform "
                  "only lives in the ring buffer (RAM) for the session.")

    read_priority = ["ring_buffer (RAM)"]
    if sc["exists"]:
        read_priority.append(f"SeisComP SDS ({sc['path']})")
    if ow["exists"]:
        read_priority.append(f"SeisWork SDS ({ow['path']})")

    return jsonify({
        "seiscomp": sc,
        "online_sds": ow,
        "slarchive_running": sl_running,
        "session_connected": _LIVE_SESSION.connected,
        "archiving_target": target,
        "archiving_path": target_path,
        "decision_reason": reason,
        "read_priority": read_priority,
        "fresh_window_hours": 2,
        "retain_days": SDS_RETAIN_DAYS,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/online/realtime/event_focal", methods=["GET"])
def api_realtime_event_focal():
    """Compute the focal mechanism (FocoNet_O, polarity-only) on-demand for 1 event.
    First-motion polarities are read from SDS; only succeeds when the event
    waveform is archived. ?id=<event_id>. Result cached in event['focal'].
    Beachball PNG written to work/online_focal/<id>.png (served by event_focal_png)."""
    from obspy import UTCDateTime
    from collections import Counter
    eid = (request.args.get("id") or "").strip()
    assoc = get_realtime_associator()
    ev = next((e for e in assoc.events if e["event_id"] == eid), None)
    if ev is None:
        from seiswork.web._realtime_pipeline import load_catalog
        _cid2 = _LIVE_SESSION.cfg_id if _LIVE_SESSION else None
        ev = next((e for e in load_catalog(str(BASE_DIR), cfg_id=_cid2)
                   if e["event_id"] == eid), None)
    if ev is None:
        return jsonify({"error": "event not found"}), 404

    # Cache hit
    if isinstance(ev.get("focal"), dict):
        return jsonify({"focal": ev["focal"], "cached": True})

    try:
        t0 = UTCDateTime(ev["datetime"]).timestamp
    except Exception:
        return jsonify({"error": "datetime event invalid"}), 400

    # Inventory + SDS (polarity source)
    inv_path = getattr(assoc, "_inventory_path", None) or \
               (_LIVE_SESSION.inventory_path if _LIVE_SESSION.connected else None) or \
               find_default_inventory()
    if not inv_path or not Path(inv_path).exists():
        return jsonify({"error": "inventory not found for computing the focal mechanism"})

    from seiswork.web._seiscomp_detect import detect_seiscomp as _dsc
    from seiswork.web._realtime_pipeline import (
        online_sds_path as _osp, compute_event_focal, _seiscomp_sds_is_fresh)
    _sc_sds   = (_dsc(CFG_FILE).get("sds_dir") or "")
    _online_sds = _osp(str(BASE_DIR))
    _sc_fresh = bool(_sc_sds) and Path(_sc_sds).exists() and _seiscomp_sds_is_fresh(_sc_sds)
    if _sc_fresh:
        sds_path = _sc_sds
    elif Path(_online_sds).exists() and any(Path(_online_sds).iterdir()):
        sds_path = _online_sds
    elif _sc_sds and Path(_sc_sds).exists():
        sds_path = _sc_sds
    else:
        sds_path = getattr(assoc, "_sds_path", "") or ""
    if not sds_path or not Path(sds_path).exists():
        return jsonify({"error": "SDS waveform unavailable for computing the focal mechanism"})

    # P picks: prefer ev["p_picks"] (stored at detection time),
    # fall back to the live pick log (for events detected just now).
    p_rows = []
    stored_p = ev.get("p_picks") or []
    if stored_p:
        from datetime import datetime as _dt2
        for pp in stored_p:
            try:
                ts = pp["pick_time"]
                # GaMMA timestamps use a space instead of 'T' plus a timezone
                # ("2026-06-26 01:45:11.375000+00:00"), which UTCDateTime fails
                # to parse; use datetime.fromisoformat() as a bridge
                try:
                    pt = UTCDateTime(ts)
                except Exception:
                    pt = UTCDateTime(_dt2.fromisoformat(str(ts)))
                p_rows.append({"network"  : pp["network"],
                               "station"  : pp["station"],
                               "pick_time": pt})
            except Exception:
                pass

    if len(p_rows) < 3:
        # Fallback: search the live pick log (new events whose picks are not stored yet)
        picklog = _LIVE_SESSION.get_pick_log(since=t0 - 30, n=1000) if _LIVE_SESSION.connected else []
        rec_keys = set(ev.get("stations") or [])
        for p in picklog:
            if not str(p.get("phase", "P")).upper().startswith("P"):
                continue
            if not (t0 - 30 <= p["t"] <= t0 + 90):
                continue
            if rec_keys and f'{p["net"]}.{p["sta"]}' not in rec_keys:
                continue
            p_rows.append({"network": p["net"], "station": p["sta"],
                           "pick_time": UTCDateTime(p["t"])})

    if len(p_rows) < 3:
        return jsonify({"error": f"This event has < 3 P picks ({len(p_rows)}) — "
                                 f"not enough polarities for FocoNet. "
                                 f"stored p_picks: {len(stored_p)}"})

    # Dominant network, just for labeling/logging. Not used to force one
    # channel band onto every station: a real network mixes broadband and
    # short-period sensors (e.g. IA has several SHZ-only sites alongside
    # HHZ/BHZ ones, see seiswork/utils/channels.py). Borrowing one station's
    # default_channel for the whole event used to make any station on a
    # different band silently read 0 samples, so polarities were 0 even with
    # good data. compute_polarities()/compute_event_focal() already
    # auto-detect the right band per station (HH>BH>EH>SH) when channel=None.
    network = Counter(r["network"] for r in p_rows).most_common(1)[0][0]
    channel = None

    out_dir = str(BASE_DIR / "work" / "online_focal")
    try:
        focal, status = compute_event_focal(
            ev, p_rows, sds_path, inv_path, network, channel,
            str(BASE_DIR), out_dir)
    except Exception as exc:   # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"error": f"FocoNet failed: {exc}"})
    if focal is None:
        return jsonify({"error": status, "network": network, "channel": channel})

    ev["focal"] = focal   # cache on the in-memory event (assoc.events reference)
    from seiswork.web._realtime_pipeline import update_catalog_focal
    try:
        update_catalog_focal(str(BASE_DIR), eid, focal)   # persist across restarts
    except Exception:
        pass
    return jsonify({"focal": focal, "network": network, "channel": channel})


@app.route("/api/online/realtime/event_focal_png", methods=["GET"])
def api_realtime_event_focal_png():
    """Serve the FocoNet beachball PNG for 1 event (produced by event_focal)."""
    from flask import send_file
    eid = (request.args.get("id") or "").strip()
    png = BASE_DIR / "work" / "online_focal" / f"{eid}.png"
    if not png.exists():
        return jsonify({"error": "beachball not computed yet"}), 404
    resp = send_file(str(png), mimetype="image/png")
    # The PNG for one solution never changes (the client versions the URL by
    # strike/dip/rake), so let the browser cache it and avoid re-downloading
    # the image on every poll-driven re-render of the event card.
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.route("/api/online/realtime/events", methods=["GET"])
def api_realtime_events():
    return jsonify({"events": get_realtime_associator().events})


@app.route("/api/online/realtime/catalog", methods=["GET"])
def api_realtime_catalog():
    """Persistent event catalog (from disk); survives restarts, and is
    already partitioned per cfg_id on disk (see _catalog_path). ?limit=N.
    ?cfg_id= targets a specific config's catalog; omitted uses the active one."""
    from seiswork.web._realtime_pipeline import load_catalog
    try:
        limit = int(request.args.get("limit", 0))
    except ValueError:
        limit = 0
    cid = (request.args.get("cfg_id") or "").strip() or (
        _LIVE_SESSION.cfg_id if _LIVE_SESSION else None)
    events = load_catalog(str(BASE_DIR), limit, cfg_id=cid)
    return jsonify({"events": events, "total": len(events)})


@app.route("/api/online/realtime/catalog.csv", methods=["GET"])
def api_realtime_catalog_csv():
    """Download the event catalog as CSV."""
    import csv, io
    from flask import Response
    from seiswork.web._realtime_pipeline import load_catalog
    cid = (request.args.get("cfg_id") or "").strip() or (
        _LIVE_SESSION.cfg_id if _LIVE_SESSION else None)
    events = load_catalog(str(BASE_DIR), 0, cfg_id=cid)
    cols = ["event_id", "datetime", "lat", "lon", "depth_km",
            "mag", "nsta", "rms", "gap", "detected_at"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for ev in events:
        w.writerow(ev)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment; filename=online_catalog.csv"})


@app.route("/api/online/realtime/reloc", methods=["POST"])
def api_online_reloc_run():
    """Trigger a HypoDD relocation on the online catalog (background thread).
    Uses the global IASP91 model. Poll GET /api/online/realtime/reloc for status."""
    from seiswork.web._online_hypodd import run_online_hypodd, get_reloc_status
    from seiswork.web._realtime_pipeline import get_active_cfg_id
    inv = (_LIVE_SESSION.inventory_path
           if _LIVE_SESSION and _LIVE_SESSION.connected else None)
    cid = (_LIVE_SESSION.cfg_id if _LIVE_SESSION else None) or get_active_cfg_id(str(BASE_DIR))
    status = get_reloc_status(cfg_id=cid)
    if status.get("state") == "running":
        return jsonify({"ok": False, "error": "A relocation is already running",
                        "status": status}), 409
    threading.Thread(
        target=run_online_hypodd,
        args=(str(BASE_DIR),),
        kwargs={"inventory_path": inv, "cfg_id": cid},
        daemon=True,
    ).start()
    return jsonify({"ok": True, "message": "HypoDD relocation started (global IASP91)"})


@app.route("/api/online/realtime/reloc", methods=["GET"])
def api_online_reloc_status():
    """Online HypoDD relocation status + the result catalog once finished."""
    from seiswork.web._online_hypodd import get_reloc_status, load_reloc_catalog
    from seiswork.web._realtime_pipeline import get_active_cfg_id
    cid = (_LIVE_SESSION.cfg_id if _LIVE_SESSION else None) or get_active_cfg_id(str(BASE_DIR))
    status = get_reloc_status(cfg_id=cid)
    result = None
    if status.get("state") == "done":
        result = load_reloc_catalog(str(BASE_DIR), cfg_id=cid)
    elif not status:
        # In-memory _reloc_status is empty (e.g. right after a server restart),
        # so restore the last run's result from disk to keep the relocation
        # bar from vanishing even though a valid result is already stored.
        result = load_reloc_catalog(str(BASE_DIR), cfg_id=cid)
        if result:
            status = {"state": "done", "run_id": result.get("run_id"),
                      "started": result.get("run_at"), "finished": result.get("run_at"),
                      "n_input": result.get("n_input", 0), "n_reloc": result.get("n_reloc", 0),
                      "error": None}
    return jsonify({"status": status, "result": result})


@app.route("/api/online/trigger", methods=["POST"])
def api_online_trigger():
    """Called by the SeisWork Agent (the SeisComP-side bridge) on every new pick
    from scautopick, forwarded to _LIVE_SESSION (waveform display) and to
    RealtimePicker.rolling_picks (GaMMA associator) when real-time detection is active."""
    data = request.get_json(force=True) or {}
    net = data.get("net", "")
    sta = data.get("sta", "")
    phase = data.get("phase", "P")
    time_str = data.get("time", "")
    if not net or not sta or not time_str:
        return jsonify({"error": "net, sta, time required"}), 400
    try:
        from obspy import UTCDateTime
        t = UTCDateTime(time_str).timestamp
    except Exception as ex:
        return jsonify({"error": f"cannot parse time: {ex}"}), 400
    _LIVE_SESSION.add_pick(net, sta, t, phase)
    get_realtime_picker().add_bridge_pick(net, sta, phase, t)
    return jsonify({"ok": True})


@app.route("/api/online/picks/recent", methods=["GET"])
def api_online_picks_recent():
    """Real-time pick log, used by the frontend Pick Log View.
    ?since=FLOAT  epoch float, only return picks after this time (incremental)
    ?n=INT        max entries (default 200)
    ?cfg_id=      targets a specific config's session; omitted uses the active one"""
    try:
        since = float(request.args.get("since", 0))
    except ValueError:
        since = 0.0
    try:
        n = int(request.args.get("n", 200))
    except ValueError:
        n = 200
    cfg_id = (request.args.get("cfg_id") or "").strip() or None
    picks = _LIVE_SESSION.get_pick_log(since=since, n=max(1, min(n, 500)), cfg_id=cfg_id)
    return jsonify({"picks": picks, "total": _LIVE_SESSION.get_n_picks_total(cfg_id)})


@app.route("/api/online/psd", methods=["GET"])
def api_online_psd():
    """Power Spectral Density (Welch) for a station from the live buffer, via obspy.
    When an inventory is available, the instrument response is removed, giving PSD in
    (m/s)^2/Hz (acceleration-like dB). Otherwise, raw-counts PSD. ?net= &sta= [&cha=]"""
    net = (request.args.get("net") or "").strip()
    sta = (request.args.get("sta") or "").strip()
    cha = (request.args.get("cha") or "").strip()
    if not net or not sta:
        return jsonify({"error": "net & sta required"}), 400

    # Pick a channel: when not given, take this station's active Z channel
    if not cha:
        for k in _LIVE_SESSION.list_stream_keys():
            p = k.split(".")
            if len(p) == 4 and p[0] == net and p[1] == sta:
                cha = p[3]
                if cha.endswith("Z"):
                    break
    if not cha:
        return jsonify({"error": "no active channel"}), 404

    window_s = 600.0  # 10 minutes is enough for a stable PSD without waste
    t, v = _LIVE_SESSION.get_window(net, sta, "", cha, seconds=window_s)
    sr = _LIVE_SESSION.get_sampling_rate(net, sta, "", cha)
    if len(t) < 256 or not sr:
        return jsonify({"error": "not enough data for the PSD"}), 200

    try:
        import numpy as _np
        from scipy.signal import welch as _welch
        from obspy import Stream, Trace, UTCDateTime

        tr = Trace(data=v.astype(_np.float64))
        tr.stats.network = net; tr.stats.station = sta
        tr.stats.location = ""; tr.stats.channel = cha
        tr.stats.sampling_rate = float(sr)
        tr.stats.starttime = UTCDateTime(float(t[0]))
        st = Stream([tr])
        st.detrend("demean")

        # Remove response to m/s, then acceleration-like PSD (multiply by omega^2) if an inventory exists
        removed = False
        inv_path = _LIVE_SESSION.inventory_path
        if inv_path and Path(inv_path).exists():
            try:
                from obspy import read_inventory
                inv = read_inventory(inv_path)
                st.remove_response(inventory=inv, output="VEL",
                                   pre_filt=None, water_level=60)
                removed = True
            except Exception:
                removed = False

        data = st[0].data
        nperseg = int(min(len(data), max(256, sr * 20)))  # ~20s segments
        f, Pxx = _welch(data, fs=float(sr), nperseg=nperseg,
                        noverlap=nperseg // 2, scaling="density")
        # Drop DC; convert to dB (10*log10). +eps avoids log(0).
        mask = f > 0
        f = f[mask]; Pxx = Pxx[mask]
        psd_db = 10.0 * _np.log10(Pxx + 1e-30)

        # Downsample to ~180 points (log-spaced so the curve stays smooth on a log-f axis)
        if len(f) > 180:
            idx = _np.unique(_np.round(
                _np.logspace(0, _np.log10(len(f) - 1), 180)).astype(int))
            f = f[idx]; psd_db = psd_db[idx]

        return jsonify({
            "net": net, "sta": sta, "cha": cha, "sr": float(sr),
            "window_s": int(window_s),
            "removed_response": removed,
            "freqs": [round(float(x), 4) for x in f],
            "psd":   [round(float(x), 2) for x in psd_db],
        })
    except Exception as ex:
        return jsonify({"error": f"PSD failed: {ex}"}), 200


def _fast_station_count(path: str) -> int:
    """Count <Station> elements via a streaming byte scan, ~90x faster than a full
    ObsPy read_inventory (which parses every response stage). Used only for the
    upload preview count, so an approximate count is enough. Robust against a tag
    split across read-buffer boundaries: the carry is (len(needle)-1) bytes, so no
    complete match ever lies wholly inside it, avoiding missed or double-counted tags."""
    total = 0
    for needle in (b"<Station ", b"<station "):
        carry = b""
        ov = len(needle) - 1
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1 << 20)
                    if not chunk:
                        break
                    buf = carry + chunk
                    total += buf.count(needle)
                    carry = buf[-ov:]
        except Exception:
            return total
    return total


@app.route("/api/online/inventory/upload", methods=["POST"])
def upload_online_inventory():
    """
    Upload a StationXML/FDSNXML file from the browser.
    Saved to WORK_DIR/online_inventory/<filename>.xml, returning its path so
    it can be filled directly into the om-new-inventory form.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file attached"}), 400
    fobj = request.files["file"]
    fname = (fobj.filename or "inventory.xml").strip()
    # Sanitize the file name: only letters, digits, underscores, dots, hyphens
    import re as _re
    safe = _re.sub(r"[^\w.\-]", "_", fname)
    if not safe.lower().endswith(".xml"):
        safe += ".xml"

    inv_dir = WORK_DIR / "online_inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    dest = inv_dir / safe
    try:
        fobj.save(str(dest))
    except Exception as exc:
        return jsonify({"error": f"Failed to save the file: {exc}"}), 500

    # Validate + preview the station count in a single streaming byte scan.
    # Previously this read the whole file into a str and called get_stations()
    # (ObsPy read_inventory, a full parse of every response stage, ~1s per 20
    # MB) just to count stations, which was why large uploads felt slow;
    # _fast_station_count is ~90x faster and enough for a preview.
    n_stations = _fast_station_count(str(dest))
    if n_stations == 0:
        dest.unlink(missing_ok=True)
        return jsonify({"error": "File does not contain StationXML station data"}), 400

    return jsonify({
        "ok": True,
        "path": str(dest),
        "filename": safe,
        "n_stations": n_stations,
    })


# ── Online Config Persistence ────────────────────────────────────────────────────
# Online configs are stored in CONFIGS_DIR/{cfg_id}/meta.json with type="online".
# Unlike offline configs they carry seedlink_host, agent_url, inventory_path
# and no SDS waveforms. GET /api/configs already returns everything; the frontend
# filters by c.type === 'online'.

# ── SeisWork Agent Management ────────────────────────────────────────────────────
_AGENT_PY    = BASE_DIR / "seiswork-agent" / "agent.py"
_AGENT_CONF  = Path.home() / ".seiswork-agent.conf"
_AGENT_PORT  = 7001
_agent_proc  = None   # Popen from a GUI-initiated start


def _agent_python() -> str:
    """Find a Python interpreter that can import Flask for the agent."""
    for candidate in [
        str(BASE_DIR / "seiswork-agent" / ".venv" / "bin" / "python3"),
        "/opt/miniconda3/envs/seiswork/bin/python3",
        "/opt/miniconda3/bin/python3",
        sys.executable,
    ]:
        if Path(candidate).exists():
            return candidate
    return sys.executable


@app.route("/api/agent/status", methods=["GET"])
def api_agent_status():
    """Check whether the agent is running + whether the token has been initialized."""
    import urllib.request as _ur, urllib.error as _ue

    has_conf  = _AGENT_CONF.exists()
    has_agent = _AGENT_PY.exists()
    running   = False
    agent_url = f"http://localhost:{_AGENT_PORT}"

    # Ping /status without auth (agent replies 401 when a token is required)
    try:
        _ur.urlopen(f"{agent_url}/status", timeout=2)
        running = True
    except _ue.HTTPError as e:
        running = (e.code in (200, 401))  # 401 = port open, auth required
    except Exception:
        running = False

    return jsonify({
        "ok"       : True,
        "running"  : running,
        "has_conf" : has_conf,
        "has_agent": has_agent,
        "agent_url": agent_url,
        "port"     : _AGENT_PORT,
    })


def _agent_run_init() -> tuple[bool, str | None, str]:
    """Run agent.py --init and parse the plaintext token from its output.
    Returns (ok, token, raw_output)."""
    import subprocess as _sp
    py = _agent_python()
    r = _sp.run(
        [py, str(_AGENT_PY), "--init"],
        capture_output=True, text=True, timeout=15,
    )
    output = (r.stdout + r.stderr).strip()
    # Agent output format: the token line is a non-empty line containing only
    # alphanumeric + base64 special characters (length > 20 chars),
    # appearing after a "Token" line or a "===" line.
    import re as _re
    token = None
    found_marker = False
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "token" in stripped.lower() or "===" in stripped:
            found_marker = True
            # If the token sits on the same line after ":"
            if ":" in stripped:
                after = stripped.split(":", 1)[1].strip()
                if after and _re.match(r'^[A-Za-z0-9+/=_\-]{20,}$', after):
                    token = after
                    break
        elif found_marker and _re.match(r'^[A-Za-z0-9+/=_\-]{20,}$', stripped):
            token = stripped
            break
    return r.returncode == 0, token, output


def _agent_url_is_local(url: str) -> bool:
    return any(h in (url or "") for h in ("localhost", "127.0.0.1", "0.0.0.0"))


def _propagate_agent_token(token: str) -> int:
    """Write a freshly generated agent token into every online config that points
    at the local agent. agent.py --init overwrites the stored token hash, which
    instantly invalidates the token saved in every existing config, causing
    persistent 401s on /sync (and /dispatch from the realtime pipeline) after
    any token regeneration. Remote-agent configs are never touched (their
    agent has its own conf). Returns the number of configs updated."""
    n = 0
    for meta_file in CONFIGS_DIR.glob("*/meta.json"):
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
            continue
        if meta.get("type") != "online":
            continue
        if not _agent_url_is_local(meta.get("agent_url", "")):
            continue
        if meta.get("agent_token") == token:
            continue
        meta["agent_token"] = token
        meta["updated"] = datetime.now().isoformat(timespec="seconds")
        try:
            meta_file.write_text(json.dumps(meta, indent=2))
            n += 1
        except Exception:
            pass
    return n


@app.route("/api/agent/init", methods=["POST"])
def api_agent_init():
    """
    Run agent.py --init: generate a new token, save its hash to ~/.seiswork-agent.conf.
    Returns the plaintext token (only once; the user must copy it now).
    """
    if not _AGENT_PY.exists():
        return jsonify({"error": f"agent.py not found at {_AGENT_PY}"}), 404
    try:
        ok, token, output = _agent_run_init()
        # Keep existing local-agent configs working: regeneration used to leave
        # them all holding a now-invalid token (401 on every sync/dispatch).
        n_updated = _propagate_agent_token(token) if (ok and token) else 0
        return jsonify({
            "ok"    : ok,
            "token" : token,
            "output": output,
            "configs_updated": n_updated,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent/start", methods=["POST"])
def api_agent_start():
    """Start the agent as a background process. Idempotent; skips if already running."""
    global _agent_proc
    import subprocess as _sp, urllib.request as _ur, urllib.error as _ue

    # Check whether it is already running
    try:
        _ur.urlopen(f"http://localhost:{_AGENT_PORT}/status", timeout=2)
        return jsonify({"ok": True, "message": "Agent is already running"})
    except _ue.HTTPError as e:
        if e.code in (200, 401):
            return jsonify({"ok": True, "message": "Agent is already running"})
    except Exception:
        pass

    if not _AGENT_PY.exists():
        return jsonify({"error": f"agent.py not found at {_AGENT_PY}"}), 404
    if not _AGENT_CONF.exists():
        return jsonify({"error": "Token not generated yet — run Init Token first"}), 400

    py = _agent_python()
    log_path = WORK_DIR / "logs" / "agent.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        log_fh = open(log_path, "a")
        _agent_proc = _sp.Popen(
            [py, str(_AGENT_PY), "--start"],
            stdout=log_fh, stderr=_sp.STDOUT,
            start_new_session=True,   # detach from the Flask process
        )
        import time; time.sleep(1.5)  # brief wait for the port to open
        # Verify
        ok = False
        try:
            _ur.urlopen(f"http://localhost:{_AGENT_PORT}/status", timeout=2)
            ok = True
        except _ue.HTTPError as e:
            ok = e.code in (200, 401)
        except Exception:
            ok = False
        return jsonify({"ok": ok, "pid": _agent_proc.pid,
                        "message": "Agent started successfully" if ok else "Agent started but not yet responding to ping"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/agent/stop", methods=["POST"])
def api_agent_stop():
    """Stop the agent (a process started via the GUI)."""
    global _agent_proc
    import subprocess as _sp

    killed = False
    # Try killing via the Popen we kept
    if _agent_proc is not None:
        try:
            _agent_proc.terminate()
            _agent_proc.wait(timeout=3)
        except Exception:
            try: _agent_proc.kill()
            except Exception: pass
        _agent_proc = None
        killed = True

    # Fallback: fuser / pkill
    try:
        _sp.run(["fuser", "-k", f"{_AGENT_PORT}/tcp"], capture_output=True, timeout=5)
        killed = True
    except Exception:
        pass

    return jsonify({"ok": True, "killed": killed})


@app.route("/api/agent/log", methods=["GET"])
def api_agent_log():
    """Return the last 50 lines of the agent log."""
    log_path = WORK_DIR / "logs" / "agent.log"
    if not log_path.exists():
        return jsonify({"lines": []})
    lines = log_path.read_text(errors="replace").splitlines()
    return jsonify({"lines": lines[-50:]})


@app.route("/api/online/configs", methods=["POST"])
def create_online_config():
    """Create a new online-type config from an inventory XML."""
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    data = request.get_json(force=True) or {}

    inventory_path = (data.get("inventory_path") or "").strip()
    if not inventory_path:
        return jsonify({"error": "inventory_path required"}), 400

    cfg_id    = str(uuid.uuid4())[:8]
    cfg_dir   = CONFIGS_DIR / cfg_id
    cfg_dir.mkdir()

    # Parse stations from the inventory
    stations = []
    if Path(inventory_path).exists():
        try:
            stations = get_stations(inventory_path)
        except Exception:
            pass

    meta = {
        "id"            : cfg_id,
        "type"          : "online",
        "name"          : (data.get("name") or f"Online {cfg_id}").strip(),
        "created"       : datetime.now().isoformat(timespec="seconds"),
        "inventory_path": inventory_path,
        "seedlink_host" : data.get("seedlink_host", ""),
        "seedlink_port" : int(data.get("seedlink_port") or 18000),
        "agent_url"     : data.get("agent_url", ""),
        "agent_token"   : data.get("agent_token", ""),
        "local_sl_host" : data.get("local_sl_host", "localhost"),
        "local_sl_port" : int(data.get("local_sl_port") or 18000),
        "stations"      : stations,
        "n_stations"    : len(stations),
        "synced"        : False,
        "region"        : data.get("region", {}),
        "waveform"      : {},
        "fdsn_url"      : data.get("fdsn_url", ""),
        # Extra SeedLink sources: [{host, port, inventory_path}]; each may
        # bring its own sensor XML so one session ingests several servers.
        "extra_sources" : data.get("extra_sources") or [],
        # Subscribe to accelerometer bands (HN/BN/EN/...) too, not just
        # seismometer bands. Off by default (PhaseNet-style pickers are
        # tuned on velocity data).
        "include_accel" : bool(data.get("include_accel", False)),
        # SDS archive retention (days), separate from the live ring buffer
        # (fixed, in-memory, ~1h): controls how long historical event
        # waveforms stay readable from disk before _sds_cleanup_loop deletes
        # them. Default 1 day.
        "archive_days"  : int(data.get("archive_days") or 1),
    }
    (cfg_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return jsonify({"id": cfg_id, "message": "Online config saved"})


@app.route("/api/online/configs/<cfg_id>", methods=["PUT"])
def update_online_config(cfg_id):
    """Update seedlink host / agent URL / sync status after wizard step 2."""
    cfg_dir = CONFIGS_DIR / cfg_id
    if not cfg_dir.exists():
        return jsonify({"error": "Config not found"}), 404
    meta_file = cfg_dir / "meta.json"
    try:
        meta = json.loads(meta_file.read_text())
    except Exception:
        return jsonify({"error": "meta.json corrupt"}), 500

    data = request.get_json(force=True) or {}
    # Only update the fields that were sent
    for field in ("name", "seedlink_host", "seedlink_port", "agent_url",
                  "agent_token", "local_sl_host", "local_sl_port", "synced",
                  "extra_sources", "inventory_path", "include_accel", "archive_days"):
        if field in data:
            meta[field] = data[field]
    # Re-derive the cached station list when the inventory changed (e.g. a
    # re-upload after station positions moved), otherwise the station count/
    # map shown for this config keeps reflecting the old XML.
    if "inventory_path" in data:
        inv_path = (data.get("inventory_path") or "").strip()
        if inv_path and Path(inv_path).exists():
            try:
                stations = get_stations(inv_path)
                meta["stations"] = stations
                meta["n_stations"] = len(stations)
            except Exception:
                pass
    meta["updated"] = datetime.now().isoformat(timespec="seconds")
    meta_file.write_text(json.dumps(meta, indent=2))
    return jsonify({"id": cfg_id, "message": "Updated"})


# ── Monitoring area (View Map XML, Set Area, Save) ─────────────────────────────
# A user draws a bounding box on the inventory preview map; we clone the config
# into a NEW cfg_id limited to that area: only in-box stations are kept, the
# NLLoc travel-time grid is (re)generated centered on the box midpoint, and a
# region-limit flag makes the live session subscribe only to in-box stations.
# The REAL associator's lat_center then follows the same box midpoint (see
# RealtimeAssociator._region_cfg honoring the region override).
def _norm_region(region: dict) -> dict | None:
    """Validate/normalize a drawn bbox into a full region dict with center + span.
    Returns None when the box is degenerate (min>=max)."""
    try:
        lat_min = float(region["lat_min"]); lat_max = float(region["lat_max"])
        lon_min = float(region["lon_min"]); lon_max = float(region["lon_max"])
    except (KeyError, TypeError, ValueError):
        return None
    if lat_min >= lat_max or lon_min >= lon_max:
        return None
    return {
        "name"     : (region.get("name") or "Monitoring Area"),
        "lat"      : (lat_min + lat_max) / 2.0,   # box midpoint = REAL/NLLoc center
        "lon"      : (lon_min + lon_max) / 2.0,
        "lat_min"  : lat_min, "lat_max": lat_max,
        "lon_min"  : lon_min, "lon_max": lon_max,
        "depth_max": float(region.get("depth_max") or 60.0),
        "limit"    : True,   # subscribe/pipeline restrict stations to this box
    }


def _config_region(cfg_id) -> dict | None:
    """Return a config's monitoring-area region only when it is limit-enabled
    (created via Set Area). None otherwise, so callers keep the old behavior."""
    if not cfg_id:
        return None
    try:
        meta = json.loads((CONFIGS_DIR / str(cfg_id) / "meta.json").read_text())
    except Exception:
        return None
    reg = meta.get("region") or {}
    if not reg.get("limit"):
        return None
    if all(k in reg for k in ("lat_min", "lat_max", "lon_min", "lon_max")):
        return reg
    return None


def _in_bbox(lat, lon, region) -> bool:
    return (region["lat_min"] <= lat <= region["lat_max"] and
            region["lon_min"] <= lon <= region["lon_max"])


def _streams_in_region(streams, inventory: str, region: dict):
    """Keep only streams [net, sta, loc, cha] whose station falls inside the box.
    Station coordinates come from the inventory XML; a stream whose station is
    not found in the inventory is dropped (can't confirm it is inside).

    Fails closed (returns []) when the inventory is missing or unparsable;
    this is a safety-relevant filter (it's the only thing keeping a
    region-limited config's monitor confined to its own box), so an inventory
    problem must never silently fall back to "subscribe to everything"."""
    if not inventory or not Path(inventory).exists():
        return []
    try:
        coords = {(s["net"], s["sta"]): (s["lat"], s["lon"])
                  for s in get_stations(inventory)}
    except Exception:
        return []
    out = []
    for s in streams:
        key = (s[0], s[1])
        c = coords.get(key)
        if c and _in_bbox(c[0], c[1], region):
            out.append(s)
    return out


def _stations_in_bbox(stations: list, region: dict) -> list:
    """Subset of station dicts (net/sta/lat/lon/...) that fall inside the box."""
    return [s for s in stations if _in_bbox(s["lat"], s["lon"], region)]


def _nll_grid_tool(name: str) -> str:
    """Path to a bundled NonLinLoc grid binary (Vel2Grid/Grid2Time), else PATH."""
    cand = BASE_DIR / "core" / "bin" / name
    if cand.exists():
        return str(cand)
    return shutil.which(name) or ""


def _build_area_grid_bg(cfg_dir: Path, stations: list, center):
    """Generate the IASP91 travel-time grid for an area config in a background
    thread (Grid2Time can take a while). Records status in meta.json:
    nlloc_grid_status in {building, ready, failed} + nlloc_grid_dir."""
    meta_file = cfg_dir / "meta.json"

    def _run():
        status, grid_dir_out = "failed", ""
        try:
            from seiswork.modules.locator.nlloc_grids import ensure_global_grids
            cache_root = str(WORK_DIR / "nlloc_global_grids")
            v2g = _nll_grid_tool("Vel2Grid")
            g2t = _nll_grid_tool("Grid2Time")
            if v2g and g2t and stations:
                gdir, _prefix = ensure_global_grids(
                    stations, cache_root, v2g, g2t, center=center)
                if gdir:
                    status, grid_dir_out = "ready", gdir
        except Exception:
            status = "failed"
        # Persist the outcome (re-read meta so we don't clobber concurrent edits)
        try:
            m = json.loads(meta_file.read_text())
            m["nlloc_grid_status"] = status
            if grid_dir_out:
                m["nlloc_grid_dir"] = grid_dir_out
            meta_file.write_text(json.dumps(m, indent=2))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


@app.route("/api/online/configs/<cfg_id>/area", methods=["POST"])
def set_online_config_area(cfg_id):
    """Set a monitoring area on an existing online config: clone into a new
    region-limited config, keep only in-box stations, and kick off NLLoc grid
    generation centered on the box midpoint."""
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    src_dir  = CONFIGS_DIR / cfg_id
    src_meta = src_dir / "meta.json"
    if not src_meta.exists():
        return jsonify({"error": "Config not found"}), 404
    try:
        src = json.loads(src_meta.read_text())
    except Exception:
        return jsonify({"error": "meta.json corrupt"}), 500

    data   = request.get_json(force=True) or {}
    region = _norm_region(data.get("region") or {})
    if not region:
        return jsonify({"error": "Invalid area — need lat_min<lat_max and lon_min<lon_max"}), 400

    inventory_path = (src.get("inventory_path") or "").strip()
    if not inventory_path or not Path(inventory_path).exists():
        return jsonify({"error": "Source config has no inventory XML"}), 400

    # In-box stations only (get_stations already supports bbox filtering).
    try:
        stations = get_stations(
            inventory_path,
            lat_min=region["lat_min"], lat_max=region["lat_max"],
            lon_min=region["lon_min"], lon_max=region["lon_max"])
    except Exception as exc:
        return jsonify({"error": f"Failed to parse inventory: {exc}"}), 500
    if not stations:
        return jsonify({"error": "No stations fall inside the drawn area"}), 400

    new_id  = str(uuid.uuid4())[:8]
    new_dir = CONFIGS_DIR / new_id
    new_dir.mkdir()

    meta = {
        "id"            : new_id,
        "type"          : "online",
        "name"          : (data.get("name") or
                           f"{src.get('name', 'Online')} — Area").strip(),
        "created"       : datetime.now().isoformat(timespec="seconds"),
        "parent_id"     : cfg_id,
        "inventory_path": inventory_path,
        "seedlink_host" : src.get("seedlink_host", ""),
        "seedlink_port" : int(src.get("seedlink_port") or 18000),
        "agent_url"     : src.get("agent_url", ""),
        "agent_token"   : src.get("agent_token", ""),
        "local_sl_host" : src.get("local_sl_host", "localhost"),
        "local_sl_port" : int(src.get("local_sl_port") or 18000),
        "stations"      : stations,
        "n_stations"    : len(stations),
        "synced"        : False,
        "region"        : region,
        "waveform"      : {"limit_to_region": True},
        "fdsn_url"      : src.get("fdsn_url", ""),
        "extra_sources" : src.get("extra_sources") or [],
        "include_accel" : bool(src.get("include_accel", False)),
        "archive_days"  : int(src.get("archive_days") or 1),
        "nlloc_grid_status": "building",
    }
    (new_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    # Grid generation (Grid2Time is slow) runs in the background; the clone is
    # usable immediately (runtime falls back to the bundled grid until ready).
    _build_area_grid_bg(new_dir, stations, (region["lat"], region["lon"]))

    return jsonify({
        "id"        : new_id,
        "n_stations": len(stations),
        "center"    : {"lat": region["lat"], "lon": region["lon"]},
        "grid_status": "building",
        "message"   : f"Area config created ({len(stations)} stations in box)",
    })


@app.route("/api/online/configs/<cfg_id>/sync", methods=["POST"])
def sync_online_config(cfg_id):
    """
    Calls the SeisWork Agent to:
      1. Query INFO STREAMS from the remote seedlink
      2. Push the inventory to SeisComP
      3. Generate binding key files
      4. Restart seedlink
    Then updates meta.json synced=True.
    """
    import urllib.request as _urlreq

    cfg_dir   = CONFIGS_DIR / cfg_id
    meta_file = cfg_dir / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": "Config not found"}), 404

    try:
        meta = json.loads(meta_file.read_text())
    except Exception:
        return jsonify({"error": "meta.json corrupt"}), 500

    if meta.get("type") != "online":
        return jsonify({"error": "This config is not the online type"}), 400

    def _save_last_sync(ok: bool, error: str = "", extra: dict | None = None):
        """Record the sync attempt result in meta.json so the failure/success reason
        stays visible in the UI after a reload (not just a 'not synced' badge)."""
        try:
            meta["last_sync"] = {
                "ok": bool(ok),
                "error": error,
                "time": datetime.now().isoformat(timespec="seconds"),
                **(extra or {}),
            }
            meta_file.write_text(json.dumps(meta, indent=2))
        except Exception:
            pass

    agent_url   = meta.get("agent_url", "").rstrip("/")
    agent_token = meta.get("agent_token", "")
    if not agent_url:
        _save_last_sync(False, "agent_url is not set in the config")
        return jsonify({"error": "agent_url is not set in the config"}), 400

    sl_host = meta.get("seedlink_host", "")
    sl_port = int(meta.get("seedlink_port") or 18000)
    if not sl_host:
        _save_last_sync(False, "seedlink_host (remote) is not set in the config")
        return jsonify({"error": "seedlink_host is not set in the config"}), 400

    # Read the inventory XML when present
    inv_xml = ""
    inv_path = meta.get("inventory_path", "")
    if inv_path and Path(inv_path).exists():
        try:
            inv_xml = Path(inv_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass

    station_channels = {
        s["sta"]: s.get("default_channel") or f"{BAND_PRIORITY[0]}Z"
        for s in meta.get("stations", []) if s.get("sta")
    }

    payload = json.dumps({
        "seedlink_host": sl_host,
        "seedlink_port": sl_port,
        "inventory_xml": inv_xml,
        "station_channels": station_channels,
    }).encode()

    def _post_sync(token: str) -> dict:
        req = _urlreq.Request(
            f"{agent_url}/sync",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with _urlreq.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read())

    try:
        try:
            result = _post_sync(agent_token)
        except _urlreq.HTTPError as e:
            if e.code not in (401, 403):
                raise
            # Auth failed. When the agent runs on THIS machine, the mismatch is
            # self-inflicted (agent.py --init was re-run after this config saved
            # its token) and self-healable: regenerating the token is already an
            # unauthenticated local operation (/api/agent/init), so rotate it,
            # heal every local-agent config, and retry once.
            if not (_agent_url_is_local(agent_url) and _AGENT_PY.exists()):
                raise
            ok, new_token, _out = _agent_run_init()
            if not (ok and new_token):
                raise
            _propagate_agent_token(new_token)
            agent_token = new_token
            meta["agent_token"] = new_token   # keep the in-memory copy coherent
            result = _post_sync(new_token)
    except _urlreq.HTTPError as e:
        body = ""
        try: body = e.read().decode(errors="replace")[:300]
        except Exception: pass
        if e.code in (401, 403):
            msg = ("The agent refused (auth failed) — the Agent Token doesn't match. "
                   "Fill in/generate the correct agent token in the Sync tab.")
        else:
            msg = f"Agent error HTTP {e.code}: {body or e.reason}"
        _save_last_sync(False, msg)
        return jsonify({"ok": False, "error": msg}), 502
    except Exception as e:
        msg = (f"Could not reach the agent at {agent_url} ({e}). "
               "Make sure the agent is running; also check that the remote SeedLink "
               f"{sl_host}:{sl_port} is reachable.")
        _save_last_sync(False, msg)
        return jsonify({"ok": False, "error": msg}), 502

    # Update status synced
    if result.get("ok"):
        meta["synced"] = True
        meta["updated"] = datetime.now().isoformat(timespec="seconds")
        _save_last_sync(True, "", {"n_streams": result.get("n_streams"),
                                   "n_stations": result.get("n_stations")})
    else:
        _save_last_sync(False, result.get("error", "Sync failed on the agent — check the steps"))

    return jsonify(result)


@app.route("/api/online/configs/<cfg_id>/verify_channels", methods=["POST"])
def verify_online_channels(cfg_id):
    """Cross-check this config's per-station default_channel (from the
    inventory, already epoch-corrected by get_stations()) against what the
    configured SeedLink server actually carries right now, via slinktool.
    An inventory can go stale relative to the live acquisition chain (sensor
    swapped, channel re-bound) without ever being regenerated; this is the
    only way to catch that discrepancy, since it needs a live query."""
    cfg_dir = CONFIGS_DIR / cfg_id
    meta_file = cfg_dir / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": "Config not found"}), 404
    try:
        meta = json.loads(meta_file.read_text())
    except Exception:
        return jsonify({"error": "meta.json corrupt"}), 500

    host = (request.get_json(silent=True) or {}).get("host") or meta.get("seedlink_host", "")
    port = int((request.get_json(silent=True) or {}).get("port") or meta.get("seedlink_port") or 18000)
    if not host:
        return jsonify({"error": "No SeedLink host configured for this config"}), 400

    stations = meta.get("stations", [])
    if not stations:
        return jsonify({"error": "This config has no stations to verify"}), 400

    from seiswork.utils.slinktool_verify import verify_channels
    try:
        report = verify_channels(stations, host, port, cfg_id=cfg_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify(report)


def _local_ipv4s() -> list:
    """Best-effort list of this host's LAN IPv4 addresses (for the connect URL)."""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ips.append(s.getsockname()[0]); s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


@app.route("/api/server-info", methods=["GET"])
def api_server_info():
    """Connection info to hand to clients: addresses, server_id, auth status, and
    the bearer token. The token value is only revealed to a localhost request
    (the operator on the server machine) or to a caller that already presents the
    correct token, never to an unauthenticated remote caller."""
    remote   = (request.remote_addr or "")
    is_local = remote in ("127.0.0.1", "::1") or remote.startswith("127.")
    has_tok  = bool(AUTH_TOKEN) and secrets.compare_digest(_request_token(), AUTH_TOKEN)
    show_tok = is_local or has_tok or not AUTH_TOKEN
    host_hdr = request.host or ""
    port     = host_hdr.split(":")[-1] if ":" in host_hdr else "5000"
    urls     = [f"http://{ip}:{port}" for ip in _local_ipv4s()]
    return jsonify({
        "server_id"    : SERVER_ID,
        "hostname"     : socket.gethostname(),
        "host"         : host_hdr,
        "urls"         : urls,
        "auth_required": bool(AUTH_TOKEN),
        "token"        : (AUTH_TOKEN if show_tok else ""),
        "token_hidden" : (not show_tok),
    })


@app.route("/api/server-info/token", methods=["POST"])
def api_set_server_token():
    """Set or generate this server's bearer token at runtime (so clients can
    connect with it). Allowed only from localhost (operator on the server) or by
    a caller already holding the current token. Persisted to ~/.seiswork/token."""
    global AUTH_TOKEN
    remote   = (request.remote_addr or "")
    is_local = remote in ("127.0.0.1", "::1") or remote.startswith("127.")
    has_tok  = bool(AUTH_TOKEN) and secrets.compare_digest(_request_token(), AUTH_TOKEN)
    if not (is_local or has_tok or not AUTH_TOKEN):
        return jsonify({"error": "only from server machine or with the current token"}), 403
    data = request.get_json(force=True, silent=True) or {}
    new = secrets.token_urlsafe(18) if data.get("generate") else str(data.get("token", "")).strip()
    AUTH_TOKEN = new
    try:
        if new:
            _TOKEN_FILE.write_text(new)
        elif _TOKEN_FILE.exists():
            _TOKEN_FILE.unlink()
    except Exception:
        pass
    return jsonify({"token": new, "auth_required": bool(new)})


# ── Live reload (hot-reload of GUI assets, no hard refresh/cache) ───────────────
# Edits to templates/index.html or static/{js,css} appear in the browser within
# a second: no Ctrl+Shift+R, no manual restart. The page opens an SSE stream;
# when any front-end file's mtime changes the server pushes a new token and the
# page reloads itself. Python edits trigger a graceful in-process re-exec, but
# only while no job is running (so long pipelines are never killed mid-flight).
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0   # never cache static files
app.config["TEMPLATES_AUTO_RELOAD"]     = True  # re-read index.html on every request

LIVE_RELOAD  = os.environ.get("SEISWORK_NO_LIVERELOAD") != "1"
AUTO_RESTART = os.environ.get("SEISWORK_NO_AUTORESTART") != "1"

_WEB_DIR       = Path(__file__).resolve().parent
_STATIC_DIR    = _WEB_DIR / "static"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_PKG_DIR       = _WEB_DIR.parent                 # seiswork/ package (watch .py)
_REPO_ROOT     = _PKG_DIR.parent
_DOCS_HTML     = _REPO_ROOT / "docs" / "SeisWork_Dokumentasi_Lengkap.html"
_CHANGELOG_JSON = _REPO_ROOT / "CHANGELOG.json"


def _latest_mtime(bases, suffixes) -> float:
    latest = 0.0
    for base in bases:
        if not base.exists():
            continue
        for f in base.rglob("*"):
            if f.is_file() and f.suffix in suffixes:
                try:
                    latest = max(latest, f.stat().st_mtime)
                except OSError:
                    pass
    return latest


def _asset_version() -> str:
    """Cache-busting token / reload signal = newest front-end asset mtime."""
    return str(int(_latest_mtime([_STATIC_DIR, _TEMPLATES_DIR],
                                 {".js", ".css", ".html"})))


@app.route("/api/livereload")
def api_livereload():
    """Server-sent events stream: emits a new token whenever a GUI asset changes."""
    if not LIVE_RELOAD:
        return jsonify({"enabled": False})
    import time as _t
    from flask import Response

    def _stream():
        last = _asset_version()
        yield f"retry: 1500\ndata: {last}\n\n"
        while True:
            _t.sleep(1.0)
            cur = _asset_version()
            if cur != last:
                last = cur
                yield f"data: {cur}\n\n"
            else:
                yield ": ping\n\n"             # keep-alive comment

    return Response(_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


def _server_busy() -> bool:
    """True if any pick/pipeline subprocess, or FDSN/catalog download thread, is
    still active (don't restart then)."""
    for procs in (_pick_procs, _pipe_procs):
        for p in list(procs.values()):
            try:
                if p is not None and p.poll() is None:
                    return True
            except Exception:
                pass
    for jobs in (_jobs, _ql_jobs):
        for j in list(jobs.values()):
            if j.get("state") == "running":
                return True
    return False


def _reexec_server():
    """Restart this server in place via os.execv, reusing the same listening port.
    os.execv inherits open FDs, including werkzeug's listening socket, which
    would keep holding the port and make the fresh bind fail with 'Address
    already in use'. Mark every non-std FD close-on-exec so the socket is
    released as the new image loads and can rebind cleanly. Shared by the idle
    live-reload loop and the manual POST /api/server/restart."""
    import fcntl
    for fd in range(3, 4096):
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFD)
            fcntl.fcntl(fd, fcntl.F_SETFD, flags | fcntl.FD_CLOEXEC)
        except OSError:
            pass
    # Prefer sys.orig_argv (Py3.10+): preserves the exact original launch,
    # incl. '-m seiswork.web.app' for the viewer child. Re-running via a bare
    # 'python <app.py path>' would drop the package root from sys.path,
    # raising "ModuleNotFoundError: No module named 'seiswork'". Fall back for older Pythons.
    argv = list(getattr(sys, "orig_argv", None) or
                [sys.executable, sys.argv[0]] + sys.argv[1:])
    os.execv(argv[0], argv)


def _watch_py_and_restart():
    """Re-exec the server when a .py source changes, but only while idle, so a
    running job is never killed (the change is applied once jobs drain)."""
    import time as _t
    baseline = _latest_mtime([_PKG_DIR], {".py"})
    while True:
        _t.sleep(2.0)
        try:
            cur = _latest_mtime([_PKG_DIR], {".py"})
        except Exception:
            continue
        if cur <= baseline:
            continue
        if _server_busy():
            continue                            # defer: re-check next tick
        print("[live-reload] Python source changed & server idle — restarting.",
              flush=True)
        try:
            _reexec_server()
        except Exception as e:
            print(f"[live-reload] re-exec failed ({e}); please restart manually.",
                  flush=True)
            baseline = cur                      # avoid a tight retry loop


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_base_config() -> dict:
    if CFG_FILE.exists():
        with open(CFG_FILE) as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _parse_station_text(content: str) -> list[dict]:
    """Parse station data from CSV or pipe-delimited text.

    Accepted column order (with or without header):
      network | station | latitude | longitude | elevation
    Separator: | or ,  (auto-detected).
    """
    stations: list[dict] = []
    sep = "|" if content.count("|") > content.count(",") else ","
    reader = csv.reader(io.StringIO(content), delimiter=sep)
    col_map: dict | None = None

    for row in reader:
        row = [c.strip() for c in row]
        if not row or not row[0] or row[0].startswith("#"):
            continue

        # Header detection: contains alphabetical station/lat keywords
        if col_map is None:
            lower = [c.lower() for c in row]
            if any(k in lower for k in ("lat", "latitude", "network", "net", "station", "sta")):
                # Map header names to column indices
                aliases = {
                    "network"   : ("network", "net"),
                    "station"   : ("station", "sta", "code"),
                    "lat"       : ("latitude", "lat"),
                    "lon"       : ("longitude", "lon", "long"),
                    "elev"      : ("elevation", "elev", "alt", "depth"),
                    "start_time": ("starttime", "start_time", "ontime", "start"),
                    "end_time"  : ("endtime", "end_time", "offtime", "end"),
                }
                col_map = {}
                for field, keys in aliases.items():
                    for k in keys:
                        if k in lower:
                            col_map[field] = lower.index(k)
                            break
                continue
            else:
                col_map = {}  # positional: net, sta, lat, lon[, elev]

        try:
            if col_map:
                net  = row[col_map.get("network",  0)]
                sta  = row[col_map.get("station",  1)]
                lat  = float(row[col_map.get("lat", 2)])
                lon  = float(row[col_map.get("lon", 3)])
                elev = float(row[col_map.get("elev", 4)]) if "elev" in col_map and len(row) > col_map["elev"] else (float(row[4]) if len(row) > 4 else 0.0)
            else:
                net, sta = row[0], row[1]
                lat, lon = float(row[2]), float(row[3])
                elev     = float(row[4]) if len(row) > 4 else 0.0
            stations.append({
                "network"   : net, "station": sta,
                "lat"       : lat, "lon": lon, "elev": elev,
                "start_time": row[col_map["start_time"]].strip() if "start_time" in col_map and len(row) > col_map["start_time"] else "",
                "end_time"  : row[col_map["end_time"]].strip()   if "end_time"   in col_map and len(row) > col_map["end_time"]   else "",
            })
        except (ValueError, IndexError):
            continue

    return stations


def _parse_fdsn_text(content: str) -> list[dict]:
    """Parse FDSN station text response (pipe-delimited).

    Standard FDSN level=station columns:
      Network|Station|Latitude|Longitude|Elevation|SiteName|StartTime|EndTime
    """
    stations: list[dict] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        try:
            sta = {
                "network"   : parts[0],
                "station"   : parts[1],
                "lat"       : float(parts[2]),
                "lon"       : float(parts[3]),
                "elev"      : float(parts[4]) if len(parts) > 4 else 0.0,
                "start_time": parts[6] if len(parts) > 6 else "",
                "end_time"  : parts[7] if len(parts) > 7 else "",
            }
            stations.append(sta)
        except (ValueError, IndexError):
            continue
    return stations


# ── Routes ─────────────────────────────────────────────────────────────────────

def _slugify_cfg(name: str) -> str:
    """Config name to URL slug (e.g. 'raspyshake' -> 'raspyshake', 'PALU-SIGI'
    -> 'palu-sigi'). Used for the per-config mirror URL (3346/<slug>)."""
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


def _resolve_cfg_ref(ref: str):
    """ref from the URL (cfg_id or name slug) -> (cfg_id, name, meta) | None."""
    ref = (ref or "").strip()
    if not ref:
        return None
    # 1) exact cfg_id match (folder exists)
    d = CONFIGS_DIR / ref
    if d.is_dir() and (d / "meta.json").exists():
        try:
            m = json.loads((d / "meta.json").read_text())
            return (ref, m.get("name", ref), m)
        except Exception:
            pass
    # 2) match the config-name slug: exact first, then prefix (e.g. 'raspy' ->
    #    'raspyshake'). A unique prefix wins; with several matches, take the first (sorted).
    want = _slugify_cfg(ref)
    if not want or not CONFIGS_DIR.is_dir():
        return None
    exact = None
    prefix = []
    for dd in sorted(CONFIGS_DIR.iterdir()):
        mf = dd / "meta.json"
        if not (dd.is_dir() and mf.exists()):
            continue
        try:
            m = json.loads(mf.read_text())
        except Exception:
            continue
        slug = _slugify_cfg(m.get("name", ""))
        if slug == want or dd.name == ref:
            exact = (dd.name, m.get("name", dd.name), m)
            break
        if slug.startswith(want):
            prefix.append((dd.name, m.get("name", dd.name), m))
    if exact:
        return exact
    return prefix[0] if prefix else None


_CFG_NET_CACHE: dict = {}   # inventory_path -> (mtime, {"networks":[...], "stations":[...]})


def _inventory_meta(inv_path: str) -> dict:
    """Networks + stations (NET.STA) from an inventory (FDSN StationXML), via a
    fast line-by-line scan (tracks the active <Network>, collects NET.STA per
    <Station>). Used by the per-config mirror to filter events to this config's stations. Cached by mtime."""
    empty = {"networks": [], "stations": []}
    if not inv_path:
        return empty
    p = Path(inv_path)
    if not p.is_file():
        return empty
    try:
        mt = p.stat().st_mtime
    except OSError:
        return empty
    hit = _CFG_NET_CACHE.get(inv_path)
    if hit and hit[0] == mt:
        return hit[1]
    nets: set[str] = set()
    stas: set[str] = set()
    cur_net = ""
    try:
        with p.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "<Network" in line:
                    m = re.search(r'<Network[^>]*\bcode="([^"]+)"', line)
                    if m:
                        cur_net = m.group(1); nets.add(cur_net)
                if "<Station" in line and cur_net:
                    sm = re.search(r'<Station[^>]*\bcode="([^"]+)"', line)
                    if sm:
                        stas.add(f"{cur_net}.{sm.group(1)}")
    except Exception:
        pass
    out = {"networks": sorted(nets), "stations": sorted(stas)}
    _CFG_NET_CACHE[inv_path] = (mt, out)
    return out


def _render_index(cfg_ref: str | None = None):
    cfg_id = cfg_name = ""
    nets: list[str] = []
    stas: list[str] = []
    if cfg_ref:
        resolved = _resolve_cfg_ref(cfg_ref)
        if resolved:
            cfg_id, cfg_name, meta = resolved
            im = _inventory_meta(meta.get("inventory_path")
                                 or meta.get("inventory") or "")
            nets, stas = im["networks"], im["stations"]
    return render_template("index.html",
                           asset_version=_asset_version(),
                           version=SERVER_VERSION,
                           live_reload=LIVE_RELOAD,
                           viewer_mode=VIEWER_MODE,
                           viewer_upstream=VIEWER_UPSTREAM,
                           viewer_cfg_id=cfg_id,
                           viewer_cfg_name=cfg_name,
                           viewer_cfg_networks=nets,
                           viewer_cfg_stations=stas,
                           mirror_port=VIEWER_PORT)


@app.route("/")
def index():
    return _render_index()


@app.route("/<cfg_ref>")
def index_cfg(cfg_ref):
    """Per-config mirror: /<name-slug> or /<cfg_id> (e.g. 3346/raspy).
    When ref is unrecognized, still render the normal app (graceful)."""
    return _render_index(cfg_ref)


@app.route("/api/base-config")
def base_config():
    cfg = _load_base_config()
    region = cfg.get("region", {})
    return jsonify({
        "name"     : region.get("name", ""),
        "lat"      : region.get("lat",      0.0),
        "lon"      : region.get("lon",      0.0),
        "lat_min"  : region.get("lat_min", -10.0),
        "lat_max"  : region.get("lat_max",   5.0),
        "lon_min"  : region.get("lon_min", 120.0),
        "lon_max"  : region.get("lon_max", 135.0),
        "starttime": region.get("starttime", ""),
        "endtime"  : region.get("endtime",   ""),
    })


@app.route("/api/configs", methods=["GET"])
def list_configs():
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    configs = []
    for d in sorted(CONFIGS_DIR.iterdir(),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        meta_file = d / "meta.json"
        if d.is_dir() and meta_file.exists():
            try:
                configs.append(json.loads(meta_file.read_text()))
            except json.JSONDecodeError:
                pass
    return jsonify(configs)


@app.route("/api/configs", methods=["POST"])
def create_config():
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    data = request.get_json(force=True)

    config_id  = str(uuid.uuid4())[:8]
    config_dir = CONFIGS_DIR / config_id
    config_dir.mkdir()

    stations = data.get("stations", [])
    meta = {
        "id"       : config_id,
        "name"     : data.get("name") or f"Config {config_id}",
        "created"  : datetime.now().isoformat(timespec="seconds"),
        "region"   : data.get("region", {}),
        "stations" : stations,
        "fdsn_url" : data.get("fdsn_url", ""),
        "waveform" : data.get("waveform", {}),
        "n_stations": len(stations),
    }
    (config_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    if stations:
        with open(config_dir / "stations.csv", "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["network","station","lat","lon","elev","start_time","end_time"],
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(stations)

    return jsonify({"id": config_id, "message": "Config saved"})


@app.route("/api/configs/<config_id>", methods=["GET"])
def get_config(config_id):
    meta_file = CONFIGS_DIR / config_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": "Config not found"}), 404
    return jsonify(json.loads(meta_file.read_text()))


@app.route("/api/configs/<config_id>", methods=["PUT"])
def update_config(config_id):
    """Overwrite an existing config (preserve ID and created timestamp)."""
    config_dir = CONFIGS_DIR / config_id
    if not config_dir.exists():
        return jsonify({"error": f"Config {config_id} not found"}), 404
    data = request.get_json(force=True)
    meta_file = config_dir / "meta.json"
    try:
        existing = json.loads(meta_file.read_text())
    except Exception:
        existing = {}
    stations = data.get("stations", [])
    region   = data.get("region", {})
    meta = {
        "id"        : config_id,
        "name"      : data.get("name") or existing.get("name", config_id),
        "created"   : existing.get("created", datetime.now().isoformat(timespec="seconds")),
        "updated"   : datetime.now().isoformat(timespec="seconds"),
        "region"    : region,
        "stations"  : stations,
        "fdsn_url"  : data.get("fdsn_url", ""),
        "waveform"  : data.get("waveform", {}),
        "n_stations": len(stations),
    }
    meta_file.write_text(json.dumps(meta, indent=2))
    if stations:
        with open(config_dir / "stations.csv", "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["network","station","lat","lon","elev","start_time","end_time"],
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(stations)
    return jsonify({"id": config_id, "message": "Updated"})


@app.route("/api/configs/<config_id>", methods=["DELETE"])
def delete_config(config_id):
    config_dir = CONFIGS_DIR / config_id
    if config_dir.exists():
        shutil.rmtree(config_dir)
    return jsonify({"message": "Deleted"})


@app.route("/api/configs/<config_id>/sync-token", methods=["GET"])
def get_cfg_sync_token(config_id):
    """Reveal this project's federation sync token (give this to a subscriber
    so they can pull only this project via /api/sync/*). Only revealed to a
    localhost caller (the operator on the server machine) or one who already
    holds it, same reveal rule as /api/server-info, scoped to one project."""
    if not (CONFIGS_DIR / config_id / "meta.json").exists():
        return jsonify({"error": f"Config {config_id} not found"}), 404
    token    = _cfg_sync_token(config_id)
    remote   = (request.remote_addr or "")
    is_local = remote in ("127.0.0.1", "::1") or remote.startswith("127.")
    has_tok  = secrets.compare_digest(_request_token(), token)
    if not (is_local or has_tok):
        return jsonify({"error": "only from server machine or with the current token"}), 403
    return jsonify({"cfg_id": config_id, "sync_token": token})


@app.route("/api/configs/<config_id>/sync-token", methods=["POST"])
def regen_cfg_sync_token(config_id):
    """Regenerate this project's federation sync token; invalidates the old
    one immediately, revoking any subscriber that was using it."""
    if not (CONFIGS_DIR / config_id / "meta.json").exists():
        return jsonify({"error": f"Config {config_id} not found"}), 404
    old_token = _cfg_sync_token(config_id)
    remote    = (request.remote_addr or "")
    is_local  = remote in ("127.0.0.1", "::1") or remote.startswith("127.")
    has_tok   = secrets.compare_digest(_request_token(), old_token)
    if not (is_local or has_tok):
        return jsonify({"error": "only from server machine or with the current token"}), 403
    new = secrets.token_urlsafe(18)
    (CONFIGS_DIR / config_id / ".sync_token").write_text(new)
    return jsonify({"cfg_id": config_id, "sync_token": new})


# ── Coverage helpers ──────────────────────────────────────────────

def _sds_station_presence(sds_root: Path, net: str, sta: str, start_y: int, end_y: int) -> set:
    """Scan SDS archive; return set of (year, doy) tuples that have at least one file."""
    present: set = set()
    for year in range(start_y, end_y + 1):
        sta_dir = sds_root / str(year) / net / sta
        if not sta_dir.exists():
            continue
        for cha_dir in sta_dir.iterdir():
            if not cha_dir.is_dir():
                continue
            for f in cha_dir.iterdir():
                parts = f.name.rsplit(".", 2)
                if len(parts) == 3:
                    try:
                        present.add((int(parts[1]), int(parts[2])))
                    except ValueError:
                        pass
    return present


def _flat_station_presence(folder: Path, net: str, sta: str) -> set:
    """Scan flat MiniSEED folder; return set of (year, doy) tuples.

    FDSN download saves per-chunk files as YEAR.DOY.HHMMSS.mseed; all stations
    are combined in one file, so any file covering a day means all stations have data.
    """
    present: set = set()
    if not folder.exists():
        return present
    for f in folder.iterdir():
        if not f.is_file():
            continue
        parts = f.name.split(".")
        if len(parts) >= 3:
            try:
                year = int(parts[0])
                doy  = int(parts[1])
                if 1970 <= year <= 2100 and 1 <= doy <= 366:
                    present.add((year, doy))
            except ValueError:
                pass
    return present


@app.route("/api/configs/<config_id>/coverage", methods=["GET"])
def config_coverage(config_id):
    """Scan waveform archive and return day/week-level coverage matrix per station."""
    meta_file = CONFIGS_DIR / config_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": "Config not found"}), 404

    meta     = json.loads(meta_file.read_text())
    region   = meta.get("region", {})
    waveform = meta.get("waveform", {})
    stations = meta.get("stations", [])

    start_str = (region.get("starttime") or "")[:10]
    end_str   = (region.get("endtime")   or "")[:10]
    empty = {"granularity":"day","n_periods":0,"labels":[],"month_marks":[],
             "coverage":{},"has_path":False,"data_path":"","path_type":"sds","pct_present":0.0,"n_stations":0}
    if not start_str or not end_str:
        return jsonify(empty), 200

    from datetime import date as _date, timedelta as _td
    try:
        start_d = _date.fromisoformat(start_str)
        end_d   = _date.fromisoformat(end_str)
    except ValueError:
        return jsonify(empty), 200

    days_total = (end_d - start_d).days + 1
    if days_total > 365:
        gran = "week"
        start_mon = start_d - _td(days=start_d.weekday())
        periods: list = []
        cur = start_mon
        while cur <= end_d:
            periods.append(cur); cur += _td(weeks=1)
    else:
        gran = "day"
        periods = [start_d + _td(days=i) for i in range(days_total)]

    path      = waveform.get("path", "")
    path_type = waveform.get("path_type", "sds")
    fdsn      = waveform.get("fdsn", {})
    data_path = path or fdsn.get("output_path", "")
    has_path  = bool(data_path and Path(data_path).exists())

    from datetime import timedelta as _td2
    coverage: dict = {}
    for sta_info in stations:
        net = sta_info.get("network", "")
        sta = sta_info.get("station", "")
        key = f"{net}.{sta}"
        if not has_path:
            coverage[key] = [0] * len(periods)
            continue
        dpath   = Path(data_path)
        if path_type == "sds":
            present = _sds_station_presence(dpath, net, sta, start_d.year, end_d.year)
        else:
            present = _flat_station_presence(dpath, net, sta)
        vals: list = []
        for p in periods:
            if gran == "week":
                in_range = [p + _td2(days=d) for d in range(7) if p + _td2(days=d) <= end_d]
                found    = sum(1 for d in in_range if (d.year, d.timetuple().tm_yday) in present)
                vals.append(round(found / len(in_range), 2) if in_range else 0.0)
            else:
                vals.append(1.0 if (p.year, p.timetuple().tm_yday) in present else 0.0)
        coverage[key] = vals

    month_marks: list = []
    seen_m: set = set()
    for i, p in enumerate(periods):
        mk = (p.year, p.month)
        if mk not in seen_m:
            seen_m.add(mk)
            month_marks.append({"idx": i, "label": p.strftime("%b %Y")})

    total_n   = sum(len(v) for v in coverage.values())
    present_n = sum(1 for v in coverage.values() for x in v if x > 0)
    pct       = round(present_n / total_n * 100, 1) if total_n else 0.0

    return jsonify({
        "granularity" : gran,
        "n_periods"   : len(periods),
        "labels"      : [p.isoformat() for p in periods],
        "month_marks" : month_marks,
        "coverage"    : coverage,
        "has_path"    : has_path,
        "data_path"   : str(data_path),
        "path_type"   : path_type,
        "pct_present" : pct,
        "n_stations"  : len(stations),
    })


@app.route("/api/stations/upload", methods=["POST"])
def upload_stations():
    if "file" not in request.files:
        return jsonify({"error": "No file attached"}), 400
    fobj    = request.files["file"]
    content = fobj.read().decode("utf-8", errors="replace")
    stations = _parse_station_text(content)
    if not stations:
        return jsonify({"error": "No station data could be parsed. "
                                  "Check file format (net|sta|lat|lon[|elev])."}), 400
    return jsonify({"stations": stations, "count": len(stations)})


@app.route("/api/footages")
def list_footages():
    if not FOOTAGES_DIR.exists():
        return jsonify([])
    files = sorted(f.name for f in FOOTAGES_DIR.glob("*.geojson"))
    return jsonify(files)


@app.route("/footages/<path:filename>")
def serve_footage(filename):
    # Prevent path traversal
    if ".." in filename or filename.startswith("/"):
        return jsonify({"error": "Invalid filename"}), 400
    filepath = (FOOTAGES_DIR / filename).resolve()
    if not str(filepath).startswith(str(FOOTAGES_DIR)) or not filepath.exists():
        return jsonify({"error": "Not found"}), 404
    return send_file(filepath, mimetype="application/json")


@app.route("/api/stations/fdsn")
def get_stations_fdsn():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Parameter 'url' is required"}), 400

    # Force text format for easy parsing. FDSNWS only allows format=text for
    # level=network/station/channel; level=response requires format=xml, so
    # downgrade it to level=station (only coordinates are needed here anyway)
    # to avoid a 400 from the server when a pasted URL still has level=response.
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode  # noqa: PLC0415
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query))
    if q.get("level") == "response":
        q["level"] = "station"
    q["format"] = "text"
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))

    try:
        resp = requests.get(url, timeout=30, headers={"Accept": "text/plain"})
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout: FDSN server did not respond"}), 504
    except requests.exceptions.HTTPError as e:
        return jsonify({"error": f"HTTP {resp.status_code}: {e}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    stations = _parse_fdsn_text(resp.text)
    if not stations:
        return jsonify({"error": "FDSN response contains no valid station data."}), 400
    return jsonify({"stations": stations, "count": len(stations)})


# ── Waveform download ─────────────────────────────────────────────────────────

def _normalize_fdsn_url(url: str) -> str:
    """Return base URL only; strips /fdsnws/... path if a full URL is given."""
    url = url.strip()
    if url.startswith("http") and "/fdsnws/" in url:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}"
    return url


def _fdsn_download_worker(job_id: str, params: dict, username: str = "", password: str = "") -> None:
    """Background thread: SDS-aware FDSN waveform download.

    Strategy (per-station-day):
      1. Fetch inventory to determine expected channels per station
         (priority: SH > BH > HH > EH, best loc prefers "00")
      2. Pre-check: skip station-days where all expected SDS files already exist (>1 KB)
      3. Download one request per station-day with channel=SH?,BH?,HH?,EH? + location=*;
         the server returns whatever is available (no hard-coded channel/loc assumption)
      4. Split returned Stream by (loc, channel), save each trace to correct SDS path

    This way: if SH is unavailable, BH is returned automatically; if loc differs
    from inventory, the actual loc from the trace is used, so no silent data loss.

    Output: SDS archive  {output_path}/YYYY/NET/STA/CHA.D/NET.STA.LOC.CHA.D.YYYY.DOY
    """
    import concurrent.futures                                             # noqa: PLC0415
    import random                                                         # noqa: PLC0415
    import threading as _threading                                        # noqa: PLC0415
    import time as _time                                                  # noqa: PLC0415
    from collections import defaultdict                                   # noqa: PLC0415
    from obspy import UTCDateTime, Stream                                 # noqa: PLC0415
    from obspy.clients.fdsn import Client                                 # noqa: PLC0415
    from obspy.clients.fdsn.header import FDSNNoDataException             # noqa: PLC0415
    try:
        from obspy.clients.fdsn.header import FDSNUnauthorizedException   # noqa: PLC0415
    except ImportError:
        FDSNUnauthorizedException = None

    job_dir     = TMP_DIR / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    status_file = job_dir / "status.json"
    _lock       = _threading.Lock()
    _cancel     = _threading.Event()

    safe_params = {k: v for k, v in params.items() if k not in ("username", "password")}

    def _write(s: dict) -> None:
        status_file.write_text(json.dumps(s, indent=2))
        _jobs[job_id] = dict(s)

    t1        = UTCDateTime(params["starttime"])
    t2        = UTCDateTime(params["endtime"])
    n_workers = min(max(1, int(params.get("parallel_workers", 4))), 16)

    out_str = (params.get("output_path") or "").strip()
    out_dir = Path(out_str) if out_str else WORK_DIR / "waveforms" / job_id
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    status: dict = {
        "id": job_id, "state": "running",
        "total": 0, "done": 0, "errors": 0, "skipped": 0,
        "current": "Fetching inventory...", "output_path": str(out_dir),
        "started": datetime.now().isoformat(timespec="seconds"),
        "params": safe_params,
        "parallel_workers": n_workers,
    }
    _write(status)

    try:
        client_str = _normalize_fdsn_url(params.get("client") or "IRIS")
        kw: dict = {}
        if username:
            kw["user"] = username
            kw["password"] = password
        if client_str.startswith("http"):
            kw["service_mappings"] = {
                "dataselect": client_str.rstrip("/") + "/fdsnws/dataselect/1/",
                "station":    client_str.rstrip("/") + "/fdsnws/station/1/",
            }

        def _make_client() -> "Client":
            return Client(client_str, **kw)

        # ── Step 1: fetch inventory ────────────────────────────────────────────
        networks    = params.get("networks", "*") or "*"
        stations    = params.get("stations", "*") or "*"
        chan_config = params.get("channels", "HH?,BH?,EH?,SH?") or "HH?,BH?,EH?,SH?"
        lat_min     = params.get("lat_min")
        lat_max     = params.get("lat_max")
        lon_min     = params.get("lon_min")
        lon_max     = params.get("lon_max")

        try:
            inv = _make_client().get_stations(
                network=networks, station=stations, channel=chan_config,
                starttime=t1, endtime=t2,
                minlatitude=lat_min, maxlatitude=lat_max,
                minlongitude=lon_min, maxlongitude=lon_max,
                level="channel",
            )
        except Exception as e:
            status["state"] = "error"
            status["error"] = f"get_stations failed: {e}"
            _write(status)
            return

        # ── Step 2: build per-station-day tasks ────────────────────────────────
        # For pre-check only: determine which files are expected from inventory
        # (priority SH>BH>HH>EH + best loc).  Actual download uses wildcards so
        # the server decides which channels/locs it has.
        PREF_ORDER = ["SH", "BH", "HH", "EH"]
        tasks:    list = []
        n_skipped: int = 0

        status["current"] = "Building task list from inventory..."
        _write(status)

        for net in inv:
            for sta in net:
                # Group channels by 2-char prefix, collect all locs per prefix
                groups: dict = {}
                for cha in sta:
                    pfx = cha.code[:2]
                    groups.setdefault(pfx, []).append(cha)

                if not groups:
                    continue

                # Expected files for pre-check: use priority prefix + best loc
                sel_pfx  = next((p for p in PREF_ORDER if p in groups), next(iter(groups)))
                locs     = list({c.location_code for c in groups[sel_pfx]})
                best_loc = "00" if "00" in locs else (locs[0] if locs else "")
                exp_chans = sorted({
                    c.code for c in groups[sel_pfx] if c.location_code == best_loc
                })

                cur = UTCDateTime(t1.year, t1.month, t1.day)
                while cur < t2:
                    day_start = UTCDateTime(cur.year, cur.month, cur.day)
                    chunk_end = min(day_start + 86399.999999, t2)
                    yr  = str(cur.year)
                    doy = str(cur.julday).zfill(3)

                    # Pre-check: all expected channel files present?
                    expected = [
                        out_dir / yr / net.code / sta.code
                        / f"{ch}.D" / f"{net.code}.{sta.code}.{best_loc}.{ch}.D.{yr}.{doy}"
                        for ch in exp_chans
                    ]
                    all_ok = expected and all(
                        f.exists() and f.stat().st_size > 1024 for f in expected
                    )
                    if all_ok:
                        n_skipped += 1
                    else:
                        tasks.append({
                            "net": net.code, "sta": sta.code,
                            "chan_filter": chan_config,
                            "t_start": day_start, "t_end": chunk_end,
                            "yr": yr, "doy": doy,
                        })
                    cur = day_start + 86400

        status["total"]   = len(tasks)
        status["skipped"] = n_skipped
        status["current"] = f"{len(tasks)} station-days to download, {n_skipped} already present"
        _write(status)

        if not tasks:
            status["state"]   = "done"
            status["current"] = f"All data present — SDS: {out_dir}"
            _write(status)
            return

        # ── Step 3: parallel download, split stream into SDS files ───────────
        _tlocal = _threading.local()

        def _get_client():
            if not hasattr(_tlocal, "client"):
                _tlocal.client = _make_client()
            return _tlocal.client

        def _worker(task):
            if _cancel.is_set():
                return ("cancelled", 0)
            net, sta = task["net"], task["sta"]
            ts, te   = task["t_start"], task["t_end"]
            yr, doy  = task["yr"], task["doy"]

            for attempt in range(3):
                try:
                    st = _get_client().get_waveforms(
                        network=net, station=sta,
                        location="*",
                        channel=task["chan_filter"],
                        starttime=ts, endtime=te,
                    )
                    if not st:
                        return ("nodata", 0)

                    # Group traces by (location, channel) to merge gaps
                    grp: dict = defaultdict(Stream)
                    for tr in st:
                        grp[(tr.stats.location or "", tr.stats.channel)].append(tr)

                    saved = 0
                    for (loc, cha), sub_st in grp.items():
                        sub_st.merge(method=1, fill_value=0)
                        sds_dir  = out_dir / yr / net / sta / f"{cha}.D"
                        sds_dir.mkdir(parents=True, exist_ok=True)
                        sds_file = sds_dir / f"{net}.{sta}.{loc}.{cha}.D.{yr}.{doy}"
                        sub_st.write(str(sds_file), format="MSEED")
                        saved += 1
                    return ("dl", saved)

                except FDSNNoDataException:
                    return ("nodata", 0)
                except Exception as exc:
                    if hasattr(_tlocal, "client"):
                        del _tlocal.client
                    if FDSNUnauthorizedException and isinstance(exc, FDSNUnauthorizedException):
                        _cancel.set()
                        return ("auth_error", 0)
                    msg = str(exc)
                    if "401" in msg or "Unauthorized" in msg or "unauthorized" in msg:
                        _cancel.set()
                        return ("auth_error", 0)
                    is_rate = "429" in msg or "too many" in msg.lower()
                    if is_rate and attempt < 2:
                        _time.sleep(1.0 * (2 ** attempt) + random.uniform(0, 1))
                        continue
                    if attempt == 2:
                        return (f"err:{msg[:80]}", 0)
            return ("err", 0)

        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
            futs = {executor.submit(_worker, t): t for t in tasks}
            for fut in concurrent.futures.as_completed(futs):
                task_info = futs[fut]
                try:
                    res, n_saved = fut.result()
                except Exception as exc:
                    res, n_saved = f"err:{exc}", 0
                with _lock:
                    if res == "auth_error":
                        status["state"] = "auth_error"
                        status["error"] = "Authentication required or credentials invalid"
                        _write(status)
                        return
                    if _jobs.get(job_id, {}).get("state") == "stopped":
                        _cancel.set()
                    if res.startswith("err:"):
                        status["errors"] += 1
                        print(f"[wv:{job_id}] {task_info['sta']} "
                              f"{task_info['yr']}.{task_info['doy']}: {res}")
                    if res != "cancelled":
                        status["done"] += 1
                    status["current"] = (
                        f"{task_info['sta']} {task_info['yr']}.{task_info['doy']}"
                        + (f" → {n_saved} ch" if n_saved else "")
                    )
                    _write(status)

        with _lock:
            if status["state"] == "running":
                status["state"]   = "stopped" if _cancel.is_set() else "done"
                status["current"] = f"SDS path: {out_dir}"
                _write(status)
    except Exception as exc:
        status["state"] = "error"
        status["error"] = str(exc)
        _write(status)


@app.route("/api/waveform/test-path", methods=["POST"])
def test_waveform_path():
    data      = request.get_json(force=True)
    path_str  = (data.get("path") or "").strip()
    path_type = data.get("type", "sds")
    if not path_str:
        return jsonify({"error": "Empty path"}), 400
    p = Path(path_str)
    try:
        exists = p.exists()
    except PermissionError:
        return jsonify({"error": f"Permission denied: {path_str} — user does not have read access to this folder"}), 403
    if not exists:
        return jsonify({"error": f"Not found: {path_str}"}), 404
    if not p.is_dir():
        return jsonify({"error": "Not a directory"}), 400
    try:
        if path_type == "sds":
            files = [f for f in p.rglob("*") if f.is_file() and len(f.name.split(".")) == 7]
        else:
            files = [f for ext in ("*.mseed", "*.ms", "*.MiniSEED") for f in p.rglob(ext) if f.is_file()]
        total_bytes = sum(f.stat().st_size for f in files)
    except PermissionError:
        return jsonify({"error": f"Permission denied: cannot read contents of {path_str}"}), 403
    return jsonify({"exists": True, "n_files": len(files),
                    "total_size_mb": round(total_bytes / (1024 ** 2), 1)})


@app.route("/api/waveform/test-client")
def test_wv_client():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Empty URL"}), 400
    base = _normalize_fdsn_url(url)
    # Short code (IRIS, GEOFON, etc): check via ObsPy without making network calls
    if not base.startswith("http"):
        from obspy.clients.fdsn.header import URL_MAPPINGS  # noqa: PLC0415
        if base.upper() not in URL_MAPPINGS:
            return jsonify({"error": f"Unknown client code: {base}"}), 400
        return jsonify({"ok": True, "base_url": URL_MAPPINGS[base.upper()], "type": "code"})
    # Full URL: hit the dataselect version endpoint
    try:
        resp = requests.get(base.rstrip("/") + "/fdsnws/dataselect/1/version", timeout=10)
        if resp.ok:
            return jsonify({"ok": True, "base_url": base, "version": resp.text.strip()})
        return jsonify({"error": f"HTTP {resp.status_code} from server"}), 400
    except requests.exceptions.Timeout:
        return jsonify({"error": "Timeout — server did not respond"}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/waveform/download", methods=["POST"])
def start_wv_download():
    params = request.get_json(force=True)
    if not params.get("client"):
        return jsonify({"error": "client is required"}), 400
    if not params.get("starttime") or not params.get("endtime"):
        return jsonify({"error": "starttime and endtime are required"}), 400
    # Credentials are extracted and split off; never kept in the params stored in status
    username = (params.pop("username", "") or "").strip()
    password = params.pop("password", "") or ""
    job_id = str(uuid.uuid4())[:8]
    t = threading.Thread(target=_fdsn_download_worker, args=(job_id, params, username, password), daemon=True)
    _job_threads[job_id] = t
    t.start()
    return jsonify({"id": job_id, "state": "running"})


@app.route("/api/waveform/jobs", methods=["GET"])
def list_wv_jobs():
    jobs_dir = TMP_DIR / "jobs"
    result: list[dict] = []
    if jobs_dir.exists():
        for d in sorted(jobs_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            sf = d / "status.json"
            if sf.is_file():
                try:
                    result.append(json.loads(sf.read_text()))
                except Exception:
                    pass
    return jsonify(result)


@app.route("/api/waveform/jobs/<job_id>", methods=["DELETE"])
def stop_wv_job(job_id):
    if job_id in _jobs and _jobs[job_id].get("state") == "running":
        _jobs[job_id]["state"] = "stopped"
        sf = TMP_DIR / "jobs" / job_id / "status.json"
        if sf.is_file():
            try:
                d = json.loads(sf.read_text()); d["state"] = "stopped"
                sf.write_text(json.dumps(d, indent=2))
            except Exception:
                pass
    else:
        job_dir = TMP_DIR / "jobs" / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)
        _jobs.pop(job_id, None)
        _job_threads.pop(job_id, None)
    return jsonify({"message": "ok"})


# ── QuakeLink / FDSN-Event catalog download ────────────────────────────────────

def _xml_to_pha(xml_file: Path) -> Path:
    """Convert QuakeML to HypoDD/VELEST phase format (.pha).
    Output format:
      # YYYY MM DD HH MIN SEC.SS LAT LON DEP MAG EH EZ RMS ID
      STA        TT   WEIGHT  PHASE
    """
    from obspy import read_events as _re

    pha_file = xml_file.with_suffix(".pha")
    catalog  = _re(str(xml_file))

    with open(pha_file, "w") as f:
        ev_id = 1
        for event in catalog:
            if not event.origins:
                continue
            origin = event.preferred_origin() or event.origins[0]
            mag_obj = event.preferred_magnitude()
            mag     = mag_obj.mag if mag_obj and mag_obj.mag is not None else 0.0

            ot  = origin.time.datetime
            sc  = ot.second + ot.microsecond / 1_000_000
            lat = origin.latitude  or 0.0
            lon = origin.longitude or 0.0
            dep = (origin.depth / 1000.0) if origin.depth is not None else 0.0
            rms = 0.0
            if origin.quality and origin.quality.standard_error:
                rms = origin.quality.standard_error

            f.write(
                f"# {ot.year:4d} {ot.month:2d} {ot.day:2d} {ot.hour:2d} {ot.minute:2d} "
                f"{sc:5.2f} {lat:8.4f} {lon:9.4f} {dep:7.2f} {mag:5.2f} "
                f"0.00  0.00 {rms:5.2f} {ev_id:>9}\n"
            )

            pick_map = {p.resource_id: p for p in event.picks}
            for arr in origin.arrivals:
                if arr.phase not in ("P", "S"):
                    continue
                pick = pick_map.get(arr.pick_id)
                if not pick:
                    continue
                tt = pick.time - origin.time
                if tt <= 0:
                    continue
                sta = pick.waveform_id.station_code[:5]
                f.write(f"{sta:<11} {tt:6.3f}   1.000   {arr.phase}\n")

            ev_id += 1

    return pha_file


def _ql_download_worker(job_id: str, params: dict) -> None:
    """Background thread: download catalog XML from QuakeLink / FDSN event."""
    import requests as _req
    from datetime import datetime as _dt

    job_dir = TMP_DIR / "ql_jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    status: dict = {
        "id"      : job_id,
        "state"   : "running",
        # Scope the job to the config session it was launched from so the
        # Overview / download lists only show this config's catalogs (a job
        # downloaded under PALU-SIGI must not leak into another config).
        "cfg_id"  : params.get("cfg_id", ""),
        "params"  : params,
        "started" : _dt.utcnow().isoformat(),
        "downloaded": 0,
        "total_bytes": 0,
        "output_file": "",
    }
    _ql_jobs[job_id] = status

    def _write(s: dict) -> None:
        (job_dir / "status.json").write_text(json.dumps(s, indent=2))
        _ql_jobs[job_id] = dict(s)

    _write(status)

    try:
        base_url = (params.get("base_url") or "").rstrip("/")
        if not base_url:
            raise ValueError("base_url is required")

        event_url = base_url + "/fdsnws/event/1/query"

        def _iso(t: str) -> str:
            return t.strip().replace(" ", "T") if t else ""

        query = {
            "starttime"          : _iso(params.get("starttime", "")),
            "endtime"            : _iso(params.get("endtime", "")),
            "minlatitude"        : params.get("minlatitude", ""),
            "maxlatitude"        : params.get("maxlatitude", ""),
            "minlongitude"       : params.get("minlongitude", ""),
            "maxlongitude"       : params.get("maxlongitude", ""),
            "mindepth"           : params.get("mindepth", ""),
            "maxdepth"           : params.get("maxdepth", ""),
            "minmagnitude"       : params.get("minmagnitude", ""),
            "maxmagnitude"       : params.get("maxmagnitude", ""),
            "magnitudetype"      : params.get("magnitudetype", "M"),
            "minphases"          : params.get("minphases", ""),
            "maxphases"          : params.get("maxphases", ""),
            "includeallorigins"  : "true",
            "includeallmagnitudes": "true",
            "includearrivals"    : "true" if params.get("include_arrivals") else "false",
            "includepicks"       : "true" if params.get("include_arrivals") else "false",
            "includeamps"        : "true",
            "includestamag"      : "true",
            "native"             : "true",
            "format"             : params.get("format", "xml"),
            "formatted"          : "true",
            "nodata"             : "404",
        }
        # strip empty values
        query = {k: v for k, v in query.items() if v != ""}

        r = _req.get(event_url, params=query, stream=True, timeout=180)

        if r.status_code == 404:
            raise RuntimeError("No data (HTTP 404) — no events match criteria")
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

        fmt   = params.get("format", "xml")
        out_p = Path(params.get("output_path", "")).expanduser() if params.get("output_path") else (job_dir / "output")
        out_p.mkdir(parents=True, exist_ok=True)

        ts       = _dt.utcnow().strftime("%Y%m%dT%H%M%S")
        out_file = out_p / f"catalog_{job_id}_{ts}.{fmt}"

        total = int(r.headers.get("Content-Length", 0))
        dl    = 0
        with open(out_file, "wb") as f:
            for chunk in r.iter_content(16384):
                if chunk:
                    f.write(chunk)
                    dl += len(chunk)
                    if total:
                        status["downloaded"]   = dl
                        status["total_bytes"]  = total
                        _write(status)

        status["state"]       = "done"
        status["output_file"] = str(out_file)
        status["downloaded"]  = dl
        status["total_bytes"] = dl
        _write(status)

        # Convert XML to PHA when requested and the format is xml
        if params.get("convert_pha") and fmt == "xml":
            status["state"] = "converting"
            _write(status)
            try:
                pha_file = _xml_to_pha(out_file)
                status["pha_file"] = str(pha_file)
            except Exception as exc_pha:
                status["pha_error"] = str(exc_pha)
            status["state"] = "done"
            _write(status)

    except Exception as exc:
        status["state"] = "error"
        status["error"] = str(exc)
        _write(status)


@app.route("/api/catalog/download", methods=["POST"])
def start_ql_download():
    params = request.get_json(force=True)
    if not params.get("base_url"):
        return jsonify({"error": "base_url is required"}), 400
    if not params.get("starttime") or not params.get("endtime"):
        return jsonify({"error": "starttime and endtime are required"}), 400
    job_id = str(uuid.uuid4())[:8]
    t = threading.Thread(target=_ql_download_worker, args=(job_id, params), daemon=True)
    t.start()
    return jsonify({"id": job_id, "state": "running"})


@app.route("/api/catalog/jobs", methods=["GET"])
def list_ql_jobs():
    jobs_dir = TMP_DIR / "ql_jobs"
    filter_cfg = request.args.get("cfg_id", "").strip()
    if not filter_cfg:
        return jsonify({"error": "cfg_id required"}), 400
    result: list[dict] = []
    if jobs_dir.exists():
        for d in sorted(jobs_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            sf = d / "status.json"
            if sf.is_file():
                try:
                    j = json.loads(sf.read_text())
                    # When a config session is active, only return its own jobs
                    if filter_cfg and j.get("cfg_id", "") != filter_cfg:
                        continue
                    result.append(j)
                except Exception:
                    pass
    return jsonify(result)


@app.route("/api/catalog/jobs/<job_id>", methods=["DELETE"])
def remove_ql_job(job_id):
    job_dir = TMP_DIR / "ql_jobs" / job_id
    if job_dir.exists():
        shutil.rmtree(job_dir)
    _ql_jobs.pop(job_id, None)
    return jsonify({"message": "ok"})


_BED = "http://quakeml.org/xmlns/bed/1.2"


def _xml_fast_parse_events(xml_file: Path) -> list[dict]:
    """Parse QuakeML using ElementTree (70x faster than ObsPy).
    Only reads origin + magnitude; skips picks/arrivals/waveforms.
    """
    import xml.etree.ElementTree as ET

    B = _BED
    tree = ET.parse(str(xml_file))
    root = tree.getroot()
    events, n = [], 0

    for ev_el in root.iter(f"{{{B}}}event"):
        pref_orig = ev_el.findtext(f"{{{B}}}preferredOriginID") or ""
        pref_mag  = ev_el.findtext(f"{{{B}}}preferredMagnitudeID") or ""

        # preferred origin (or the first origin)
        orig = None
        for o in ev_el.findall(f"{{{B}}}origin"):
            if not pref_orig or o.get("publicID") == pref_orig:
                orig = o; break
        if orig is None:
            orig = ev_el.find(f"{{{B}}}origin")
        if orig is None:
            continue

        ot_raw = orig.findtext(f".//{{{B}}}time/{{{B}}}value") or ""
        try:
            ot = datetime.fromisoformat(ot_raw.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        except Exception:
            ot = ot_raw[:23]

        try:
            lat = float(orig.findtext(f".//{{{B}}}latitude/{{{B}}}value") or "")
        except (ValueError, TypeError):
            continue
        try:
            lon = float(orig.findtext(f".//{{{B}}}longitude/{{{B}}}value") or "")
        except (ValueError, TypeError):
            continue
        try:
            dep = round(float(orig.findtext(f".//{{{B}}}depth/{{{B}}}value") or "") / 1000.0, 2)
        except (ValueError, TypeError):
            dep = 0.0

        rms = None
        try:
            rms = round(float(orig.findtext(f".//{{{B}}}standardError") or ""), 4)
        except (ValueError, TypeError):
            pass

        nsta = None
        try:
            nsta = int(orig.findtext(f".//{{{B}}}usedStationCount") or "")
        except (ValueError, TypeError):
            pass

        # preferred magnitude (or the first magnitude)
        mag = None
        for m_el in ev_el.findall(f"{{{B}}}magnitude"):
            if not pref_mag or m_el.get("publicID") == pref_mag:
                try:
                    mag = round(float(m_el.findtext(f".//{{{B}}}mag/{{{B}}}value") or ""), 3)
                except (ValueError, TypeError):
                    pass
                break

        n += 1
        events.append({
            "event_id": f"ql_{n:06d}",
            "datetime": ot,
            "lat"     : lat,
            "lon"     : lon,
            "depth_km": dep,
            "mag"     : mag,
            "rms"     : rms,
            "nsta"    : nsta,
            "method"  : "QuakeLink",
        })

    return events


def _xml_to_result_events(xml_file: Path) -> list[dict]:
    """Parse QuakeML into events; uses a JSON cache when the XML hasn't changed."""
    cache = xml_file.with_suffix(".events.json")

    # Validate cache by mtime
    if cache.exists():
        try:
            if cache.stat().st_mtime >= xml_file.stat().st_mtime:
                return json.loads(cache.read_text())
        except Exception:
            pass

    events = _xml_fast_parse_events(xml_file)

    try:
        cache.write_text(json.dumps(events, ensure_ascii=False))
    except Exception:
        pass

    return events


@app.route("/api/catalog/as_result/<job_id>", methods=["GET"])
def catalog_as_result(job_id):
    """Convert a QuakeML job into the Result View format (identical to result_catalog)."""
    sf = TMP_DIR / "ql_jobs" / job_id / "status.json"
    if not sf.exists():
        return jsonify({"error": "Job not found"}), 404
    status = json.loads(sf.read_text())
    if status.get("state") != "done":
        return jsonify({"error": f"Job not done (state={status.get('state')})"}), 400
    xml_file = Path(status.get("output_file", ""))
    if not xml_file.exists():
        return jsonify({"error": "Output file not found"}), 404
    if not xml_file.suffix.lower() == ".xml":
        return jsonify({"error": "File is not XML — pick a job with XML format"}), 400
    try:
        events = _xml_to_result_events(xml_file)
        p      = status.get("params", {})
        region = {}
        for key, rk in [("minlatitude","lat_min"),("maxlatitude","lat_max"),
                         ("minlongitude","lon_min"),("maxlongitude","lon_max")]:
            if p.get(key) not in (None, ""):
                try: region[rk] = float(p[key])
                except (ValueError, TypeError): pass
        job = {
            "job_id"  : job_id,
            "step"    : "external",
            "method"  : "QuakeLink",
            "mode"    : "QuakeLink/FDSN",
            "started" : status.get("started", ""),
            "finished": "",
            "n_events": len(events),
            "events"  : events,
            "filtered": False,
        }
        return jsonify({"jobs": [job], "stations": [], "region": region})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# In-memory progress store for catalog stat parsing
_cat_parse: dict[str, dict] = {}


def _catalog_parse_worker(job_id: str, xml_file: Path) -> None:
    """Parse QuakeML in the background (using ElementTree, not ObsPy).
    Updates _cat_parse[job_id] with progress. Caches the result to .stats.json.
    """
    import xml.etree.ElementTree as ET
    from collections import Counter

    prog  = _cat_parse[job_id]
    B     = _BED
    cache = xml_file.with_suffix(".stats.json")

    # Use the cached stats while still valid
    if cache.exists():
        try:
            if cache.stat().st_mtime >= xml_file.stat().st_mtime:
                result = json.loads(cache.read_text())
                _cat_parse[job_id] = {"state": "done", "pct": 100,
                                      "n_events": result.get("n_events", 0),
                                      "result": result}
                return
        except Exception:
            pass

    try:
        prog.update({"state": "reading", "pct": 5, "label": "Membaca file XML…"})
        tree     = ET.parse(str(xml_file))
        root     = tree.getroot()
        ev_els   = list(root.iter(f"{{{B}}}event"))
        n_events = len(ev_els)

        if n_events == 0:
            _cat_parse[job_id] = {"state": "done", "pct": 100,
                                  "result": {"n_events": 0}}
            return

        prog.update({"state": "processing", "pct": 20, "n_events": n_events,
                     "label": f"Memproses {n_events} event…"})

        mags, depths, times_ym, rmss, nphases = [], [], [], [], []

        for i, ev_el in enumerate(ev_els):
            pref_orig = ev_el.findtext(f"{{{B}}}preferredOriginID") or ""
            pref_mag  = ev_el.findtext(f"{{{B}}}preferredMagnitudeID") or ""

            orig = None
            for o in ev_el.findall(f"{{{B}}}origin"):
                if not pref_orig or o.get("publicID") == pref_orig:
                    orig = o; break
            if orig is None:
                orig = ev_el.find(f"{{{B}}}origin")
            if orig is None:
                continue

            # Time -> YYYY-MM
            ot_raw = orig.findtext(f".//{{{B}}}time/{{{B}}}value") or ""
            if len(ot_raw) >= 7:
                times_ym.append(ot_raw[:7])

            # Depth
            try:
                depths.append(float(orig.findtext(f".//{{{B}}}depth/{{{B}}}value") or "") / 1000.0)
            except (ValueError, TypeError):
                pass

            # RMS & phase count
            try:
                rmss.append(float(orig.findtext(f".//{{{B}}}standardError") or ""))
            except (ValueError, TypeError):
                pass
            try:
                nphases.append(int(orig.findtext(f".//{{{B}}}usedPhaseCount") or ""))
            except (ValueError, TypeError):
                pass

            # Magnitude
            for m_el in ev_el.findall(f"{{{B}}}magnitude"):
                if not pref_mag or m_el.get("publicID") == pref_mag:
                    try:
                        mags.append(float(m_el.findtext(f".//{{{B}}}mag/{{{B}}}value") or ""))
                    except (ValueError, TypeError):
                        pass
                    break

            if i % 100 == 0:
                pct = 20 + int(i / n_events * 70)
                prog.update({"pct": pct, "n_done": i + 1,
                             "label": f"Memproses event {i+1}/{n_events}…"})

        # Histogram
        prog.update({"state": "computing", "pct": 92, "label": "Menghitung distribusi…"})

        def _hist(vals, lo, hi, step):
            bins, v = [], lo
            while v < hi + step:
                bins.append({"label": f"{v:.1f}",
                             "count": sum(1 for x in vals if v <= x < v + step)})
                v = round(v + step, 1)
            return bins

        ym_sorted = sorted(set(times_ym))
        ym_counts = Counter(times_ym)

        result = {
            "n_events" : n_events,
            "starttime": min(times_ym) if times_ym else "",
            "endtime"  : max(times_ym) if times_ym else "",
            "mag"  : {
                "min" : round(min(mags), 2)              if mags   else None,
                "max" : round(max(mags), 2)              if mags   else None,
                "mean": round(sum(mags) / len(mags), 2)  if mags   else None,
                "hist": _hist(mags, 0, 8, 0.5),
            },
            "depth": {
                "min" : round(min(depths), 1)               if depths else None,
                "max" : round(max(depths), 1)               if depths else None,
                "mean": round(sum(depths) / len(depths), 1) if depths else None,
                "hist": _hist(depths, 0, (max(depths) + 10) if depths else 700, 10),
            },
            "rms"  : {
                "mean": round(sum(rmss) / len(rmss), 3) if rmss else None,
                "max" : round(max(rmss), 3)              if rmss else None,
            },
            "phases": {
                "mean": round(sum(nphases) / len(nphases), 1) if nphases else None,
                "max" : max(nphases)                           if nphases else None,
            },
            "monthly": [{"ym": ym, "count": ym_counts[ym]} for ym in ym_sorted],
        }

        # Save the stats cache
        try:
            cache.write_text(json.dumps(result, ensure_ascii=False))
        except Exception:
            pass

        _cat_parse[job_id] = {"state": "done", "pct": 100,
                              "n_events": n_events, "result": result}

    except Exception as exc:
        _cat_parse[job_id] = {"state": "error", "pct": 0, "error": str(exc)}


@app.route("/api/catalog/stats/<job_id>/start", methods=["POST"])
def start_catalog_parse(job_id):
    # if already finished and unchanged, return immediately
    existing = _cat_parse.get(job_id, {})
    if existing.get("state") in ("running", "reading", "processing", "computing"):
        return jsonify({"status": "already_running"})

    sf = TMP_DIR / "ql_jobs" / job_id / "status.json"
    if not sf.exists():
        return jsonify({"error": "Job not found"}), 404
    job_status = json.loads(sf.read_text())
    if job_status.get("state") != "done":
        return jsonify({"error": f"Job not done (state={job_status.get('state')})"}), 400
    xml_file = Path(job_status.get("output_file", ""))
    if not xml_file.exists():
        return jsonify({"error": "Output file not found"}), 404

    file_mb = xml_file.stat().st_size / 1024 / 1024
    _cat_parse[job_id] = {"state": "reading", "pct": 0,
                          "label": "Memulai…", "file_mb": round(file_mb, 1)}
    t = threading.Thread(target=_catalog_parse_worker,
                         args=(job_id, xml_file), daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/catalog/stats/<job_id>/progress", methods=["GET"])
def get_catalog_parse_progress(job_id):
    p = _cat_parse.get(job_id)
    if not p:
        return jsonify({"state": "notfound"}), 404
    # do not send the large result while still in progress, only when done
    if p.get("state") != "done":
        return jsonify({k: v for k, v in p.items() if k != "result"})
    return jsonify(p)


# ── Waveform viewer endpoints ──────────────────────────────────────────────────

def _resolve_sds_root(cfg_id: str, fallback: str = "") -> str:
    """Return SDS root path from saved config or fallback arg."""
    if cfg_id:
        meta_file = CONFIGS_DIR / cfg_id / "meta.json"
        if meta_file.exists():
            wv = json.loads(meta_file.read_text()).get("waveform", {})
            if wv.get("path_type", "sds") == "sds":
                return wv.get("path") or wv.get("fdsn", {}).get("output_path", "")
    return fallback


@app.route("/api/waveform/channels")
def get_waveform_channels():
    """List SDS channels available for net.sta on a given date."""
    cfg_id   = request.args.get("cfg_id", "")
    net      = request.args.get("net", "")
    sta      = request.args.get("sta", "")
    date_str = request.args.get("date", "")
    if not (net and sta and date_str):
        return jsonify([])
    sds_root = _resolve_sds_root(cfg_id, request.args.get("path", ""))
    if not sds_root:
        return jsonify([])
    from datetime import date as _date
    try:
        d = _date.fromisoformat(date_str)
    except ValueError:
        return jsonify([])
    doy     = d.timetuple().tm_yday
    sta_dir = Path(sds_root) / str(d.year) / net / sta
    if not sta_dir.exists():
        return jsonify([])
    channels: list[str] = []
    for cha_dir in sorted(sta_dir.iterdir()):
        if not cha_dir.is_dir():
            continue
        cha = cha_dir.name.split(".")[0]   # "HHZ.D" -> "HHZ"
        for f in cha_dir.iterdir():
            parts = f.name.split(".")
            if len(parts) == 7:
                try:
                    if int(parts[5]) == d.year and int(parts[6]) == doy and cha not in channels:
                        channels.append(cha)
                        break
                except ValueError:
                    pass
    return jsonify(sorted(channels))


@app.route("/api/waveform/trace")
def get_waveform_trace():
    """Read an SDS waveform window and return either a per-sample wiggle (when the
    visible window is small enough) or a min/max/rms envelope decimated to px points.

    Query: net, sta, cha, date, px, and an optional visible window t0/t1 as
    fractions of the day (0..1). Reading only [t0,t1] at full px means zooming in
    re-fetches crisp data instead of stretching a handful of full-day bins into
    blocks, and below ~px*3 samples we return the raw samples so the client can
    draw a true wiggle line."""
    import numpy as np
    cfg_id   = request.args.get("cfg_id", "")
    net      = request.args.get("net", "")
    sta      = request.args.get("sta", "")
    cha      = request.args.get("cha", "HH?")
    date_str = request.args.get("date", "")
    # Drum lays 24 hour-rows side by side, each needing ~1 bin/pixel, so the
    # full-day envelope wants ~width*24 bins (tens of thousands). Cap high
    # enough for that, else rows are under-resolved and the waveform pattern is invisible.
    px       = min(int(request.args.get("px", 1800)), 40000)
    try:
        t0 = max(0.0, min(1.0, float(request.args.get("t0", 0.0))))
        t1 = max(0.0, min(1.0, float(request.args.get("t1", 1.0))))
    except Exception:
        t0, t1 = 0.0, 1.0
    if t1 <= t0:
        t0, t1 = 0.0, 1.0

    if not (net and sta and date_str):
        return jsonify({"error": "net, sta, date required"}), 400

    sds_root = _resolve_sds_root(cfg_id, request.args.get("path", ""))
    if not sds_root or not Path(sds_root).exists():
        return jsonify({"error": "Waveform path not configured or not found"}), 404

    try:
        from obspy import UTCDateTime
        day0    = UTCDateTime(date_str + "T00:00:00")
        DAY     = 86400.0
        t_start = day0 + t0 * DAY
        t_end   = day0 + t1 * DAY
        # small pad so edge wiggles aren't clipped
        pad     = (t1 - t0) * DAY * 0.01
    except Exception:
        return jsonify({"error": "Invalid date"}), 400

    try:
        from obspy.clients.filesystem.sds import Client as SDSClient
        st = SDSClient(sds_root).get_waveforms(net, sta, "*", cha,
                                               t_start - pad, t_end + pad)
    except Exception as exc:
        return jsonify({"error": f"Read error: {exc}"}), 500

    if not st:
        return jsonify({"error": f"No data for {net}.{sta}..{cha} on {date_str}"}), 404

    try:
        # Merge gaps with 0-fill and pad to EXACTLY the requested window so the
        # returned series is gapless and maps 1:1 across the plot width (no stale
        # NaN/masked values that would break the wiggle polyline, no stretching).
        st.merge(method=1, fill_value=0)
        st.trim(t_start, t_end, pad=True, fill_value=0)
    except Exception:
        pass

    traces: dict = {}
    raw_mode = False
    for tr in st:
        data = np.nan_to_num(tr.data.astype(np.float32), nan=0.0,
                             posinf=0.0, neginf=0.0)
        n    = len(data)
        if n == 0:
            continue
        sr   = float(tr.stats.sampling_rate)
        entry = {
            "sample_rate": sr,
            "n_samples"  : n,
            "start"      : str(tr.stats.starttime)[:23],
            "end"        : str(tr.stats.endtime)[:23],
            "max_amp"    : float(np.abs(data).max()),
            "mean_rms"   : float(np.sqrt(np.mean(data ** 2))),
        }
        # Zoomed in enough: send raw samples for a true oscillating wiggle line.
        # Generous cap (~2 min @100 Hz) so a moderate zoom already reveals the
        # individual cycles instead of a filled envelope blob.
        if n <= max(px * 4, 12000):
            entry["samples"] = [float(v) for v in data]
            raw_mode = True
        else:
            # Vectorised min/max/rms decimation (fast even for tens of thousands of
            # bins): reshape into equal blocks, reduce per row; handle the remainder
            # tail as one extra bin so no samples are dropped.
            step = max(1, n // px)
            nblk = n // step
            head = data[:nblk * step].reshape(nblk, step)
            mins = head.min(axis=1)
            maxs = head.max(axis=1)
            rms_a = np.sqrt((head.astype(np.float64) ** 2).mean(axis=1))
            if nblk * step < n:
                tail = data[nblk * step:]
                mins = np.append(mins, tail.min())
                maxs = np.append(maxs, tail.max())
                rms_a = np.append(rms_a, np.sqrt(np.mean(tail.astype(np.float64) ** 2)))
            entry.update({"mins": mins.tolist(), "maxs": maxs.tolist(),
                          "rms": rms_a.tolist()})
        traces[tr.stats.channel] = entry

    if not traces:
        return jsonify({"error": "No valid trace data"}), 404

    return jsonify({"net": net, "sta": sta, "date": date_str, "px": px,
                    "t0": t0, "t1": t1, "raw": raw_mode, "traces": traces})


@app.route("/api/waveform/spectrogram")
def get_waveform_spectrogram():
    """Return spectrogram in dB for one channel (max 1-hour window)."""
    import numpy as np
    cfg_id   = request.args.get("cfg_id", "")
    net      = request.args.get("net", "")
    sta      = request.args.get("sta", "")
    cha      = request.args.get("cha", "HHZ")
    date_str = request.args.get("date", "")
    t0_str   = request.args.get("t0", "00:00")
    t1_str   = request.args.get("t1", "01:00")

    if not (net and sta and date_str):
        return jsonify({"error": "net, sta, date required"}), 400

    sds_root = _resolve_sds_root(cfg_id, request.args.get("path", ""))
    if not sds_root or not Path(sds_root).exists():
        return jsonify({"error": "Waveform path not found"}), 404

    try:
        from obspy import UTCDateTime
        t_start = UTCDateTime(f"{date_str}T{t0_str}:00")
        t_end   = UTCDateTime(f"{date_str}T{t1_str}:00")
    except Exception:
        return jsonify({"error": "Invalid date/time"}), 400

    if t_end - t_start > 3600:
        t_end = t_start + 3600

    try:
        from obspy.clients.filesystem.sds import Client as SDSClient
        st = SDSClient(sds_root).get_waveforms(net, sta, "*", cha, t_start, t_end)
    except Exception as exc:
        return jsonify({"error": f"Read error: {exc}"}), 500

    if not st:
        return jsonify({"error": "No data for spectrogram"}), 404

    try:
        st.merge(method=1, fill_value=0)
        tr   = st[0]
        data = tr.data.astype(np.float64)
        sr   = tr.stats.sampling_rate

        from scipy.signal import spectrogram as _spgram
        nperseg  = min(int(sr * 4), 512)
        noverlap = nperseg * 3 // 4
        f, t, Sxx = _spgram(data, fs=sr, nperseg=nperseg, noverlap=noverlap, scaling="density")
        Sxx_db = 10 * np.log10(Sxx + 1e-30)

        f_max  = min(50.0, sr / 2)
        f_mask = f <= f_max
        f_out  = f[f_mask]
        S_out  = Sxx_db[f_mask, :]

        fs = max(1, len(f_out) // 100)
        ts = max(1, S_out.shape[1] // 400)
        f_out = f_out[::fs]
        t_out = t[::ts]
        S_out = S_out[::fs, ::ts]

        return jsonify({
            "channel": tr.stats.channel,
            "start"  : str(tr.stats.starttime)[:19],
            "end"    : str(tr.stats.endtime)[:19],
            "freqs"  : f_out.tolist(),
            "times"  : t_out.tolist(),
            "Sxx"    : S_out.tolist(),
            "vmin"   : float(np.percentile(S_out, 5)),
            "vmax"   : float(np.percentile(S_out, 99)),
        })
    except Exception as exc:
        return jsonify({"error": f"Spectrogram error: {exc}"}), 500


# ── System Monitor ────────────────────────────────────────────────────────────

@app.route("/api/sysinfo")
def sysinfo():
    try:
        import psutil
    except ImportError:
        return jsonify({"error": "psutil not installed"}), 500

    # CPU
    cpu_pct  = psutil.cpu_percent(interval=0.3, percpu=True)
    cpu_freq = psutil.cpu_freq()
    cpu_info = {
        "cores"  : len(cpu_pct),
        "per_core": cpu_pct,
        "total"  : round(sum(cpu_pct) / len(cpu_pct), 1),
        "freq_mhz": round(cpu_freq.current) if cpu_freq else 0,
        "freq_max": round(cpu_freq.max)     if cpu_freq else 0,
    }

    # RAM
    vm   = psutil.virtual_memory()
    swap = psutil.swap_memory()
    ram_info = {
        "total_gb" : round(vm.total / 1024 ** 3, 1),
        "used_gb"  : round(vm.used  / 1024 ** 3, 1),
        "avail_gb" : round(vm.available / 1024 ** 3, 1),
        "pct"      : vm.percent,
        "swap_total_gb": round(swap.total / 1024 ** 3, 1),
        "swap_used_gb" : round(swap.used  / 1024 ** 3, 1),
        "swap_pct"     : swap.percent,
    }

    # Disk
    disk_info: list = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disk_info.append({
                "mount"   : part.mountpoint,
                "device"  : part.device,
                "fstype"  : part.fstype,
                "total_gb": round(usage.total / 1024 ** 3, 1),
                "used_gb" : round(usage.used  / 1024 ** 3, 1),
                "free_gb" : round(usage.free  / 1024 ** 3, 1),
                "pct"     : usage.percent,
            })
        except Exception:
            pass

    # GPU via nvidia-smi
    gpu_info: list = []
    try:
        import subprocess
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    gpu_info.append({
                        "name"    : parts[0],
                        "util_pct": int(parts[1]) if parts[1].isdigit() else 0,
                        "mem_used_mb"  : int(parts[2]) if parts[2].isdigit() else 0,
                        "mem_total_mb" : int(parts[3]) if parts[3].isdigit() else 0,
                        "temp_c"  : int(parts[4]) if parts[4].isdigit() else 0,
                    })
    except Exception:
        pass

    # ── Map each process to its SeisWork session (job_id) ────────────────────
    # SeisWork runs each job as a subprocess whose cmdline carries the job_id:
    #   _pick_runner.py     <cfg> <base> <method> <job_id>
    #   _pipeline_runner.py <cfg> <base> <step> <method> <job_id> [input]
    # Child processes (GPU/MP workers) don't carry it, so we walk up to the
    # nearest runner ancestor, then tag the top-process list with the session.
    def _job_meta_from_cmd(cmd):
        # _pick_runner.py     <cfg> <base> <method> <job_id>
        # _pipeline_runner.py <cfg> <base> <method> <flow> <job_id>
        for i, a in enumerate(cmd):
            if a.endswith("_pick_runner.py"):
                return {"kind": "pick", "method": cmd[i + 3] if len(cmd) > i + 3 else "",
                        "job_id": cmd[i + 4] if len(cmd) > i + 4 else None}
            if a.endswith("_pipeline_runner.py"):
                return {"kind": "pipeline", "method": cmd[i + 3] if len(cmd) > i + 3 else "",
                        "job_id": cmd[i + 5] if len(cmd) > i + 5 else None}
        return None

    def _job_id_from_cmd(cmd):
        m = _job_meta_from_cmd(cmd)
        return m["job_id"] if m else None

    def _pick_progress(job_id):
        # Parse last "[B<n>/<tot>]" batch marker + pick count from output.log tail.
        import re
        try:
            logf = PICK_DIR / job_id / "output.log"
            if not logf.exists():
                return None
            with open(logf, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                tail = fh.read().decode("utf-8", "ignore").replace("\r", "\n")
            out: dict = {}
            bm = None
            for bm in re.finditer(r"\[B(\d+)/(\d+)\]", tail):
                pass
            if bm:
                out["batch"], out["batches"] = int(bm.group(1)), int(bm.group(2))
            return out or None
        except Exception:
            return None

    runner_job: dict = {}    # pid -> job_id (direct runners)
    ppid_of: dict = {}       # pid -> ppid
    active_jobs: list = []   # live runner processes (survives server restart / orphaned status)
    _now_ts = datetime.now().timestamp()
    try:
        for p in psutil.process_iter(["pid", "ppid", "cmdline", "create_time"]):
            pid = p.info["pid"]
            ppid_of[pid] = p.info.get("ppid")
            meta = _job_meta_from_cmd(p.info.get("cmdline") or [])
            if meta and meta["job_id"]:
                runner_job[pid] = meta["job_id"]
                el_s = int(_now_ts - (p.info.get("create_time") or _now_ts))
                job = {"pid": pid, "job_id": meta["job_id"], "kind": meta["kind"],
                       "method": meta["method"], "elapsed_s": el_s,
                       "elapsed": f"{el_s // 3600}h {(el_s % 3600) // 60:02d}m"}
                if meta["kind"] == "pick":
                    prog = _pick_progress(meta["job_id"])
                    if prog:
                        job.update(prog)
                active_jobs.append(job)
    except Exception:
        pass
    active_jobs.sort(key=lambda j: j["elapsed_s"], reverse=True)

    def _resolve_session(pid, depth=0):
        if pid in runner_job:
            return runner_job[pid]
        if depth > 25:
            return None
        pp = ppid_of.get(pid)
        if not pp or pp == pid:
            return None
        return _resolve_session(pp, depth + 1)

    # phasenet_native workers run inside Docker, so their parent is
    # containerd-shim, not the runner, and _resolve_session (parent-walk)
    # can't reach the session. Fall back to the job dir embedded in the
    # predict.py cmdline (--data_list / --result_dir = .../picking/<job_id>/_pn_native/...).
    import re as _re
    def _session_from_cmd(cmd):
        for a in (cmd or []):
            m = _re.search(r"/picking/([^/]+)/_pn_native/", a)
            if m:
                return m.group(1)
        return None

    # Top processes (by CPU usage)
    procs: list = []
    try:
        for p in sorted(
            psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "cmdline"]),
            key=lambda x: x.info.get("cpu_percent") or 0, reverse=True,
        )[:15]:
            pid = p.info["pid"]
            session = _resolve_session(pid) or _session_from_cmd(p.info.get("cmdline"))
            procs.append({
                "pid"     : pid,
                "name"    : (p.info["name"] or "")[:32],
                "cpu_pct" : round(p.info.get("cpu_percent") or 0, 1),
                "mem_pct" : round(p.info.get("memory_percent") or 0, 1),
                "session" : session or "",
            })
    except Exception:
        pass

    # Uptime
    boot = psutil.boot_time()
    uptime_s = int(datetime.now().timestamp() - boot)
    uptime_h, uptime_m = divmod(uptime_s // 60, 60)

    return jsonify({
        "cpu"    : cpu_info,
        "ram"    : ram_info,
        "disk"   : disk_info,
        "gpu"    : gpu_info,
        "procs"  : procs,
        "active_jobs": active_jobs,
        "uptime" : f"{uptime_h}h {uptime_m}m",
        "ts"     : datetime.now().strftime("%H:%M:%S"),
    })


# ── Picking Jobs ───────────────────────────────────────────────────────────────

PICK_DIR = TMP_DIR / "picking"
PICK_DIR.mkdir(exist_ok=True)

_pick_jobs: dict[str, dict]  = {}
_pick_procs: dict[str, object] = {}

# Runner is a static file seiswork/web/_pick_runner.py, not a template string.
# This avoids the "server not restarted, old template stays in memory" problem.
_RUNNER_PATH = Path(__file__).resolve().parent / "_pick_runner.py"


def _ensure_runner() -> Path:
    if not _RUNNER_PATH.exists():
        raise FileNotFoundError(f"Runner not found: {_RUNNER_PATH}")
    return _RUNNER_PATH


def _write_stations_file(meta: dict, job_dir: Path) -> str:
    """Write stations from meta config as pipe-delimited file into job_dir.
    Returns absolute path to the written file, or empty string if no stations."""
    stations = meta.get("stations", [])
    if not stations:
        return ""
    job_dir.mkdir(parents=True, exist_ok=True)
    sta_path = job_dir / "stations.txt"
    with open(sta_path, "w") as fh:
        for s in stations:
            net  = str(s.get("network",  "")).strip()
            sta  = str(s.get("station",  "")).strip()
            lat  = s.get("lat",  0.0)
            lon  = s.get("lon",  0.0)
            elev = s.get("elev", 0.0)
            fh.write(f"{net}|{sta}|{lat}|{lon}|{elev}\n")
    return str(sta_path)


def _build_pick_cfg(meta: dict, method: str, params: dict, job_id: str = "") -> dict:
    """Convert GUI config dict + form params into SeisWork config.yaml structure."""
    region   = meta.get("region", {})
    waveform = meta.get("waveform", {})

    # Resolve waveform path: SDS path or FDSN output path
    wv_path     = (waveform.get("path") or "").strip()
    wv_fdsn_out = (waveform.get("fdsn", {}).get("output_path") or "").strip()
    wv_dir      = wv_path or wv_fdsn_out or "work/waveforms"
    path_type   = waveform.get("path_type", "sds")

    # data_source from form; if not set, fall back to path_type
    gui_datasrc = params.get("data_source", params.get("data_src", ""))
    if not gui_datasrc:
        gui_datasrc = "sds" if path_type == "sds" else "file"

    # Station file: written from meta["stations"] to job_dir so no external file needed
    job_dir    = PICK_DIR / job_id if job_id else None
    sta_file   = _write_stations_file(meta, job_dir) if job_dir else ""
    # Fallback to config/stations.txt if meta has no stations
    if not sta_file:
        sta_file = str(BASE_DIR / "config" / "stations.txt")

    cfg: dict = {
        "region": {
            "name"      : meta.get("name", ""),
            "lat_min"   : float(region.get("lat_min", -10.0)),
            "lat_max"   : float(region.get("lat_max",  10.0)),
            "lon_min"   : float(region.get("lon_min",  90.0)),
            "lon_max"   : float(region.get("lon_max", 141.0)),
            "depth_max" : float(region.get("depth_max", 60.0)),
            "starttime" : (region.get("starttime") or "")[:19].replace("T", " "),
            "endtime"   : (region.get("endtime")   or "")[:19].replace("T", " "),
        },
        # data section, used by all pickers for wave_dir
        "data": {
            "waveform_dir" : wv_dir,
            "sds_format"   : (path_type == "sds"),
            "station_file" : sta_file,
            "inventory"    : str(BASE_DIR / "config" / "inventory.xml"),
            # picks_dir: job-specific so sessions do not overwrite each other
            "picks_dir"    : str(PICK_DIR / job_id) if job_id else "",
            # channels: empty = auto-detect from SDS (EH?, HH?, BH?, etc.)
            # set by user only to override, e.g. "EH?" for Raspberry Shake
            "channels"     : (params.get("channels") or "").strip(),
        },
        "fdsn": {
            "client"  : waveform.get("fdsn", {}).get("client", "LOCAL"),
            "user"    : "",
            "password": "",
        },
        "pick": {},
    }

    if method == "phasenet":
        # GUI model dropdown: "original","stead","diting","instance";
        # maps to a pretrained key in seisbench
        pretrained_map = {
            "original" : "original",
            "stead"    : "stead",
            "diting"   : "diting",
            "instance" : "instance",
        }
        gui_model  = params.get("model", "stead")
        pretrained = pretrained_map.get(gui_model, gui_model)

        cfg["pick"]["phasenet"] = {
            "model"              : "PhaseNet",    # seisbench model class
            "pretrained"         : pretrained,    # pretrained weights key
            "p_threshold"        : float(params.get("p_thr",    0.3)),
            "s_threshold"        : float(params.get("s_thr",    0.3)),
            "batch_size"         : 64,
            "highpass_hz"        : 1.0,
            "workers"            : int(params.get("workers",    8)),
            "io_processes"       : int(params.get("io_proc",    0)),
            "gpu_workers"        : int(params.get("gpu_workers", 1)),
            "data_source"        : gui_datasrc,
            "sds_path"           : wv_dir if path_type == "sds" else "",
            "sds_chunk_days"     : 1,
            "annotate_chunk_hours": 1,
            "denoise"            : bool(params.get("denoise", False)),
            "denoise_pretrained" : str(params.get("denoise_pretrained", "original")),
        }

    elif method == "phasenet_native":
        # Native AI4EPS PhaseNet (TensorFlow predict.py), Docker only (TF-GPU
        # image built by install.sh). See seiswork/modules/picker/phasenet_native.py.
        # Reads SDS directly via per-station-day globs; balances shards by data
        # volume across N workers. No conda env involved.
        cfg["pick"]["phasenet_native"] = {
            "docker_image" : params.get("docker_image", "seiswork/phasenet:tf2.12"),
            "workers"      : int(params.get("workers", 4)),
            "p_threshold"  : float(params.get("p_thr", 0.3)),
            "s_threshold"  : float(params.get("s_thr", 0.3)),
            "highpass_hz"  : 1.0,
            "sds_path"     : wv_dir if path_type == "sds" else "",
            "model_dir"    : params.get("model_dir") or os.environ.get("PHASENET_MODEL")
                             or str(Path.home() / "apps" / "PhaseNet" / "model" / "190703-214543"),
            "phasenet_dir" : params.get("phasenet_dir") or os.environ.get("PHASENET_DIR")
                             or str(Path.home() / "apps" / "PhaseNet"),
        }

    elif method == "eqt":
        pretrained_map = {
            "original" : "original",
            "stead"    : "stead",
            "instance" : "instance",
            "ethz"     : "ethz",
            "geofon"   : "geofon",
        }
        gui_model  = params.get("model", "original")
        pretrained = pretrained_map.get(gui_model, gui_model)

        cfg["pick"]["eqt"] = {
            "model"               : "EQTransformer",
            "pretrained"          : pretrained,
            "detection_threshold" : float(params.get("det_thr", 0.3)),
            "p_threshold"         : float(params.get("p_thr",   0.1)),
            "s_threshold"         : float(params.get("s_thr",   0.1)),
            "workers"             : int(params.get("workers",    8)),
            "data_source"         : gui_datasrc,
            "sds_path"            : wv_dir if path_type == "sds" else "",
        }

    elif method == "stalta":
        cfg["pick"]["stalta"] = {
            "sta_sec"      : float(params.get("sta",       0.5)),
            "lta_sec"      : float(params.get("lta",      10.0)),
            "thresh_on"    : float(params.get("thr_on",    3.0)),
            "thresh_off"   : float(params.get("thr_off",   1.0)),
            "freqmin"      : float(params.get("f_lo",      1.0)),
            "freqmax"      : float(params.get("f_hi",     40.0)),
            "min_duration" : float(params.get("min_len",   1.0)),
            "max_duration" : float(params.get("max_len",  60.0)),
            "n_workers"    : int(params.get("n_workers",   4)),
        }

    return cfg


def _pick_worker(job_id: str, cfg: dict, method: str, cfg_id: str = "") -> None:
    """Background thread: write temp YAML, launch subprocess, stream log."""
    import subprocess, sys as _sys

    job_dir = PICK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    log_path    = job_dir / "output.log"
    status_path = job_dir / "status.json"
    cfg_path    = job_dir / "run_config.yaml"

    print(f"[pick:{job_id}] picks_dir={job_dir}", flush=True)

    def _ws(s: dict) -> None:
        status_path.write_text(json.dumps(s, indent=2))
        _pick_jobs[job_id] = dict(s)

    status: dict = {
        "id"      : job_id,
        "method"  : method,
        "cfg_id"  : cfg_id,
        "state"   : "running",
        "started" : datetime.now().isoformat(timespec="seconds"),
        "picks_dir": str(job_dir),
    }
    _ws(status)

    try:
        cfg_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
        runner = _ensure_runner()

        base_cmd = [_sys.executable, str(runner),
                    str(cfg_path), str(BASE_DIR), method, job_id]
        # phasenet_native is Docker-only: the runner spawns `docker run`, which
        # needs the `docker` group. The Flask server may not have it (group is
        # only granted to new login sessions), so re-exec the runner under
        # `sg docker` to acquire the group for it and its container children.
        if method == "phasenet_native":
            import shlex
            cmd = ["sg", "docker", "-c", shlex.join(base_cmd)]
        else:
            cmd = base_cmd
        with open(log_path, "w") as log_fh:
            proc = subprocess.Popen(
                cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            _pick_procs[job_id] = proc
            proc.wait()
            rc = proc.returncode

        if _pick_jobs.get(job_id, {}).get("state") == "stopped":
            status["state"] = "stopped"
        elif rc == 0:
            status["state"] = "done"
            # picks.csv is written directly to job_dir by the picker (via data.picks_dir config)
            picks_path = job_dir / "picks.csv"
            if picks_path.exists():
                try:
                    import pandas as _pd
                    df = _pd.read_csv(picks_path)
                    ph_col = "phase_hint" if "phase_hint" in df.columns else "phase_type"
                    status["picks"] = {
                        "total": len(df),
                        "P"    : int((df[ph_col] == "P").sum()),
                        "S"    : int((df[ph_col] == "S").sum()),
                    }
                except Exception:
                    pass
        else:
            status["state"] = "error"
            status["returncode"] = rc
    except Exception as exc:
        status["state"] = "error"
        status["error"] = str(exc)
        with open(log_path, "a") as lf:
            lf.write(f"\n[flask] Exception: {exc}\n")

    status["finished"] = datetime.now().isoformat(timespec="seconds")
    _ws(status)
    _pick_procs.pop(job_id, None)


@app.route("/api/picking/run", methods=["POST"])
def picking_run():
    data      = request.get_json(force=True)
    cfg_id    = data.get("cfg_id", "").strip()
    method    = data.get("method", "").strip().lower()
    params    = data.get("params", {})

    if method not in ("phasenet", "phasenet_native", "eqt", "stalta"):
        return jsonify({"error": f"Unknown method: {method}"}), 400

    if not cfg_id:
        return jsonify({"error": "cfg_id required"}), 400

    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": f"Config {cfg_id} not found"}), 404

    meta    = json.loads(meta_file.read_text())
    job_id  = str(uuid.uuid4())[:8]
    cfg     = _build_pick_cfg(meta, method, params, job_id)

    t = threading.Thread(target=_pick_worker, args=(job_id, cfg, method, cfg_id), daemon=True)
    _job_threads[job_id] = t
    t.start()

    return jsonify({"id": job_id, "state": "running", "method": method})


def _live_runner_job_ids() -> set:
    """Job IDs whose _pick_runner.py / _pipeline_runner.py process is still alive.
    Survives server restart: the in-memory _job_threads dict is lost on restart, but
    already-detached subprocesses keep running, so don't incorrectly mark them 'stopped'."""
    live: set = set()
    try:
        import psutil
    except Exception:
        return live
    import re as _re
    try:
        for p in psutil.process_iter(["cmdline"]):
            cmd = p.info.get("cmdline") or []
            for i, a in enumerate(cmd):
                if a.endswith("_pick_runner.py") and len(cmd) > i + 4:
                    live.add(cmd[i + 4]); break
                if a.endswith("_pipeline_runner.py") and len(cmd) > i + 5:
                    live.add(cmd[i + 5]); break
            # phasenet_native runs picking inside `docker run ... predict.py`
            # workers (no _pick_runner parent once detached). Detect them as live
            # by the job dir embedded in the predict.py --data_list path.
            for a in cmd:
                m = _re.search(r"/picking/([^/]+)/_pn_native/", a)
                if m:
                    live.add(m.group(1)); break
    except Exception:
        pass
    return live


_PIPE_CATALOG_NAMES = (
    "catalog_associated.csv", "catalog_gamma.csv", "catalog_real.csv",
    "catalog_nlloc.csv", "catalog_locsat.csv", "catalog_ml.csv",
    "velest_vel.csv", "hypodd_reloc.csv", "catalog_relocated.csv",
    "catalog_matchlocate.csv", "catalog.csv", "events.csv",
)


def _reconcile_state(j: dict, sf, live: set, job_dir=None) -> dict:
    """Reconcile job state against actual OS processes. A live process forces
    'running' (even when _job_threads is empty after a server restart);
    'running' with no thread and no live process inspects output.log first
    (job may have finished during the restart) for 'done'/'error', then falls
    back to 'stopped'."""
    jid = j.get("id")
    if jid in live:
        if j.get("state") != "running":
            j["state"] = "running"
            j["note"]  = "recovered — process running (orphaned from server)"
            try:
                sf.write_text(json.dumps(j, indent=2))
            except Exception:
                pass
    elif (j.get("state") == "running" and jid not in _job_threads) or (
          j.get("state") == "stopped" and
          "orphaned" in (j.get("note") or "") and
          job_dir is not None and not j.get("events")):
        # No process, no thread: check log before marking 'stopped'.
        # Also re-check 'stopped' jobs from orphans that may actually be done
        # (log already has '[runner] DONE:' but status was not yet updated).
        recovered = False
        if job_dir is not None:
            log_path = Path(job_dir) / "output.log"
            try:
                text = log_path.read_text(errors="replace") if log_path.exists() else ""
                if "[runner] DONE:" in text:
                    j["state"] = "done"
                    j["note"]  = "recovered — finished during server restart"
                    # count events from catalog file if present
                    for fname in _PIPE_CATALOG_NAMES:
                        fpath = Path(job_dir) / fname
                        if fpath.exists():
                            try:
                                import pandas as _pd
                                df = _pd.read_csv(fpath)
                                j["events"]      = len(df)
                                j["result_file"] = str(fpath)
                            except Exception:
                                pass
                            break
                    recovered = True
                elif "[runner] ERROR:" in text:
                    j["state"] = "error"
                    j["note"]  = "recovered — error during server restart"
                    recovered = True
            except Exception:
                pass
        if not recovered:
            j["state"] = "stopped"
            j["note"]  = "orphaned — server restarted while job was running"
        try:
            sf.write_text(json.dumps(j, indent=2))
        except Exception:
            pass
    return j


@app.route("/api/picking/jobs", methods=["GET"])
def list_pick_jobs():
    filter_cfg = request.args.get("cfg_id", "").strip()
    if not filter_cfg:
        return jsonify({"error": "cfg_id required"}), 400
    live = _live_runner_job_ids()
    result: list[dict] = []
    try:
        dirs = list(PICK_DIR.iterdir()) if PICK_DIR.exists() else []
    except PermissionError as e:
        return jsonify({"error": f"Akses ditolak ke folder picking: {e}"}), 403
    try:
        dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    except Exception:
        pass
    for d in dirs:
        sf = d / "status.json"
        if sf.is_file():
            try:
                j = json.loads(sf.read_text())
                if not j.get("id"):
                    j["id"] = d.name
                j = _reconcile_state(j, sf, live)
                if not filter_cfg or j.get("cfg_id") == filter_cfg:
                    result.append(j)
            except Exception:
                pass
    return jsonify(result)


@app.route("/api/picking/jobs/<job_id>/log", methods=["GET"])
def picking_log(job_id):
    log_path = PICK_DIR / job_id / "output.log"
    if not log_path.exists():
        return jsonify({"lines": [], "offset": 0, "state": "unknown"})

    offset = int(request.args.get("offset", 0))
    tail   = int(request.args.get("tail",   0))

    state = "running"
    sf    = PICK_DIR / job_id / "status.json"
    if sf.exists():
        try:
            state = json.loads(sf.read_text()).get("state", "running")
        except Exception:
            pass
    if state != "running" and job_id in _live_runner_job_ids():
        state = "running"

    if tail > 0:
        fsize = log_path.stat().st_size
        chunk = min(fsize, tail * 200)
        with open(log_path, "rb") as fh:
            fh.seek(max(0, fsize - chunk))
            raw = fh.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()[-tail:]
        return jsonify({"lines": lines, "offset": fsize, "state": state,
                        "tail": True, "total_size": fsize})

    with open(log_path, "rb") as fh:
        fh.seek(offset)
        raw  = fh.read()
        next_offset = fh.tell()

    lines = raw.decode("utf-8", errors="replace").splitlines()
    return jsonify({"lines": lines, "offset": next_offset, "state": state})


def _kill_pick_processes(job_id: str) -> bool:
    """Kill EVERY live process belonging to picking job_id + its Docker workers.
    Works even after a server restart (when the in-memory _pick_procs handle is
    gone) by matching the job_id in process cmdlines, fixing the 'stopped but
    still running' orphan. Returns True if anything live was found/killed."""
    found = False
    try:
        import psutil, signal
        for p in psutil.process_iter(["pid", "cmdline"]):
            cmd = p.info.get("cmdline") or []
            joined = " ".join(cmd)
            is_runner = (cmd and any(a.endswith("_pick_runner.py") for a in cmd)
                         and job_id in cmd)
            is_worker = f"/picking/{job_id}/_pn_native/" in joined   # native predict.py
            if is_runner or is_worker:
                try:
                    p.send_signal(signal.SIGKILL); found = True
                except Exception:
                    pass
    except Exception:
        pass
    # phasenet_native Docker containers: sw-pick-phasenet_native-<job_id>-w*
    try:
        import subprocess
        subprocess.run(
            ["sg", "docker", "-c",
             f"docker ps -q --filter name=phasenet_native-{job_id}- "
             f"| xargs -r docker kill"],
            timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return found


@app.route("/api/picking/jobs/<job_id>", methods=["DELETE"])
def stop_pick_job(job_id):
    # Kill any live process/containers for this job (handles orphans after a
    # server restart, where _pick_procs is empty). Also terminate the tracked
    # handle if we still have it (same-process launch).
    was_live = _kill_pick_processes(job_id)
    proc = _pick_procs.get(job_id)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass

    if was_live or proc is not None:
        # was running: mark stopped, keep the job dir (logs/partial output)
        _pick_jobs[job_id] = {**_pick_jobs.get(job_id, {}), "state": "stopped"}
        sf = PICK_DIR / job_id / "status.json"
        if sf.exists():
            try:
                d = json.loads(sf.read_text()); d["state"] = "stopped"
                sf.write_text(json.dumps(d, indent=2))
            except Exception:
                pass
    else:
        # not running: user is removing the job, delete its dir
        job_dir = PICK_DIR / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)
        _pick_jobs.pop(job_id, None)
        _job_threads.pop(job_id, None)

    return jsonify({"message": "ok"})


@app.route("/api/picking/jobs/<job_id>/files", methods=["GET"])
def list_job_files(job_id):
    """List all files in the job directory with size and modification time."""
    job_dir = PICK_DIR / job_id
    try:
        if not job_dir.exists():
            return jsonify([])
    except PermissionError as e:
        return jsonify({"error": f"Access denied: {e}"}), 403
    files = []
    try:
        entries = sorted(job_dir.iterdir())
    except PermissionError as e:
        return jsonify({"error": f"Akses ditolak ke folder job: {e}"}), 403
    for f in entries:
        if f.is_file():
            try:
                st = f.stat()
                files.append({
                    "name"    : f.name,
                    "path"    : str(f),
                    "size"    : st.st_size,
                    "mtime"   : datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "download": f.suffix in (".csv", ".yaml", ".log", ".json"),
                })
            except Exception:
                pass
    return jsonify(files)


@app.route("/api/results", methods=["GET"])
def list_results():
    """Overview of every pipeline output folder under work/."""
    steps = [
        ("picks",      "Phase Picking",   WORK_DIR / "picks"),
        ("catalog",    "Association",     WORK_DIR / "catalog"),
        ("location",   "Location",        WORK_DIR / "location"),
        ("magnitude",  "Magnitude",       WORK_DIR / "magnitude"),
        ("velocity",   "Velocity",        WORK_DIR / "velocity"),
        ("relocation", "Relocation",      WORK_DIR / "relocation"),
        ("plots",      "Plots",           WORK_DIR / "plots"),
    ]
    result = []
    for key, label, p in steps:
        entry: dict = {
            "key"        : key,
            "label"      : label,
            "path"       : str(p),
            "exists"     : p.exists(),
            "files"      : [],
            "size_total" : 0,
            "file_count" : 0,
        }
        if p.exists():
            for f in sorted(p.rglob("*")):
                if f.is_file():
                    sz = f.stat().st_size
                    entry["files"].append({
                        "name"  : f.name,
                        "path"  : str(f),
                        "rel"   : str(f.relative_to(p)),
                        "size"  : sz,
                        "mtime" : datetime.fromtimestamp(f.stat().st_mtime).isoformat(
                                      timespec="seconds"),
                    })
                    entry["size_total"] += sz
                    entry["file_count"] += 1
        result.append(entry)
    return jsonify(result)


# ── Benchmark: compare SeisWork results vs reference catalogs ─────────────────
# Reference paths are set via env vars (SEISWORK_BENCH_DIR / SEISWORK_BENCH_*)
# or the ?velest=...&hypodd=... query params; no default, so a fresh install
# with none of these set just shows an empty benchmark (no error).
# SeisWork's own relocation outputs are read from work/benchmark (preferred)
# or work/relocation.
_BENCH_BASE = os.environ.get("SEISWORK_BENCH_DIR", "")
BENCH_DEFAULTS = {
    "real_ref"   : os.environ.get("SEISWORK_BENCH_REAL",   f"{_BENCH_BASE}/REAL"),
    "mag_ref"    : os.environ.get("SEISWORK_BENCH_MAG",    f"{_BENCH_BASE}/cal_mags/Newcatalog_magPhases.txt"),
    "velest_ref" : os.environ.get("SEISWORK_BENCH_VELEST", f"{_BENCH_BASE}/Newvelest_935_1149_02Jun/final.CNV"),
    "hypodd_ref" : os.environ.get("SEISWORK_BENCH_HYPODD", f"{_BENCH_BASE}/hypodd_filter903/hypoDD.reloc"),
}


def _first_existing(*paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return ""


@app.route("/api/benchmark", methods=["GET"])
def benchmark():
    """Assemble a per-method comparison (reference vs SeisWork) of event count,
    RMS, depth, magnitude and azimuthal gap for the View Results Benchmark panel."""
    from seiswork.web import _benchmark as bm

    bdir = str(WORK_DIR / "benchmark")
    rdir = str(WORK_DIR / "relocation")

    def arg(k):
        return request.args.get(k, BENCH_DEFAULTS.get(k, ""))

    # (key, label, group, path, parser)
    plan = [
        ("real_ref",   "REAL (referensi)",        "reference", arg("real_ref"),   bm.parse_real_catalog),
        ("mag_ref",    "Magnitude (referensi)",   "reference", arg("mag_ref"),    bm.parse_mag_phase),
        ("velest_ref", "VELEST (referensi)",      "reference", arg("velest_ref"), bm.parse_velest_cnv),
        ("hypodd_ref", "hypoDD (referensi)",      "reference", arg("hypodd_ref"), bm.parse_hypodd_reloc),
        ("velest_sw",  "VELEST (SeisWork)",       "seiswork",
            _first_existing(arg("velest_sw"),
                            str(WORK_DIR / "velocity" / "final.CNV"),
                            str(WORK_DIR / "velocity" / "velest" / "final.CNV")),
            bm.parse_velest_cnv),
        ("hypodd_sw",  "hypoDD catalog (SeisWork)", "seiswork",
            _first_existing(arg("hypodd_sw"),
                            os.path.join(bdir, "hypodd", "hypoDD.reloc"),
                            os.path.join(rdir, "catalog", "hypoDD.reloc")),
            bm.parse_hypodd_reloc),
        ("hypoddcc_sw","hypoDD cross-corr (SeisWork)", "seiswork",
            _first_existing(arg("hypoddcc_sw"),
                            os.path.join(bdir, "crosscorr", "hypoDD.reloc"),
                            os.path.join(rdir, "crosscorr", "hypoDD.reloc")),
            bm.parse_hypodd_reloc),
        ("growclust_sw","GrowClust (SeisWork)",   "seiswork",
            _first_existing(arg("growclust_sw"),
                            os.path.join(bdir, "growclust", "OUT", "out.growclust_cat"),
                            os.path.join(rdir, "growclust", "OUT", "out.growclust_cat")),
            bm.parse_growclust_cat),
    ]

    items = []
    for key, label, group, path, parser in plan:
        entry = {"key": key, "label": label, "group": group,
                 "path": path, "exists": bool(path and os.path.exists(path))}
        if entry["exists"]:
            try:
                entry.update(bm.summarize(parser(path)))
            except Exception as e:
                entry["error"] = str(e)
        else:
            entry["n"] = 0
        items.append(entry)
    return jsonify({"items": items})


def _picks_path_or_error(job_id: str):
    """Return (picks_path, None) or (None, (json_response, status_code)).
    Checks: job status, then PermissionError, then file existence."""
    job_dir = PICK_DIR / job_id
    picks_path = job_dir / "picks.csv"

    # 1. Check job status first: if "error", return the error message from the runner
    sf = job_dir / "status.json"
    if sf.is_file():
        try:
            st = json.loads(sf.read_text())
            if st.get("state") == "error":
                err_msg = st.get("error") or "Picking failed — check log for details"
                return None, (jsonify({"error": f"Job failed: {err_msg}",
                                       "state": "error",
                                       "log_hint": f"Check /api/picking/jobs/{job_id}/log-full"}), 500)
        except Exception:
            pass

    # 2. Explicitly check for PermissionError (Path.exists() silently returns False)
    try:
        exists = picks_path.exists()
    except PermissionError as e:
        return None, (jsonify({"error": f"Access denied: {e} — ensure user has read permission for {picks_path}"}), 403)

    # 3. File does not exist
    if not exists:
        state = "unknown"
        try:
            if sf.is_file():
                state = json.loads(sf.read_text()).get("state", "unknown")
        except Exception:
            pass
        hint = ("Picking may not have finished yet" if state == "running"
                else f"picks.csv not found at {picks_path}")
        return None, (jsonify({"error": hint, "state": state,
                               "picks_dir": str(job_dir)}), 404)

    return picks_path, None


@app.route("/api/picking/jobs/<job_id>/picks.csv", methods=["GET"])
def get_picks_csv(job_id):
    picks_path, err = _picks_path_or_error(job_id)
    if err:
        return err
    return send_file(
        str(picks_path),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"picks_{job_id}.csv",
    )


@app.route("/api/picking/jobs/<job_id>/preview", methods=["GET"])
def preview_picks(job_id):
    """Return paginated rows of picks.csv as JSON for inline table display."""
    picks_path, err = _picks_path_or_error(job_id)
    if err:
        return err
    row_offset = int(request.args.get("offset", 0))
    row_limit  = min(int(request.args.get("limit", int(request.args.get("rows", 200)))), 2000)
    try:
        import csv as _csv
        columns: list = []
        rows: list    = []
        total = 0
        with open(picks_path, newline="", encoding="utf-8") as fh:
            for i, row in enumerate(_csv.reader(fh)):
                if i == 0:
                    columns = row
                    continue
                total += 1
                data_i = total - 1
                if data_i >= row_offset and len(rows) < row_limit:
                    rows.append(row)
        return jsonify({
            "columns" : columns,
            "rows"    : rows,
            "total"   : total,
            "offset"  : row_offset,
            "showing" : len(rows),
            "has_more": row_offset + row_limit < total,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/picking/jobs/<job_id>/log-full", methods=["GET"])
def get_log_full(job_id):
    """Return paginated log lines. Default: last 500 lines (tail mode).
    Use ?start=N&limit=M to load a specific window (for scroll-up loading)."""
    log_path = PICK_DIR / job_id / "output.log"
    if not log_path.exists():
        return jsonify({"lines": [], "total": 0, "start": 0, "showing": 0, "has_prev": False})
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        all_lines = fh.read().splitlines()
    total = len(all_lines)
    if "start" in request.args:
        line_start = max(0, min(int(request.args["start"]), total))
        line_limit = min(int(request.args.get("limit", 500)), 2000)
        lines = all_lines[line_start:line_start + line_limit]
        return jsonify({"lines": lines, "total": total, "start": line_start,
                        "showing": len(lines), "has_prev": line_start > 0})
    # tail mode
    line_limit = min(int(request.args.get("limit", int(request.args.get("tail", 500)))), 2000)
    line_start = max(0, total - line_limit)
    lines = all_lines[line_start:]
    return jsonify({"lines": lines, "total": total, "start": line_start,
                    "showing": len(lines), "has_prev": line_start > 0})


@app.route("/api/picking/jobs/<job_id>/picks-for-viewer", methods=["GET"])
def picks_for_viewer(job_id):
    """Return P/S picks for a station+date as day-fraction values, for canvas overlay."""
    station  = request.args.get("station", "")   # NET.STA
    date_str = request.args.get("date", "")

    picks_path = PICK_DIR / job_id / "picks.csv"
    if not picks_path.exists():
        return jsonify([])

    if not station or not date_str:
        return jsonify({"error": "station and date required"}), 400

    parts = station.split(".")
    net, sta = parts[0], parts[1] if len(parts) > 1 else ""

    import csv as _csv
    from datetime import datetime as _dt, timedelta as _td

    try:
        day_start = _dt.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400

    day_start_ts = day_start.timestamp()
    day_secs     = 86400.0

    picks = []
    with open(picks_path, newline="", encoding="utf-8") as fh:
        reader = _csv.DictReader(fh)
        for row in reader:
            if row.get("network", "") != net or row.get("station", "") != sta:
                continue
            phase = row.get("phase_hint", "") or row.get("phase_type", "")
            pt    = row.get("phase_time", "")
            if not pt:
                continue
            try:
                pt_clean = pt.replace("Z", "").replace("T", " ")[:19]
                t = _dt.strptime(pt_clean, "%Y-%m-%d %H:%M:%S")
                frac = (t.timestamp() - day_start_ts) / day_secs
                if 0.0 <= frac <= 1.0:
                    picks.append({"phase": phase, "frac": round(frac, 8),
                                  "time": pt[:19], "score": row.get("phase_score", "")})
            except Exception:
                pass

    return jsonify(picks)


# ── DeepDenoiser standalone jobs ─────────────────────────────────────────────

DENOISE_DIR = TMP_DIR / "denoising"
DENOISE_DIR.mkdir(exist_ok=True)

_denoise_jobs:  dict[str, dict]   = {}
_denoise_procs: dict[str, object] = {}

_DENOISE_RUNNER_PATH = Path(__file__).resolve().parent / "_denoise_runner.py"


def _denoise_worker(job_id: str, cfg: dict, cfg_id: str = "") -> None:
    """Background thread: write temp YAML, launch subprocess, stream log."""
    import subprocess, sys as _sys

    job_dir     = DENOISE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path    = job_dir / "output.log"
    status_path = job_dir / "status.json"
    cfg_path    = job_dir / "run_config.yaml"

    def _ws(s: dict) -> None:
        status_path.write_text(json.dumps(s, indent=2))
        _denoise_jobs[job_id] = dict(s)

    status: dict = {
        "id"      : job_id,
        "cfg_id"  : cfg_id,
        "state"   : "running",
        "started" : datetime.now().isoformat(timespec="seconds"),
        "job_dir" : str(job_dir),
    }
    _ws(status)

    try:
        cfg_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
        if not _DENOISE_RUNNER_PATH.exists():
            raise FileNotFoundError(f"Denoise runner not found: {_DENOISE_RUNNER_PATH}")

        cmd = [_sys.executable, str(_DENOISE_RUNNER_PATH),
               str(cfg_path), str(BASE_DIR), job_id]
        with open(log_path, "w") as log_fh:
            proc = subprocess.Popen(
                cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            _denoise_procs[job_id] = proc
            proc.wait()
            rc = proc.returncode

        if _denoise_jobs.get(job_id, {}).get("state") == "stopped":
            status["state"] = "stopped"
        elif rc == 0:
            status["state"] = "done"
            out_dir = cfg.get("data", {}).get("denoised_dir", "")
            if not out_dir:
                out_dir = str(BASE_DIR / "work" / "denoised_sds")
            status["out_dir"] = out_dir
        else:
            status["state"] = "error"
            status["rc"]    = rc
        _ws(status)
    except Exception as exc:
        status["state"] = "error"
        status["error"] = str(exc)
        _ws(status)
        raise


@app.route("/api/denoise/run", methods=["POST"])
def api_denoise_run():
    """Start a standalone DeepDenoiser job."""
    body   = request.get_json(force=True) or {}
    cfg_id = body.get("cfg_id", "")
    params = body.get("params", {})

    cfg = _load_cfg(cfg_id)
    if cfg is None:
        return jsonify({"error": "cfg_id not found"}), 404

    pretrained  = params.get("pretrained",  "original")
    data_source = params.get("data_source", "sds")
    highpass    = float(params.get("highpass_hz", 0.0))

    path_type = cfg["data"].get("path_type", "sds")
    wv_dir    = str(BASE_DIR / cfg["data"]["waveform_dir"])

    cfg["denoise"] = {
        "deepdenoiser": {
            "pretrained"  : pretrained,
            "data_source" : data_source,
            "sds_path"    : wv_dir if path_type == "sds" else "",
            "highpass_hz" : highpass,
            "batch_size"  : 1,
            "workers"     : int(params.get("workers", 4)),
        }
    }

    job_id = uuid.uuid4().hex[:8]
    t = threading.Thread(target=_denoise_worker, args=(job_id, cfg, cfg_id), daemon=True)
    t.start()
    return jsonify({"id": job_id, "state": "running"})


@app.route("/api/denoise/jobs/<job_id>/log", methods=["GET"])
def api_denoise_log(job_id):
    """Stream log denoise job (same pattern as picking log)."""
    offset   = int(request.args.get("offset", 0))
    log_path = DENOISE_DIR / job_id / "output.log"
    status   = _denoise_jobs.get(job_id, {})
    if not status:
        sf = DENOISE_DIR / job_id / "status.json"
        if sf.exists():
            status = json.loads(sf.read_text())

    lines = []
    if log_path.exists():
        with open(log_path, "r", errors="replace") as fh:
            all_lines = fh.readlines()
        lines = [l.rstrip("\n") for l in all_lines[offset:]]
        new_offset = offset + len(lines)
    else:
        new_offset = offset

    return jsonify({
        "lines" : lines,
        "offset": new_offset,
        "state" : status.get("state", "unknown"),
        "out_dir": status.get("out_dir", ""),
    })


@app.route("/api/denoise/jobs/<job_id>", methods=["DELETE"])
def api_denoise_stop(job_id):
    """Stop a denoise job."""
    proc = _denoise_procs.get(job_id)
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    _denoise_jobs[job_id] = {**_denoise_jobs.get(job_id, {}), "state": "stopped"}
    sf = DENOISE_DIR / job_id / "status.json"
    if sf.exists():
        try:
            s = json.loads(sf.read_text())
            s["state"] = "stopped"
            sf.write_text(json.dumps(s, indent=2))
        except Exception:
            pass
    return jsonify({"ok": True})


# ── Pipeline Jobs (Association / Location / Magnitude / Velocity / Relocation) ──

PIPE_DIR = TMP_DIR / "pipeline"
PIPE_DIR.mkdir(exist_ok=True)

_pipe_jobs: dict[str, dict]   = {}
_pipe_procs: dict[str, object] = {}

_PIPE_RUNNER_PATH = Path(__file__).resolve().parent / "_pipeline_runner.py"


def _ensure_pipe_runner() -> Path:
    if not _PIPE_RUNNER_PATH.exists():
        raise FileNotFoundError(f"Pipeline runner not found: {_PIPE_RUNNER_PATH}")
    return _PIPE_RUNNER_PATH


def _latest_job_dir(cfg_id: str, step: str, require=(), require_any=()) -> str:
    """Return the dir of the most recent completed pipeline job of `step` for
    `cfg_id` (used by the imaging step to consume prior VELEST/HypoDD output).

    require     : every filename here must exist in the job dir.
    require_any : at least one of these filenames must exist (e.g. phase files
                  whose name varies between hypoDD runs).
    """
    best, best_t = "", ""
    for sf in PIPE_DIR.glob("*/status.json"):
        try:
            st = json.loads(sf.read_text())
        except Exception:
            continue
        if not (st.get("cfg_id") == cfg_id and st.get("step") == step
                and st.get("state") == "done"):
            continue
        d = sf.parent
        if any(not (d / f).exists() for f in require):
            continue
        if require_any and not any((d / f).exists() for f in require_any):
            continue
        t = st.get("finished") or st.get("started") or ""
        if t >= best_t:
            best_t, best = t, str(d)
    return best


def _build_pipe_cfg(meta: dict, step: str, method: str, params: dict,
                    job_id: str = "", input_file: str = "") -> dict:
    """Build SeisWork config dict for pipeline steps (assoc, locate, etc.)."""
    region   = meta.get("region", {})
    waveform = meta.get("waveform", {})
    wv_path  = (waveform.get("path") or "").strip()
    wv_fdsn  = (waveform.get("fdsn", {}).get("output_path") or "").strip()
    wv_dir   = wv_path or wv_fdsn or "work/waveforms"
    pt       = waveform.get("path_type", "sds")

    lat_min  = float(region.get("lat_min",  -10.0))
    lat_max  = float(region.get("lat_max",   10.0))
    lon_min  = float(region.get("lon_min",   90.0))
    lon_max  = float(region.get("lon_max",  141.0))
    pjob_dir = PIPE_DIR / job_id if job_id else None
    job_dir  = str(pjob_dir) if pjob_dir else ""

    # Station file: directly from meta["stations"], no external file lookup needed
    sta_file = _write_stations_file(meta, pjob_dir) if pjob_dir else ""
    if not sta_file:
        sta_file = str(BASE_DIR / "config" / "stations.txt")

    cfg: dict = {
        "region": {
            "name"      : meta.get("name", ""),
            "lat_min"   : lat_min,
            "lat_max"   : lat_max,
            "lon_min"   : lon_min,
            "lon_max"   : lon_max,
            "lat"       : (lat_min + lat_max) / 2.0,
            "lon"       : (lon_min + lon_max) / 2.0,
            "depth_max" : float(region.get("depth_max", 60.0)),
            "starttime" : (region.get("starttime") or "")[:19].replace("T", " "),
            "endtime"   : (region.get("endtime")   or "")[:19].replace("T", " "),
        },
        "data": {
            "waveform_dir" : wv_dir,
            "sds_format"   : (pt == "sds"),
            "station_file" : sta_file,
            "inventory"    : str(BASE_DIR / "config" / "inventory.xml"),
            "job_dir"      : job_dir,
        },
    }

    # ── Association ─────────────────────────────────────────────────────────────
    if step == "assoc":
        cfg["associate"] = {}
        if method == "gamma":
            cfg["associate"]["gamma"] = {
                "max_sigma"              : float(params.get("max_sigma",  2.0)),
                "min_picks_per_event"    : int(params.get("min_picks",    5)),
                "min_picks_per_eq"       : int(params.get("min_picks",    5)),
                "min_p_picks"            : int(params.get("min_p",        1)),
                "min_s_picks"            : int(params.get("min_s",        0)),
                "method"                 : params.get("gmm_method",    "BGMM"),
                "use_amplitude"          : bool(params.get("use_amp",    True)),
                "max_time"               : float(params.get("max_time",  30.0)),
                "use_dbscan"             : bool(params.get("use_dbscan", False)),
                "dbscan_eps"             : float(params.get("dbscan_eps", 10.0)),
                "dbscan_min_samples"     : int(params.get("dbscan_min_samples",  4)),
                "dbscan_min_cluster_size": int(params.get("dbscan_min_cluster",  50)),
                "oversample_factor"      : int(params.get("oversample",          5)),
                "covariance_prior"       : [float(params.get("cov_time", 5.0)),
                                            float(params.get("cov_amp",  5.0))],
            }
        elif method == "real":
            # Keys must match what real.py run() reads: search/threshold/
            # tt_grid/velocity/lat_center/tt_db, producing correct -R/-S/-G/-V
            # flags (reproducing runREAL.pl). Old keys (tt_sum/np/ns/nps/nr)
            # are not read, so REAL uses defaults and -G is not sent (ttdb unused).
            cfg["associate"]["real"] = {
                "exec"       : params.get("exec",    "REAL"),
                "tt_db"      : params.get("tt_db", "config/ttdb.txt"),
                "lat_center" : float(params.get("lat_center", 0.951)),
                "search": {                                  # -R rx/rh/tdx/tdh/tint
                    "rx"   : float(params.get("rx",   0.1)),
                    "rh"   : float(params.get("rh",   20.0)),
                    "tdx"  : float(params.get("tdx",  0.02)),
                    "tdh"  : float(params.get("tdh",  2.0)),
                    "tint" : float(params.get("tint", 5.0)),
                },
                "tt_grid": {                                 # -G trx/trh/tdx/tdh
                    "trx"  : float(params.get("g_trx", 1.4)),
                    "trh"  : float(params.get("g_trh", 20.0)),
                    "tdx"  : float(params.get("g_tdx", 0.01)),
                    "tdh"  : float(params.get("g_tdh", 1.0)),
                },
                "velocity": {                                # -V vp0/vs0
                    "vp0"  : float(params.get("vp0", 6.2)),
                    "vs0"  : float(params.get("vs0", 3.4)),
                },
                "threshold": {        # -S np0/ns0/nps0/npsboth0/std0/dtps/nrt/drt
                    "np0"      : int(params.get("np0",       3)),
                    "ns0"      : int(params.get("ns0",       2)),
                    "nps0"     : int(params.get("nps0",      8)),
                    "npsboth0" : int(params.get("npsboth0",  2)),
                    "std0"     : float(params.get("std0",  0.5)),
                    "dtps"     : float(params.get("dtps",  0.1)),
                    "nrt"      : float(params.get("nrt",   1.2)),
                    "drt"      : float(params.get("drt",   0.0)),
                },
                "n_workers" : int(params.get("n_workers", 4)),
            }
        elif method == "pyocto":
            cfg["associate"]["pyocto"] = {
                "n_picks"             : int(params.get("n_picks", 10)),
                "n_p_picks"           : int(params.get("n_p_picks", 3)),
                "n_s_picks"           : int(params.get("n_s_picks", 0)),
                "n_p_and_s_picks"     : int(params.get("n_p_and_s_picks", 0)),
                "min_node_size"       : float(params.get("min_node_size", 10.0)),
                "pick_match_tolerance": float(params.get("pick_match_tolerance", 1.5)),
                "min_interevent_time" : float(params.get("min_interevent_time", 3.0)),
                "time_before"         : float(params.get("time_before", 300.0)),
                "velocity": {
                    "vp": float(params.get("vp0", 6.2)),
                    "vs": float(params.get("vs0", 3.4)),
                },
            }
        elif method == "glass3":
            cfg["associate"]["glass3"] = {
                "exec"                          : params.get("exec", "glass-app"),
                "nucleation_data_count_threshold": int(params.get("n_cut", 5)),
                "nucleation_stack_threshold"     : float(params.get("stack_thresh", 2.5)),
                "association_sd_cutoff"          : float(params.get("sd_cutoff", 6.0)),
                "node_resolution_km"              : float(params.get("resolution_km", 15.0)),
                "num_stations_per_node"           : int(params.get("num_stations_per_node", 10)),
                "n_threads"                       : int(params.get("n_threads", 2)),
                "shutdown_wait"                   : int(params.get("shutdown_wait", 60)),
                "reporting_data_threshold"        : int(params.get("reporting_data_threshold", 1)),
            }

    # ── Location ─────────────────────────────────────────────────────────────────
    elif step == "locate":
        cfg["locate"] = {}
        if method == "nlloc":
            # Travel-time grid is not built by the module; must point to an existing grid.
            # PORTABLE default: the global IASP91 grid bundled in the repo
            # (config/nlloc_grids/global, prefix "iasp91"). GUI "profile" = model_prefix.
            from seiswork.web._realtime_pipeline import _default_nlloc_grid_dir
            grid_dir = (params.get("grid_dir") or "").strip() or _default_nlloc_grid_dir()
            prefix   = (params.get("profile")  or "").strip() or "iasp91"
            cfg["locate"]["nlloc"] = {
                "exec"          : params.get("exec", "NLLoc"),
                "grid_dir"      : grid_dir,
                "model_prefix"  : prefix,
                "save_scatter"  : bool(params.get("save_scatter", False)),
                "profile"       : prefix,
            }
        elif method == "locsat":
            cfg["locate"]["locsat"] = {
                "exec"          : params.get("exec",   "LocSAT"),
                "model"         : params.get("model",  "iasp91"),
                "min_phases"    : int(params.get("min_phases", 4)),
            }

    # ── Magnitude ─────────────────────────────────────────────────────────────────
    # ML = log10(A_mm) + a*log10(R) + c*R + b  (Hutton & Boore 1987)
    # Key names MUST match what MLMagnitude (ml.py) reads:
    #   a_coefficient / b_coefficient / linear_coefficient / dist_max_km
    elif step == "magnitude":
        cfg["magnitude"] = {
            "a_coefficient"      : float(params.get("a",        1.110)),
            "b_coefficient"      : float(params.get("b",        0.591)),
            "linear_coefficient" : float(params.get("c",        0.00189)),
            "dist_max_km"        : float(params.get("max_dist", 700.0)),
            "inventory"          : params.get("paz_file", ""),
        }

    # ── Velocity ─────────────────────────────────────────────────────────────────
    # Key names match VelestVelocity (velest.py): damping_vel / damping_sta /
    # initial_model. Default damping vthet=10, stathet=1 (7G_Jailolo notebook).
    elif step == "velocity":
        cfg["velocity"] = {
            "velest": {
                "exec"          : params.get("exec",  "velest"),
                "mode"          : int(params.get("mode",    1)),
                "initial_model" : params.get("vp_model",  ""),
                "damping_vel"   : float(params.get("damp_vel", 10.0)),
                "damping_sta"   : float(params.get("damp_sta",  1.0)),
                "max_iter"      : int(params.get("max_iter",  10)),
                "vpvs_ratio"    : float(params.get("vpvs", 1.730)),
                # distmax velest.cmn (km): 7G_Jailolo ref = 100 (1000 raises RMS)
                "distmax"       : float(params.get("distmax", 100.0)),
                # lat/lon reference: GUI value, falls back to midpoint of region config
                "lat_ref"       : float(params.get("lat_ref") or (lat_min + lat_max) / 2),
                "lon_ref"       : float(params.get("lon_ref") or (lon_min + lon_max) / 2),
                # quality filter before VELEST (analogous to notebook filter_pha):
                # drop events with large gap or small mag for a cleaner RMS
                "filter_max_gap": float(params.get("max_gap", 220.0)),
                "filter_min_mag": float(params.get("min_mag",   0.1)),
                "filter_min_phase": int(params.get("min_phase",  4)),
            }
        }

    # ── Relocation ───────────────────────────────────────────────────────────────
    # Nested structure matches HypoDDRelocation (hypodd.py): rcfg["ph2dt"] and
    # rcfg["hypodd"]. Defaults = RECOMMENDED parameters from residual diagnostics
    # (run_hypodd_rekomendasi.ipynb): ph2dt MINWGHT=0 MAXDIST=60 MAXSEP=40
    # MAXNGH=10 MINLNK=8 MINOBS=8 MAXOBS=32; hypoDD DIST=60 OBSCT=8 WDCT=40 DAMP=70.
    elif step == "relocation":
        # crosscorr (FDTCC) and growclust use_fdtcc read continuous waveforms
        # straight from the SDS archive; expose it for the relocation step too.
        cfg["data_source"] = {"sds_path": wv_dir if pt == "sds" else ""}
        cfg["relocation"] = {
            "hypodd": {
                "exec"   : params.get("exec", "hypoDD"),
                "mode"   : params.get("mode", "catalog"),
                # User-edited .inp text (from GUI preview/edit). When present,
                # HypoDDRelocation writes it verbatim instead of auto-generating.
                "ph2dt_inp_text"  : (params.get("ph2dt_inp_text")  or "").strip() or None,
                "hypodd_inp_text" : (params.get("hypodd_inp_text") or "").strip() or None,
                "ph2dt": {
                    "min_wght"    : float(params.get("min_wght",   0.0)),
                    "max_dist_km" : float(params.get("max_dist",  60.0)),
                    "max_sep_km"  : float(params.get("max_sep",   40.0)),
                    "max_ngh"     : int(params.get("max_ngh",     10)),
                    "min_links"   : int(params.get("min_links",    8)),
                    "min_obs"     : int(params.get("min_obs",      8)),
                    "max_obs"     : int(params.get("max_obs",     32)),
                },
                "hypodd": {
                    "max_dist_km"    : float(params.get("max_dist", 60.0)),
                    "min_obs"        : int(params.get("obsct",       8)),
                    "wdct"           : float(params.get("wdct",     40.0)),
                    "damping"        : float(params.get("damping",  70.0)),
                    "vpvs"           : float(params.get("vpvs",     1.730)),
                    # Velocity model: "auto" | "iasp91" | "ak135" | "halmahera"
                    # "auto" = VELEST-updated config/velocity.mod, falls back to halmahera
                    "velocity_model" : params.get("velocity_model", "auto"),
                    # hypoDD.inp iteration block generated for `nset` sets,
                    # WRCT interpolated from wrct_start to wrct_end.
                    "nset"           : int(params.get("nset",          4)),
                    "niter_per_set"  : int(params.get("niter_per_set", 4)),
                    "wrct_start"     : float(params.get("wrct_start", 8.0)),
                    "wrct_end"       : float(params.get("wrct_end",   3.0)),
                },
                # cross-correlation (FDTCC waveform-similarity dt.cc), used when
                # mode == "crosscorr". Defaults reproduce the validated Demo band.
                "crosscorr": {
                    "exec_fdtcc"  : params.get("cc_exec_fdtcc", "FDTCC"),
                    "pre_filt"    : [2.0,
                                     float(params.get("cc_bplow",  2.0)),
                                     float(params.get("cc_bphigh", 8.0)), 8.0],
                    "cc_threshold": float(params.get("cc_threshold", 0.7)),
                    "snr_threshold": float(params.get("cc_snr",      1.0)),
                    "dt_max_sec"  : float(params.get("cc_dtmax",     2.0)),
                    "fdtcc_window": params.get("cc_window", "0.2/1.0/0.3/0.5/1.5/0.5"),
                    "fdtcc_grid"  : params.get("cc_grid",   "3/20/0.02/2"),
                },
            },
            # GrowClust (LOC-FLOW final relative-location refinement). Used
            # when mode == "growclust"; reuses event/station/dt.ct from hypoDD.
            "growclust": {
                "exec"        : params.get("gc_exec",     "growclust"),
                "rmin"        : float(params.get("gc_rmin",      0.6)),
                "delmax"      : float(params.get("gc_delmax",    120.0)),
                "rmsmax"      : float(params.get("gc_rmsmax",    0.2)),
                "ngoodmin"    : int(params.get("gc_ngoodmin",      8)),
                "iponly"      : int(params.get("gc_iponly",        0)),
                "nbranch_min" : int(params.get("gc_nbranch",       1)),
                "tt_dep1"     : float(params.get("gc_maxdep",    40.0)),
                "vpvs"        : float(params.get("vpvs",         1.730)),
                # GUI growclust uses catalog dt.ct->dt.cc (fast) by default; the
                # FDTCC waveform-CC path is exercised by the crosscorr mode.
                "use_fdtcc"   : bool(params.get("gc_use_fdtcc", False)),
            },
        }

    # ── Imaging (SIMUL2000 local-earthquake tomography) ──────────────────────────
    # Defaults = Jailolo grid from 7G_Jailolo.ipynb (cell 19). Grid node lists may
    # be passed as comma/space-separated strings from the GUI; parse to floats.
    elif step == "imaging":
        def _nodes(key, fallback):
            raw = params.get(key)
            if raw is None or raw == "":
                return fallback
            if isinstance(raw, (list, tuple)):
                return [float(v) for v in raw]
            return [float(v) for v in str(raw).replace(",", " ").split()]

        scfg = {
            "exec"        : params.get("exec", "simul2000"),
            "eqks_source" : params.get("eqks_source", "hypodd"),
            "checkerboard": bool(params.get("checkerboard", False)),
            "checker_percent": float(params.get("checker_percent", 5.0)),
            "target_layer": int(params.get("target_layer", 6)),
            "ref_lat"     : float(params.get("ref_lat") or (lat_min + lat_max) / 2),
            "ref_lon"     : float(params.get("ref_lon") or (lon_min + lon_max) / 2),
            "ref_elev"    : float(params.get("ref_elev", 0.0)),
            "vpvs"        : float(params.get("vpvs", 1.716)),
            "bld"         : float(params.get("bld", 0.1)),
            "damp_p"      : float(params.get("damp_p", 15.0)),
            "damp_s"      : float(params.get("damp_s", 10.0)),
            "hitct"       : float(params.get("hitct", 5.0)),
        }
        for k in ("x_nodes", "y_nodes", "z_nodes", "vp_vals"):
            v = _nodes(k, None)
            if v:
                scfg[k] = v
        scfg["z_from_velest"] = bool(params.get("z_from_velest", True))
        # Auto-resolve the latest completed VELEST + HypoDD job dirs for this
        # config so tomography consumes them (velest.mod/.sta/final.CNV +
        # hypoDD.reloc/phase). GUI overrides via velest_dir/hypodd_dir win.
        _cid = meta.get("id", "")
        scfg["velest_dir"] = params.get("velest_dir") or \
            _latest_job_dir(_cid, "velocity", require=("velest.mod",))
        # HypoDD dir must carry BOTH the reloc and a phase file (Hypodd2simul2000
        # reads them from the same folder); the bare latest reloc job may lack it.
        scfg["hypodd_dir"] = params.get("hypodd_dir") or \
            _latest_job_dir(_cid, "relocation", require=("hypoDD.reloc",),
                            require_any=("velesttohypo.pha", "hypodd_phase.pha", "phase.pha"))
        cfg["imaging"] = {"simul2000": scfg}

    # ── Detection (template matching: Match&Locate) ──────────────────────────────
    elif step == "detect":
        # M&L reads continuous waveforms straight from the SDS archive.
        cfg["data_source"] = {"sds_path": wv_dir if pt == "sds" else ""}
        cfg["detection"] = {
            "matchlocate": {
                "exec_ml"         : params.get("ml_exec",      "MatchLocate2"),
                "exec_select"     : params.get("select_exec",  "SelectFinal"),
                "template_min_mag": float(params.get("ml_min_mag",   0.5)),
                "max_templates"   : int(params.get("ml_max_tmpl",    10)),
                "comp"            : params.get("ml_comp",      "HHZ"),
                "dist_max_deg"    : float(params.get("ml_distmax",   0.3)),
                "tleng_sec"       : float(params.get("ml_tleng",    40.0)),
                "both_ps"         : int(params.get("ml_bothps",      0)),
                "phase0"          : params.get("ml_phase0",    "S"),
                "multiphase"      : int(params.get("ml_multiphase",  0)),
                "bp_low"          : float(params.get("ml_bplow",     2.0)),
                "bp_high"         : float(params.get("ml_bphigh",    8.0)),
                "target_sr"       : float(params.get("ml_targetsr", 100.0)),
                "search_R"        : params.get("ml_searchR", "0.0/0.0/0"),
                "search_I"        : params.get("ml_searchI", "0.01/0.01/0.5"),
                "T_window"        : params.get("ml_twindow", "2.0/0.5/1.5"),
                "H_thresh"        : params.get("ml_hthresh", "0.0/7.0"),
                "D_intd"          : float(params.get("ml_dintd",     3.0)),
                "scan_start"      : params.get("ml_scan_start") or None,
                "scan_end"        : params.get("ml_scan_end")   or None,
                "growclust_refine": bool(params.get("ml_gc_refine", False)),
            }
        }

    # ── Mechanism (focal mechanism inversion via SKHASH/HASH) ────────────────────
    elif step == "mechanism":
        def _nodes_mech(key, fallback):
            raw = params.get(key)
            if raw is None or raw == "":
                return fallback
            if isinstance(raw, (list, tuple)):
                return [float(v) for v in raw]
            return [float(v) for v in str(raw).replace(",", " ").split()]

        scfg = {
            "network"            : params.get("network", "7G"),
            "channel"            : params.get("channel") or None,  # None = auto HH>BH>EH>SH per station
            "params": {
                "npolmin" : int(params.get("npolmin", 5)),
                "max_agap": float(params.get("max_agap", 300)),
                "max_pgap": float(params.get("max_pgap", 90)),
                "nmc"     : int(params.get("nmc", 30)),
            },
        }
        # look_dep/nd0 override SKHASH's hardcoded [0,39,3] travel-time lookup
        # depth range, needed for subduction settings (e.g. Jailolo slab
        # seismicity routinely exceeds 39 km).
        look_dep = _nodes_mech("look_dep", None)
        if look_dep:
            scfg["params"]["look_dep"] = [int(v) for v in look_dep]
        if params.get("nd0"):
            scfg["params"]["nd0"] = int(params.get("nd0"))
        z_nodes = _nodes_mech("z_nodes", None)
        vp_vals = _nodes_mech("vp_vals", None)
        if z_nodes:
            scfg["z_nodes"] = z_nodes
        if vp_vals:
            scfg["vp_vals"] = vp_vals
        cfg["mechanism"] = {"skhash": scfg}

    # ── Optional magnitude merged into assoc/locate ──────────────────────────────
    # "Compute Magnitude" checkbox on the association page: the output catalog
    # directly includes the mag (ML) column. cfg["magnitude"]["compute"] signals the runner.
    if step in ("assoc", "locate") and params.get("compute_magnitude"):
        cfg["magnitude"] = {
            "compute"            : True,
            "a_coefficient"      : float(params.get("mag_a",        1.110)),
            "b_coefficient"      : float(params.get("mag_b",        0.591)),
            "linear_coefficient" : float(params.get("mag_c",        0.00189)),
            "dist_max_km"        : float(params.get("mag_max_dist", 700.0)),
            "inventory"          : params.get("mag_paz", ""),
        }

    return cfg


def _pipe_worker(job_id: str, cfg: dict, step: str,
                 method: str, input_file: str, cfg_id: str = "") -> None:
    """Background thread: write temp YAML, launch pipeline subprocess, stream log."""
    import subprocess, sys as _sys

    job_dir = PIPE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    log_path    = job_dir / "output.log"
    status_path = job_dir / "status.json"
    cfg_path    = job_dir / "run_config.yaml"

    def _ws(s: dict) -> None:
        status_path.write_text(json.dumps(s, indent=2))
        _pipe_jobs[job_id] = dict(s)

    # Descriptive label: distinguish relocation sub-modes (catalog / crosscorr /
    # growclust) and the detection method in the job list / Output page.
    label = method
    if step == "relocation":
        label = cfg.get("relocation", {}).get("hypodd", {}).get("mode", method)
    elif step == "detect":
        label = "matchlocate"

    status: dict = {
        "id"      : job_id,
        "step"    : step,
        "method"  : method,
        "mode"    : label,
        "cfg_id"  : cfg_id,
        "state"   : "running",
        "started" : datetime.now().isoformat(timespec="seconds"),
    }
    _ws(status)

    try:
        cfg_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
        runner = _ensure_pipe_runner()

        cmd = [_sys.executable, str(runner),
               str(cfg_path), str(BASE_DIR), step, method, job_id]
        if input_file:
            cmd.append(input_file)

        with open(log_path, "w") as log_fh:
            proc = subprocess.Popen(
                cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            _pipe_procs[job_id] = proc
            proc.wait()
            rc = proc.returncode

        if _pipe_jobs.get(job_id, {}).get("state") == "stopped":
            status["state"] = "stopped"
        elif rc == 0:
            status["state"] = "done"
            # Try to count output events
            for fname in (
                "catalog_associated.csv",   # GaMMA / REAL canonical output
                "catalog_gamma.csv",
                "catalog_real.csv",
                "catalog_nlloc.csv",
                "catalog_locsat.csv",
                "catalog_ml.csv",
                "catalog_velest.csv",
                "velest_vel.csv",
                "hypodd_reloc.csv",
                "catalog_relocated.csv",   # fallback HypoDD (no_reloc)
                "catalog_matchlocate.csv", # Match&Locate detections
                "OUT/out.csv",             # SKHASH focal mechanisms
                "catalog.csv", "events.csv",
            ):
                fpath = job_dir / fname
                if fpath.exists():
                    try:
                        import pandas as _pd
                        df = _pd.read_csv(fpath)
                        status["events"] = len(df)
                        status["result_file"] = str(fpath)
                    except Exception:
                        pass
                    break
        else:
            status["state"] = "error"
            status["returncode"] = rc
    except Exception as exc:
        status["state"] = "error"
        status["error"] = str(exc)
        with open(log_path, "a") as lf:
            lf.write(f"\n[flask] Exception: {exc}\n")

    status["finished"] = datetime.now().isoformat(timespec="seconds")
    _ws(status)
    _pipe_procs.pop(job_id, None)


def _input_file_owner_error(input_file: str, cfg_id: str) -> str:
    """If input_file resolves into a PIPE_DIR/PICK_DIR job folder, that job's
    status.json must belong to the same cfg_id, otherwise a caller could chain
    a pipeline step off another project's job by passing its path directly.
    Paths outside those job folders (e.g. an operator-picked external file) are
    left alone since they carry no cfg_id of their own."""
    if not input_file:
        return ""
    try:
        p = Path(input_file).resolve()
    except Exception:
        return ""
    for base in (PIPE_DIR, PICK_DIR):
        try:
            base_r = base.resolve()
        except Exception:
            continue
        if str(p).startswith(str(base_r) + os.sep):
            job_id = p.relative_to(base_r).parts[0]
            sf = base_r / job_id / "status.json"
            if sf.is_file():
                try:
                    s = json.loads(sf.read_text())
                except Exception:
                    s = {}
                if s.get("cfg_id", "") != cfg_id:
                    return f"input_file belongs to a job outside cfg_id {cfg_id}"
            return ""
    return ""


@app.route("/api/pipeline/run", methods=["POST"])
def pipeline_run():
    data       = request.get_json(force=True)
    cfg_id     = data.get("cfg_id", "").strip()
    step       = data.get("step",   "").strip().lower()
    method     = data.get("method", "").strip().lower()
    params     = data.get("params", {})
    input_file = data.get("input_file", "").strip()

    valid_steps = {
        "assoc"     : ("gamma", "real", "pyocto", "glass3"),
        "locate"    : ("nlloc", "locsat"),
        "magnitude" : ("ml",),
        "velocity"  : ("velest",),
        "relocation": ("hypodd",),
        "imaging"   : ("simul2000",),
        "detect"    : ("matchlocate",),
        "mechanism" : ("skhash",),
    }

    if step not in valid_steps:
        return jsonify({"error": f"Unknown step: {step}"}), 400
    if method not in valid_steps[step]:
        return jsonify({"error": f"Unknown method {method!r} for step {step!r}"}), 400
    if not cfg_id:
        return jsonify({"error": "cfg_id required"}), 400

    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": f"Config {cfg_id} not found"}), 404

    owner_err = _input_file_owner_error(input_file, cfg_id)
    if owner_err:
        return jsonify({"error": owner_err}), 403

    meta   = json.loads(meta_file.read_text())
    job_id = str(uuid.uuid4())[:8]
    cfg    = _build_pipe_cfg(meta, step, method, params, job_id, input_file)

    t = threading.Thread(
        target=_pipe_worker,
        args=(job_id, cfg, step, method, input_file, cfg_id),
        daemon=True,
    )
    _job_threads[job_id] = t
    t.start()

    return jsonify({"id": job_id, "state": "running", "step": step, "method": method})


@app.route("/api/pipeline/relocation/preview-inp", methods=["POST"])
def relocation_preview_inp():
    """Generate ph2dt.inp + hypoDD.inp from current parameters WITHOUT running,
    so the user can review and correct before executing (see run_hypodd_rekomendasi.ipynb)."""
    data   = request.get_json(force=True)
    cfg_id = data.get("cfg_id", "").strip()
    params = data.get("params", {})
    if not cfg_id:
        return jsonify({"error": "cfg_id required"}), 400
    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": f"Config {cfg_id} not found"}), 404

    meta = json.loads(meta_file.read_text())
    # Discard override text so the preview is always from parameters (not old text).
    params = {**params, "ph2dt_inp_text": "", "hypodd_inp_text": ""}
    cfg  = _build_pipe_cfg(meta, "relocation", "hypodd", params, "preview", "")
    try:
        from seiswork.modules.relocation.hypodd import HypoDDRelocation
        rel = HypoDDRelocation(cfg, str(BASE_DIR))
        out = rel.generate_inp_preview()
        return jsonify(out)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


def _offline_job_dir(job_id: str) -> "Path | None":
    """Return local sync path for job_id (pipeline only) if it exists."""
    try:
        from seiswork.client import SEISWORK_HOME
        d = SEISWORK_HOME / "remote" / "pipeline" / job_id
        return d if d.is_dir() else None
    except Exception:
        return None


def _offline_sync_dir() -> "Path | None":
    """Return ~/.seiswork/remote/pipeline if it exists."""
    try:
        from seiswork.client import SEISWORK_HOME
        d = SEISWORK_HOME / "remote" / "pipeline"
        return d if d.is_dir() else None
    except Exception:
        return None


@app.route("/api/pipeline/jobs", methods=["GET"])
def list_pipe_jobs():
    step       = request.args.get("step", "")
    filter_cfg = request.args.get("cfg_id", "").strip()
    if not filter_cfg:
        return jsonify({"error": "cfg_id required"}), 400
    live = _live_runner_job_ids()
    result: list[dict] = []
    if PIPE_DIR.exists():
        for d in sorted(PIPE_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            sf = d / "status.json"
            if sf.is_file():
                try:
                    j = json.loads(sf.read_text())
                    if step and j.get("step") != step:
                        continue
                    if filter_cfg and j.get("cfg_id") != filter_cfg:
                        continue
                    if not j.get("id"):
                        j["id"] = d.name
                    j = _reconcile_state(j, sf, live, job_dir=d)
                    # Flag jobs that used dt.cc (cross-correlation differential times)
                    if j.get("step") == "relocation":
                        j["has_dtcc"] = any(
                            (d / p).is_file() and (d / p).stat().st_size > 0
                            for p in ("dt.cc", "IN/dt.cc")
                        )
                    result.append(j)
                except Exception:
                    pass
    # Fallback: add locally synced jobs not already in result
    sync_dir = _offline_sync_dir()
    if sync_dir:
        existing_ids = {j.get("id") for j in result}
        for d in sorted(sync_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if d.name in existing_ids or not d.is_dir():
                continue
            sf = d / "status.json"
            if not sf.is_file():
                continue
            try:
                j = json.loads(sf.read_text())
                if step and j.get("step") != step:
                    continue
                if filter_cfg and j.get("cfg_id") != filter_cfg:
                    continue
                if not j.get("id"):
                    j["id"] = d.name
                if j.get("step") == "relocation":
                    j["has_dtcc"] = any(
                        (d / p).is_file() and (d / p).stat().st_size > 0
                        for p in ("dt.cc", "IN/dt.cc")
                    )
                j["_local"] = True
                result.append(j)
            except Exception:
                pass
    return jsonify(result)


@app.route("/api/pipeline/jobs/<job_id>/log", methods=["GET"])
def pipeline_log(job_id):
    log_path = PIPE_DIR / job_id / "output.log"
    if not log_path.exists():
        # Fallback to local sync dir
        sd = _offline_job_dir(job_id)
        if sd and (sd / "output.log").exists():
            log_path = sd / "output.log"
        else:
            return jsonify({"lines": [], "offset": 0, "state": "unknown"})

    offset = int(request.args.get("offset", 0))
    tail   = int(request.args.get("tail",   0))   # >0: return last N lines, set offset=file_end

    state = "running"
    sf    = PIPE_DIR / job_id / "status.json"
    if sf.exists():
        try:
            state = json.loads(sf.read_text()).get("state", "running")
        except Exception:
            pass
    if state != "running" and job_id in _live_runner_job_ids():
        state = "running"

    if tail > 0:
        # Efficient reverse-read: read last chunk from file to get tail lines
        fsize = log_path.stat().st_size
        chunk = min(fsize, tail * 200)   # ~200 bytes/line estimate
        with open(log_path, "rb") as fh:
            fh.seek(max(0, fsize - chunk))
            raw = fh.read()
        all_lines = raw.decode("utf-8", errors="replace").splitlines()
        lines = all_lines[-tail:]
        return jsonify({"lines": lines, "offset": fsize, "state": state,
                        "tail": True, "total_size": fsize})

    with open(log_path, "rb") as fh:
        fh.seek(offset)
        raw         = fh.read()
        next_offset = fh.tell()

    lines = raw.decode("utf-8", errors="replace").splitlines()
    return jsonify({"lines": lines, "offset": next_offset, "state": state})


@app.route("/api/pipeline/jobs/<job_id>/reallog", methods=["GET"])
def pipeline_reallog(job_id):
    """Return last N lines of the most recently modified real_YYYYMMDD.log for a job.
    Checks job-specific log dir first, then global fallback (old convention).
    """
    tail = int(request.args.get("tail", 20))
    job_log_dir    = PIPE_DIR / job_id / "logs" / "real"
    global_log_dir = BASE_DIR / "work" / "logs" / "real"
    candidates = []
    for d in [job_log_dir, global_log_dir]:
        if d.is_dir():
            candidates.extend(d.glob("real_*.log"))
    if not candidates:
        return jsonify({"lines": [], "name": None})
    latest = max(candidates, key=lambda f: f.stat().st_mtime)
    try:
        raw       = latest.read_bytes()
        all_lines = raw.decode("utf-8", errors="replace").splitlines()
        return jsonify({
            "lines": all_lines[-tail:],
            "name" : latest.name,
            "mtime": latest.stat().st_mtime,
            "size" : latest.stat().st_size,
        })
    except Exception as e:
        return jsonify({"lines": [], "name": None, "error": str(e)})


@app.route("/api/pipeline/jobs/<job_id>", methods=["DELETE"])
def stop_pipe_job(job_id):
    proc = _pipe_procs.get(job_id)
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
        _pipe_jobs[job_id] = {**_pipe_jobs.get(job_id, {}), "state": "stopped"}
        sf = PIPE_DIR / job_id / "status.json"
        if sf.exists():
            try:
                d = json.loads(sf.read_text()); d["state"] = "stopped"
                sf.write_text(json.dumps(d, indent=2))
            except Exception:
                pass
    else:
        job_dir = PIPE_DIR / job_id
        if job_dir.exists():
            shutil.rmtree(job_dir)
        _pipe_jobs.pop(job_id, None)
        _job_threads.pop(job_id, None)
    return jsonify({"message": "ok"})


@app.route("/api/pipeline/jobs/<job_id>/phase_text", methods=["GET"])
def pipeline_job_phase_text(job_id):
    """Return the phase file in HypoDD text format, paginated by event.
    ?page=N&per_page=M  (default page=0, per_page=200)"""
    from datetime import datetime as _dt
    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        return jsonify({"error": "Job not found"}), 404

    page     = int(request.args.get("page", 0))
    per_page = min(int(request.args.get("per_page", 200)), 1000)

    def _paginate(blocks, src):
        total_ev = len(blocks)
        start = page * per_page
        end   = min(start + per_page, total_ev)
        content = "\n".join(blocks[start:end])
        return jsonify({
            "content"  : content,
            "n_events" : total_ev,
            "page"     : page,
            "per_page" : per_page,
            "has_more" : end < total_ev,
            "source"   : src,
        })

    # Use pre-generated .pha file if available
    pha_file = job_dir / "hypodd_phase.pha"
    if pha_file.exists():
        raw = pha_file.read_text()
        # Split into per-event blocks preserving the leading '#'
        parts  = raw.split("\n#")
        blocks = [parts[0]] + ["#" + p for p in parts[1:]]
        blocks = [b for b in blocks if b.strip()]
        return _paginate(blocks, "pha")

    # Generate from catalog + picks CSV
    import pandas as pd
    cat_names  = ["catalog_associated.csv", "catalog_gamma.csv", "catalog_real.csv",
                  "catalog_nlloc.csv", "catalog_locsat.csv", "catalog_located.csv"]
    cat_names += [p.name for p in sorted(job_dir.glob("catalog_*_filter.csv"))]
    pick_names = ["picks_associated.csv", "picks_gamma.csv", "picks_real.csv"]

    cat_df = None
    for nm in cat_names:
        p = job_dir / nm
        if p.exists():
            try: cat_df = pd.read_csv(p); break
            except Exception: pass
    if cat_df is None:
        return jsonify({"error": "No catalog CSV found"}), 404

    pick_df = None
    for nm in pick_names:
        p = job_dir / nm
        if p.exists():
            try: pick_df = pd.read_csv(p); break
            except Exception: pass

    # Build per-event blocks
    event_blocks = []
    for _, ev in cat_df.iterrows():
        ot_str = str(ev.get("datetime", ""))
        try:
            ot = _dt.fromisoformat(ot_str.replace("Z", "").split("+")[0])
        except Exception:
            continue
        lat   = float(ev.get("lat",  0) or 0)
        lon   = float(ev.get("lon",  0) or 0)
        dep   = float(ev.get("depth_km", ev.get("depth", 0)) or 0)
        mag   = float(ev.get("mag",  0) or 0)
        nsta  = int(ev.get("nsta",   0) or 0)
        rms   = float(ev.get("rms",  0) or 0)
        ev_id = str(ev.get("event_id", ""))
        ev_lines = [
            f"# {ot.year:4d} {ot.month:02d} {ot.day:02d} "
            f"{ot.hour:02d} {ot.minute:02d} {ot.second:02d}.{ot.microsecond // 1000:03d} "
            f"{lat:.4f} {lon:.4f} {dep:.3f} {mag:.2f} "
            f"{rms:.2f} 0.00 0.00 {nsta}"
        ]
        if pick_df is not None:
            ev_picks = None
            for id_col in ("event_id", "event_index"):
                if id_col in pick_df.columns:
                    ev_picks = pick_df[pick_df[id_col].astype(str) == ev_id]
                    break
            if ev_picks is not None and not ev_picks.empty:
                for _, pk in ev_picks.iterrows():
                    sta   = str(pk.get("station", "")).strip()
                    phase = str(pk.get("type", pk.get("phase_hint", pk.get("phase", "P")))).upper()
                    if phase not in ("P", "S"):
                        continue
                    ts_str = ""
                    for ts_col in ("timestamp", "pick_time", "phase_time"):
                        v = str(pk.get(ts_col, ""))
                        if v and v != "nan":
                            ts_str = v.replace("Z", "").split("+")[0]; break
                    try:
                        pt = _dt.fromisoformat(ts_str)
                        tt = round((pt - ot).total_seconds(), 3)
                        if 0 < tt < 1200:
                            ev_lines.append(f" {sta:<8s} {tt:7.3f} 1 {phase}")
                    except Exception:
                        pass
        event_blocks.append("\n".join(ev_lines))

    return _paginate(event_blocks, "generated")


@app.route("/api/pipeline/jobs/<job_id>/velocity_model", methods=["GET"])
def pipeline_job_velocity_model(job_id):
    """Return initial + VELEST output velocity model layers for plotting."""
    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        job_dir = _offline_job_dir(job_id) or job_dir
    if not job_dir.exists():
        return jsonify({"error": "Job not found"}), 404

    def _parse_velest_mod(path):
        """
        Parse velest.mod (SimulFlow/VELEST format):
          Line 0 : title
          Line 1 : "N   vel,depth,vdamp,phase ..."  (N = layer count)
          Lines  : "vel  depth  damp  [P-VELOCITY MODEL]"  x N
          Line   : "N"                                (starts S section)
          Lines  : "vel  depth  damp  [S-VELOCITY MODEL]"  x N
        """
        import re
        lines = Path(path).read_text().splitlines()
        title = lines[0].strip() if lines else ""
        p_layers, s_layers = [], []
        section = None          # "P" | "S"
        skip_next_count = False

        for ln in lines[1:]:
            s = ln.strip()
            if not s:
                continue
            parts = s.split()
            # Count-only line: single integer (or "12  vel,depth...")
            first = parts[0]
            if re.match(r'^\d+$', first):
                if section is None:
                    section = "P"   # first count line: entering P section
                elif section == "P":
                    section = "S"   # second count line: entering S section
                continue            # skip count lines
            # Data line: float  float  [float  ...]
            try:
                vel   = float(parts[0])
                depth = float(parts[1])
                if section == "P":
                    p_layers.append({"vel": round(vel, 3), "depth": round(depth, 3)})
                elif section == "S":
                    s_layers.append({"vel": round(vel, 3), "depth": round(depth, 3)})
            except (ValueError, IndexError):
                continue

        return {"title": title, "p": p_layers, "s": s_layers}

    result = {}
    # Initial model: prefer saved backup, else check user-provided initial_model path
    initial_mod = job_dir / "velest_initial.mod"
    if initial_mod.exists():
        try:
            result["initial"] = _parse_velest_mod(str(initial_mod))
        except Exception:
            pass
    if "initial" not in result:
        # Fallback: return hardcoded Halmahera 1D
        from seiswork.modules.velocity.velest import MODEL_HALMAHERA
        vpvs = 1.730
        result["initial"] = {
            "title": "Halmahera 1D (default)",
            "p": [{"vel": L["vp"],           "depth": L["depth"]} for L in MODEL_HALMAHERA],
            "s": [{"vel": round(L["vp"]/vpvs,3), "depth": L["depth"]} for L in MODEL_HALMAHERA],
        }

    # Output model: velest.mod after run
    output_mod = job_dir / "velest.mod"
    if output_mod.exists():
        try:
            result["output"] = _parse_velest_mod(str(output_mod))
        except Exception:
            pass

    if not result:
        return jsonify({"error": "No velocity model found"}), 404
    return jsonify(result)


@app.route("/api/pipeline/jobs/<job_id>/velest_rms", methods=["GET"])
def pipeline_job_velest_rms(job_id):
    """Return VELEST RMS-per-iteration from main.OUT for convergence plot."""
    job_dir  = PIPE_DIR / job_id
    main_out = job_dir / "main.OUT"
    if not main_out.exists():
        # Fallback: check work/velocity (legacy single-dir runs)
        main_out = BASE_DIR / "work" / "velocity" / "main.OUT"
    if not main_out.exists():
        return jsonify({"error": "main.OUT not found"}), 404
    try:
        from seiswork.modules.velocity.velest import VelestVelocity
        iters = VelestVelocity.parse_main_out_rms(str(main_out))
        return jsonify({"iterations": iters, "n": len(iters)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload/velocity_model", methods=["POST"])
def upload_velocity_model():
    """Upload a custom VELEST velocity model (.mod/.txt) file."""
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f   = request.files["file"]
    fn  = f.filename or "velest_custom.mod"
    # Sanitize filename
    fn  = "".join(c for c in fn if c.isalnum() or c in "._- ")[:80]
    dst_dir = WORK_DIR / "velocity" / "models"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / fn
    f.save(str(dst))
    # Quick validation: try parse
    try:
        content = dst.read_text()
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        n_data = sum(1 for l in lines if l and l[0].isdigit() or l[0] in "0123456789-")
        return jsonify({"path": str(dst), "filename": fn, "lines": len(lines), "n_data": n_data})
    except Exception as e:
        return jsonify({"path": str(dst), "filename": fn, "error": str(e)})


@app.route("/api/pipeline/jobs/<job_id>/files", methods=["GET"])
def list_pipe_files(job_id):
    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        return jsonify([])
    files = []
    for f in sorted(job_dir.rglob("*")):
        if f.is_file():
            st = f.stat()
            files.append({
                "name"    : f.name,
                "rel"     : str(f.relative_to(job_dir)),
                "path"    : str(f),
                "size"    : st.st_size,
                "mtime"   : datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "download": f.suffix in (".csv", ".yaml", ".log", ".json", ".txt"),
            })
    return jsonify(files)


_IMAGING_LAYER_FIG_RE = re.compile(r"^(Tomo_Results_Topo|DWS_Results_Grayscale)_z(\d+)_(-?\d+)km\.png$")


def _imaging_figure_label(name: str, labels: dict) -> str:
    """Friendly label for a tomography figure filename: static map for the
    single-layer legacy names, regex for the per-layer 'velocity'/'dws' set
    (one PNG per depth layer, see tomography.py _make_plots)."""
    if name in labels:
        return labels[name]
    m = _IMAGING_LAYER_FIG_RE.match(name)
    if m:
        kind, iz, depth = m.group(1), int(m.group(2)), m.group(3)
        title = "Vp tomogram" if kind == "Tomo_Results_Topo" else "DWS (ray coverage)"
        return f"{title} — layer {iz} (Z={depth} km)"
    return Path(name).stem.replace("_", " ").title()


@app.route("/api/mechanism/<job_id>/results", methods=["GET"])
def mechanism_results(job_id):
    """Read the SKHASH out.csv (one row per accepted strike/dip/rake solution,
    multiple rows per event_id when SKHASH reports several plausible
    mechanisms) for the Mechanism page's table + map + 3D viewer."""
    job_dir = PIPE_DIR / job_id
    out_csv = job_dir / "OUT" / "out.csv"
    if not out_csv.exists():
        return jsonify([])
    import pandas as _pd
    df = _pd.read_csv(out_csv)
    df = df.where(_pd.notnull(df), None)
    return jsonify(df.to_dict(orient="records"))


@app.route("/api/mechanism/<job_id>/figures", methods=["GET"])
def list_mechanism_figures(job_id):
    """List SKHASH beachball PNGs (one per event_id, outfolder_plots)."""
    job_dir = PIPE_DIR / job_id
    out_dir = job_dir / "OUT"
    if not out_dir.exists():
        return jsonify([])
    figs = [{"event_id": f.stem, "name": f.name,
             "rel": str(f.relative_to(job_dir))}
            for f in sorted(out_dir.glob("*.png"))]
    return jsonify(figs)


@app.route("/api/mechanism/<job_id>/figure", methods=["GET"])
def serve_mechanism_figure(job_id):
    """Serve one SKHASH beachball PNG inline."""
    job_dir = (PIPE_DIR / job_id).resolve()
    rel = (request.args.get("name", "") or "").strip()
    if not rel:
        return jsonify({"error": "name required"}), 400
    target = (job_dir / rel).resolve()
    if not str(target).startswith(str(job_dir)) or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(target), mimetype="image/png")


@app.route("/api/mechanism/<job_id>/event/<event_id>/stations", methods=["GET"])
def mechanism_event_stations(job_id, event_id):
    """Per-event station list for the polarity map: lat/lon/polarity/azimuth/
    takeoff from SKHASH's own out_polinfo.csv (already has everything needed,
    no station.csv join required), joined with the P pick_time from
    picks_associated.csv (needed by the waveform endpoint below)."""
    import pandas as _pd
    job_dir = PIPE_DIR / job_id
    polinfo_csv = job_dir / "OUT" / "out_polinfo.csv"
    if not polinfo_csv.exists():
        return jsonify({"error": "out_polinfo.csv not found for this job"}), 404

    df = _pd.read_csv(polinfo_csv)
    df = df[df["event_id"].astype(str) == str(event_id)]
    if df.empty:
        return jsonify({"error": f"Event {event_id} not found"}), 404

    pick_time_by_sta = {}
    picks_csv = job_dir / "picks_associated.csv"
    if picks_csv.exists():
        pdf = _pd.read_csv(picks_csv)
        pdf = pdf[(pdf["event_id"].astype(str) == str(event_id)) & (pdf["phase"] == "P")]
        pick_time_by_sta = dict(zip(pdf["station"], pdf["pick_time"]))

    row0 = df.iloc[0]
    event = {
        "lat": float(row0["origin_lat"]), "lon": float(row0["origin_lon"]),
        "depth_km": float(row0["origin_depth_km"]), "mag": float(row0.get("event_mag", 0) or 0),
    }
    stations = []
    for _, r in df.iterrows():
        stations.append({
            "station": r["sta_code"], "lat": float(r["station_lat"]), "lon": float(r["station_lon"]),
            "polarity": float(r["p_polarity"]) if _pd.notna(r["p_polarity"]) else None,
            "azimuth": float(r["azimuth"]) if _pd.notna(r["azimuth"]) else None,
            "takeoff": float(r["takeoff"]) if _pd.notna(r["takeoff"]) else None,
            "dist_km": float(r["sr_dist_km"]) if _pd.notna(r["sr_dist_km"]) else None,
            "pick_time": pick_time_by_sta.get(r["sta_code"]),
        })
    return jsonify({"event": event, "stations": stations})


@app.route("/api/mechanism/<job_id>/event/<event_id>/waveform", methods=["GET"])
def mechanism_event_waveform(job_id, event_id):
    """Fetch the raw vertical-component waveform around one station's P pick,
    for the same event/station the polarity (compression/dilatation) was
    derived from in mechanism.polarity.first_motion_polarity(). Lets the
    user visually verify the SKHASH-convention call (red/up = compression,
    white/down = dilatation) against the actual seismogram."""
    import pandas as _pd
    import yaml as _yaml
    import numpy as np
    from obspy import UTCDateTime
    from obspy.clients.filesystem.sds import Client as SDSClient

    station = (request.args.get("station", "") or "").strip()
    if not station:
        return jsonify({"error": "station required"}), 400

    job_dir = PIPE_DIR / job_id
    status_file = job_dir / "status.json"
    if not status_file.exists():
        return jsonify({"error": "Job not found"}), 404
    cfg_id = json.loads(status_file.read_text()).get("cfg_id", "")

    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": "Config not found"}), 404
    wv_cfg = json.loads(meta_file.read_text()).get("waveform", {})
    sds_path = wv_cfg.get("path") or wv_cfg.get("fdsn", {}).get("output_path", "")
    if not sds_path or wv_cfg.get("path_type", "sds") != "sds":
        return jsonify({"error": "SDS waveform path not configured for this config"}), 400

    picks_csv = job_dir / "picks_associated.csv"
    if not picks_csv.exists():
        return jsonify({"error": "picks_associated.csv not found for this job"}), 404
    pdf = _pd.read_csv(picks_csv)
    prow = pdf[(pdf["event_id"].astype(str) == str(event_id)) &
               (pdf["station"] == station) & (pdf["phase"] == "P")]
    if prow.empty:
        return jsonify({"error": f"No P pick for {station} on event {event_id}"}), 404
    network = str(prow.iloc[0]["network"])
    pick_time = str(prow.iloc[0]["pick_time"])

    channel = None  # None = auto-detect via read_best_waveform (HH>BH>EH>SH)
    run_cfg_file = job_dir / "run_config.yaml"
    if run_cfg_file.exists():
        try:
            run_cfg = _yaml.safe_load(run_cfg_file.read_text()) or {}
            channel = run_cfg.get("mechanism", {}).get("skhash", {}).get("channel") or None
        except Exception:
            pass

    polinfo_csv = job_dir / "OUT" / "out_polinfo.csv"
    polarity = None
    if polinfo_csv.exists():
        pol_df = _pd.read_csv(polinfo_csv)
        prow2 = pol_df[(pol_df["event_id"].astype(str) == str(event_id)) & (pol_df["sta_code"] == station)]
        if not prow2.empty and _pd.notna(prow2.iloc[0]["p_polarity"]):
            polarity = float(prow2.iloc[0]["p_polarity"])

    NOISE_WIN, SIG_WIN = 1.0, 0.4   # same defaults as mechanism.polarity.compute_polarities()
    PRE, POST = 2.0, 3.0

    t0 = UTCDateTime(pick_time)
    try:
        sds = SDSClient(sds_path)
        st, channel_used = read_best_waveform(sds, network, station, t0 - PRE, t0 + POST, channel=channel)
    except Exception as ex:
        return jsonify({"error": f"SDS read failed: {ex}"}), 500
    if not st:
        return jsonify({"error": "no waveform data in this window"}), 404
    channel = channel_used

    tr = st[0].copy()
    tr.detrend("demean")
    sr = tr.stats.sampling_rate
    n = tr.stats.npts
    times = (np.arange(n) / sr) - PRE   # seconds relative to the P pick (t=0)
    amps = tr.data.astype(float)

    MAX_PTS = 1500
    if n > MAX_PTS:
        step = n // MAX_PTS
        times = times[::step]
        amps = amps[::step]

    return jsonify({
        "station": station, "network": network, "channel": channel,
        "times": times.tolist(), "amps": amps.tolist(),
        "noise_win": NOISE_WIN, "sig_win": SIG_WIN,
        "polarity": polarity,
    })


# ── FocoNet focal mechanism routes ──────────────────────────────────────────

@app.route("/api/mechanism/<job_id>/foconet/results", methods=["GET"])
def foconet_results(job_id):
    """FocoNet result CSV, one row per event (strike/dip/rake/n_pol/...)."""
    job_dir = PIPE_DIR / job_id
    out_csv = job_dir / "foconetout" / "foconet_out.csv"
    if not out_csv.exists():
        return jsonify([])
    import pandas as _pd
    df = _pd.read_csv(out_csv)
    df = df.where(_pd.notnull(df), None)
    return jsonify(df.to_dict(orient="records"))


@app.route("/api/mechanism/<job_id>/foconet/figures", methods=["GET"])
def list_foconet_figures(job_id):
    """List FocoNet beachball PNGs (one per event_id in foconetout/)."""
    job_dir = PIPE_DIR / job_id
    out_dir = job_dir / "foconetout"
    if not out_dir.exists():
        return jsonify([])
    figs = [{"event_id": f.stem, "name": f.name,
             "rel": "foconetout/" + f.name}
            for f in sorted(out_dir.glob("*.png"))]
    return jsonify(figs)


@app.route("/api/mechanism/<job_id>/foconet/figure", methods=["GET"])
def serve_foconet_figure(job_id):
    """Serve one FocoNet beachball PNG inline."""
    job_dir = (PIPE_DIR / job_id).resolve()
    rel = (request.args.get("name", "") or "").strip()
    if not rel:
        return jsonify({"error": "name required"}), 400
    target = (job_dir / rel).resolve()
    if not str(target).startswith(str(job_dir)) or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(target), mimetype="image/png")


@app.route("/api/mechanism/<job_id>/foconet/event/<event_id>/stations", methods=["GET"])
def foconet_event_stations(job_id, event_id):
    """Per-event station list for FocoNet polarity map.
    Reads station_rows.json written by FocoNetRunner.run()."""
    import pandas as _pd, yaml as _yaml
    job_dir = PIPE_DIR / job_id
    sta_json = job_dir / "foconetout" / "station_rows.json"
    if not sta_json.exists():
        return jsonify({"error": "station_rows.json not found"}), 404

    sta_map = json.loads(sta_json.read_text())
    srows = sta_map.get(str(event_id)) or sta_map.get(event_id)
    if srows is None:
        return jsonify({"error": f"Event {event_id} not found"}), 404

    out_csv = job_dir / "foconetout" / "foconet_out.csv"
    event = {"lat": None, "lon": None, "depth_km": None}
    if out_csv.exists():
        df = _pd.read_csv(out_csv)
        row = df[df["event_id"].astype(str) == str(event_id)]
        if not row.empty:
            r = row.iloc[0]
            event = {"lat": float(r.get("origin_lat", 0)),
                     "lon": float(r.get("origin_lon", 0)),
                     "depth_km": float(r.get("origin_depth_km", 0))}

    # Compute azimuth from dx/dy for display
    import math
    stations = []
    for s in srows:
        dx = s.get("dx_km", 0)
        dy = s.get("dy_km", 0)
        dist_km = math.sqrt(dx**2 + dy**2)
        az = (math.degrees(math.atan2(dx, dy)) + 360) % 360
        stations.append({
            "station":  s["station"], "lat": s["lat"], "lon": s["lon"],
            "polarity": s["polarity"], "snr": s.get("snr"),
            "dist_km":  round(dist_km, 2), "azimuth": round(az, 1),
            "dx_km":    dx, "dy_km": dy,
        })
    return jsonify({"event": event, "stations": stations})


@app.route("/api/imaging/<job_id>/figures", methods=["GET"])
def list_imaging_figures(job_id):
    """List SIMUL2000 tomography figures (PNG) produced by an imaging job."""
    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        return jsonify([])
    # Friendly labels for the known single-layer figure filenames (legacy;
    # current runs produce one Vp/DWS figure per depth layer instead, labeled
    # via _imaging_figure_label()).
    labels = {
        "parameterisasi_simul.png"   : "Grid parameterization XYZ (events · stations, 2D map)",
        "Tomo_Results_Topo.png"      : "Vp tomogram 2D (depth slice + cross-sections)",
        "DWS_Results_Grayscale.png"  : "Derivative Weight Sum (ray coverage / resolution)",
        "Curved_Raypaths_Synthetic.png": "Ray tracing 2D (curved raypaths)",
        "Checkerboard_Results.png"   : "Checkerboard resolution test",
    }
    figs = []
    for f in sorted(job_dir.rglob("*.png")) + sorted(job_dir.rglob("*.html")):
        if not f.is_file():
            continue
        figs.append({
            "name"        : f.name,
            "rel"         : str(f.relative_to(job_dir)),
            "label"       : _imaging_figure_label(f.name, labels),
            "interactive" : f.suffix == ".html",
            "mtime"       : datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
        })
    return jsonify(figs)


@app.route("/api/imaging/<job_id>/figure", methods=["GET"])
def serve_imaging_figure(job_id):
    """Serve one tomography PNG inline (for the Imaging page <img>)."""
    job_dir = (PIPE_DIR / job_id).resolve()
    rel = (request.args.get("name", "") or "").strip()
    if not rel:
        return jsonify({"error": "name required"}), 400
    target = (job_dir / rel).resolve()
    if not str(target).startswith(str(job_dir)) or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    mime = "text/html" if target.suffix == ".html" else "image/png"
    return send_file(str(target), mimetype=mime)


def _imaging_plotter(sim_dir: Path):
    """Build a SimulFlow Simul2000Plotter for a finished imaging job's
    simulout dir. _import_plotter() needs simulflow on sys.path, normally
    done by Simul2000Tomography.__init__ (never called from these read-only
    routes), so it's repeated here (same convention as tomography.py)."""
    from seiswork.modules.velocity.tomography import Simul2000Tomography

    simulflow_src = str(BASE_DIR / "core" / "simulflow" / "src")
    if not os.path.isdir(simulflow_src):
        simulflow_src = str(Path.home() / "apps" / "simulflow" / "src")
    if simulflow_src not in sys.path:
        sys.path.insert(0, simulflow_src)

    Simul2000Plotter = Simul2000Tomography._import_plotter()
    return Simul2000Plotter(work_dir=str(sim_dir))


@app.route("/api/imaging/<job_id>/velocity_grid", methods=["GET"])
def imaging_velocity_grid(job_id):
    """SIMUL2000 Vp (+ DWS, if available) as a JSON node grid (lon/lat per
    X/Y node, depth per Z node, field[iz][iy][ix]) for the Imaging page's
    interactive 3D viewer: one Plotly `surface` per depth layer, same
    lon/lat-from-km convention as the SimulFlow PNG plots."""
    job_dir = PIPE_DIR / job_id
    sim_dir = job_dir / "simulout"
    if not sim_dir.is_dir():
        return jsonify({"error": "no simulout dir for this job"}), 404

    plotter = _imaging_plotter(sim_dir)

    import numpy as np
    x_nodes, y_nodes, z_nodes = plotter._read_mod("MOD")
    if not x_nodes:
        return jsonify({"error": "MOD grid not found/empty"}), 404
    vp_file = "velomod.out" if (sim_dir / "velomod.out").is_file() else "MOD"
    vp_init = plotter._read_velo_matrix("MOD")       # initial model, always needed for perturbation
    vp = plotter._read_velo_matrix(vp_file)          # final model, shape (nz, ny, nx)
    olat, olon, _, _ = plotter._read_stns("STNS")

    km_lat = 111.19
    km_lon = 111.19 * math.cos(math.radians(olat))
    lons = [olon + x / km_lon for x in x_nodes]
    lats = [olat + y / km_lat for y in y_nodes]

    resp = {
        "lons": lons, "lats": lats, "depths": z_nodes,
        "vp": vp.tolist(), "vp_file": vp_file,
        "vmin": float(vp.min()), "vmax": float(vp.max()),
    }
    # Perturbation %, mirrors Simul2000Plotter.plot_velocity exactly:
    # dv = clip((vp_final - vp_ref) / vp_ref * 100, -15, 15)
    # This is what the 2D PNG contour plots display; the 3D viewer must use
    # the same quantity so the spatial patterns match.
    if vp_init is not None:
        vp_ref = np.where(vp_init == 0, 1.0, vp_init)
        dv = np.clip(((vp - vp_ref) / vp_ref) * 100.0, -15.0, 15.0)
        resp["dv"] = dv.tolist()
        resp["dv_min"] = float(dv.min())
        resp["dv_max"] = float(dv.max())
    if (sim_dir / "output").is_file():
        dws = plotter._read_dws_matrix("output", len(x_nodes), len(y_nodes), len(z_nodes))
        if dws is not None:
            resp["dws"] = dws.tolist()
            resp["dws_min"] = float(dws.min())
            resp["dws_max"] = float(dws.max())
    return jsonify(resp)


@app.route("/api/imaging/<job_id>/velocity_isosurface", methods=["GET"])
def imaging_velocity_isosurface(job_id):
    """Dense 3D isosurface of Vp perturbation %, bounded by earthquake depth
    range and DWS raypath coverage: a 3D Slicer-inspired contour interpolation:

    1. Sparse SIMUL2000 grid (e.g. 17x17x12), cubic RegularGridInterpolator,
       dense grid (n x n x n_dep) clipped to seismicity + station footprint.
       This mirrors Slicer's "interpolation between user-drawn slice contours".

    2. DWS binary coverage mask, Gaussian blur, smooth weight field (~ the
       signed-distance-function taper Slicer applies near contour boundaries).
       Nodes with no raypath coverage fade smoothly to dv=0, so the isosurface
       at +/-threshold stays naturally inside the ray-illuminated region.

    3. Depth taper: smooth fade in top+bottom 15% of the seismicity span.
       Off-cluster earthquakes in finalsmpout automatically extend this span
       (their raypaths also widen the DWS illumination laterally).

    Client renders two isosurfaces (±threshold) via Plotly type='isosurface'.

    Query params
    -----------
    threshold  float  default 2.0  |dVp %| level for the anomaly surfaces
    dws_min    float  default 1.0  minimum DWS to classify a node as "covered"
    n          int    default 45   dense-grid resolution per lateral axis (≤60)
    max_depth  float  optional     override deepest depth bound (km)
    """
    job_dir = PIPE_DIR / job_id
    sim_dir = job_dir / "simulout"
    if not sim_dir.is_dir():
        return jsonify({"error": "no simulout dir for this job"}), 404

    import numpy as np
    from scipy.interpolate import RegularGridInterpolator
    from scipy.ndimage import gaussian_filter

    threshold         = float(request.args.get("threshold", 2.0))
    dws_min           = float(request.args.get("dws_min", 1.0))
    n                 = min(int(request.args.get("n", 45)), 60)
    max_depth_arg     = request.args.get("max_depth")

    plotter = _imaging_plotter(sim_dir)
    x_nodes, y_nodes, z_nodes = plotter._read_mod("MOD")
    if not x_nodes:
        return jsonify({"error": "MOD grid not found"}), 404

    nx, ny, nz = len(x_nodes), len(y_nodes), len(z_nodes)
    vp_init  = plotter._read_velo_matrix("MOD")
    vp_file  = "velomod.out" if (sim_dir / "velomod.out").is_file() else "MOD"
    vp_final = plotter._read_velo_matrix(vp_file)
    olat, olon, sta_lats, sta_lons = plotter._read_stns("STNS")
    if vp_init is None or vp_final is None:
        return jsonify({"error": "velocity matrices unreadable"}), 404

    # DWS (proxy for raypath density)
    dws = None
    if (sim_dir / "output").is_file():
        dws = plotter._read_dws_matrix("output", nx, ny, nz)

    # Perturbation %, identical formula to plot_velocity / velocity_grid
    vp_ref = np.where(vp_init == 0, 1.0, vp_init)
    dv     = np.clip(((vp_final - vp_ref) / vp_ref) * 100.0, -15.0, 15.0)

    km_lat    = 111.19
    km_lon    = 111.19 * math.cos(math.radians(olat))
    grid_lons = np.array([olon + x / km_lon for x in x_nodes])
    grid_lats = np.array([olat + y / km_lat for y in y_nodes])
    z_arr     = np.array(z_nodes, dtype=float)

    # Sort axes ascending (RegularGridInterpolator requires monotonic)
    sx = np.argsort(grid_lons); sy = np.argsort(grid_lats); sz = np.argsort(z_arr)
    grid_lons = grid_lons[sx]; grid_lats = grid_lats[sy]; z_arr = z_arr[sz]
    dv = dv[np.ix_(sz, sy, sx)]
    if dws is not None:
        dws = dws[np.ix_(sz, sy, sx)]

    # All earthquakes including off-cluster: extend depth + lateral bounds
    eq_file = "finalsmpout" if (sim_dir / "finalsmpout").is_file() else "EQKS"
    eq_lats, eq_lons, eq_depths = plotter._read_eqks(eq_file)
    if not eq_depths:
        return jsonify({"error": "no earthquakes in " + eq_file}), 404

    dep_min = max(0.0, float(min(eq_depths)))
    dep_max = float(min(max(eq_depths), z_arr.max()))
    if max_depth_arg:
        dep_max = min(float(max_depth_arg), z_arr.max())

    # Lateral bounds: union of all events + stations (off-cluster events extend this)
    all_lons = list(eq_lons) + (list(sta_lons) if sta_lons else [])
    all_lats = list(eq_lats) + (list(sta_lats) if sta_lats else [])
    lon_lo = max(float(grid_lons.min()), min(all_lons) - 0.05)
    lon_hi = min(float(grid_lons.max()), max(all_lons) + 0.05)
    lat_lo = max(float(grid_lats.min()), min(all_lats) - 0.05)
    lat_hi = min(float(grid_lats.max()), max(all_lats) + 0.05)

    # Dense grid: depth axis scaled proportionally to seismicity span
    depth_span  = max(1.0, dep_max - dep_min)
    grid_span   = max(1.0, float(z_arr.max() - z_arr.min()))
    n_dep       = max(15, min(n, int(n * depth_span / grid_span)))
    dense_lons  = np.linspace(lon_lo, lon_hi, n)
    dense_lats  = np.linspace(lat_lo, lat_hi, n)
    dense_deps  = np.linspace(dep_min, dep_max, n_dep)

    # Build interpolators
    interp_dv = RegularGridInterpolator(
        (z_arr, grid_lats, grid_lons), dv,
        method="cubic", bounds_error=False, fill_value=0.0)

    D, La, Lo = np.meshgrid(dense_deps, dense_lats, dense_lons, indexing="ij")
    pts        = np.column_stack([D.ravel(), La.ravel(), Lo.ravel()])
    dv_dense   = interp_dv(pts).reshape(n_dep, n, n)

    # Combined mask: DWS raypath coverage AND Vp anomaly signal from velomod.out.
    # Intersection of both constraints so the isosurface body only appears where
    # raypaths actually illuminate AND a velocity anomaly exists in the output.
    # Each mask is Gaussian-smoothed before intersection (Slicer SDF taper effect).
    abs_dv_sm = gaussian_filter(np.abs(dv_dense).astype(np.float32), sigma=1.5)
    # Anomaly mask: smoothed |dv| >= 30% of threshold (signal above noise floor)
    covered_dv = (abs_dv_sm >= threshold * 0.30).astype(np.float32)

    if dws is not None:
        interp_dws = RegularGridInterpolator(
            (z_arr, grid_lats, grid_lons), dws.astype(float),
            method="linear", bounds_error=False, fill_value=0.0)
        dws_dense     = interp_dws(pts).reshape(n_dep, n, n)
        covered_dws   = (dws_dense >= dws_min).astype(np.float32)
        # Intersection: DWS coverage x Vp anomaly presence
        covered_both  = covered_dws * covered_dv
    else:
        # No DWS file: guide isosurface by Vp anomaly signal only
        covered_both = covered_dv

    # Gaussian blur the combined binary mask into a smooth weight field
    # (equivalent to SDF taper: soft fade at body boundary, not a hard cutoff)
    weight = gaussian_filter(covered_both, sigma=2.0)
    mx     = weight.max()
    weight = np.clip(weight / mx if mx > 0 else weight, 0.0, 1.0)

    # Depth taper: smooth cosine fade in top+bottom 15% of seismicity span,
    # prevents the isosurface from having a flat horizontal cap at the bounds
    margin     = depth_span * 0.15
    dep_weight = np.ones(n_dep)
    for i, d in enumerate(dense_deps):
        if d < dep_min + margin and margin > 0:
            t = (d - dep_min) / margin
            dep_weight[i] = 0.5 * (1 - math.cos(math.pi * t))  # cosine ease-in
        elif d > dep_max - margin and margin > 0:
            t = (dep_max - d) / margin
            dep_weight[i] = 0.5 * (1 - math.cos(math.pi * t))  # cosine ease-out

    # Final masked field: dv fades to 0 outside the (DWS+anomaly) bounded
    # region, so the Plotly isosurface at +/-threshold stays inside the
    # ray-illuminated + anomalous volume
    dv_masked = dv_dense * weight * dep_weight[:, None, None]

    return jsonify({
        "x":        Lo.ravel().round(5).tolist(),
        "y":        La.ravel().round(5).tolist(),
        "z":        D.ravel().round(3).tolist(),
        "value":    np.round(dv_masked.ravel(), 3).tolist(),
        "threshold": threshold,
        "dep_min":  round(dep_min, 2),
        "dep_max":  round(dep_max, 2),
        "n_points": int(dv_masked.size),
        "vp_file":  vp_file,
        "eq_file":  eq_file,
        "n_events": len(eq_depths),
    })


def _imaging_events_stations(plotter, sim_dir: Path):
    """(events, stations) for the Imaging 3D viewer: lon/lat/depth hypocenters
    + lon/lat/code stations. Shared by the lightweight events_stations route
    and the heavier raypaths route (which also needs the raw lat/lon lists)."""
    olat, olon, sta_lats, sta_lons = plotter._read_stns("STNS")
    sta_codes = []
    stns_path = sim_dir / "STNS"
    if stns_path.is_file():
        for line in stns_path.read_text().splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 3 and len(line) >= 20:
                sta_codes.append(parts[0])
    eq_file = "finalsmpout" if (sim_dir / "finalsmpout").is_file() else "EQKS"
    eq_lats, eq_lons, eq_depths = plotter._read_eqks(eq_file)

    events = [{"lon": eq_lons[i], "lat": eq_lats[i], "depth": eq_depths[i]} for i in range(len(eq_lons))]
    stations = [
        {"lon": sta_lons[j], "lat": sta_lats[j],
         "code": sta_codes[j] if j < len(sta_codes) else f"STA{j+1}"}
        for j in range(len(sta_lons))
    ]
    return events, stations, (eq_lats, eq_lons, eq_depths, sta_lats, sta_lons)


@app.route("/api/imaging/<job_id>/events_stations", methods=["GET"])
def imaging_events_stations(job_id):
    """Lightweight hypocenters + stations for the Imaging 3D viewer (no
    event-station pairing). Used to focus the depth axis on the actual
    seismicity and to always show station/event markers regardless of
    whether the (heavier) raypath overlay is toggled on."""
    job_dir = PIPE_DIR / job_id
    sim_dir = job_dir / "simulout"
    if not sim_dir.is_dir():
        return jsonify({"error": "no simulout dir for this job"}), 404
    plotter = _imaging_plotter(sim_dir)
    events, stations, _ = _imaging_events_stations(plotter, sim_dir)
    if not events or not stations:
        return jsonify({"error": "EQKS/STNS not found or empty"}), 404
    depths = [e["depth"] for e in events]
    lons = [p["lon"] for p in events + stations]
    lats = [p["lat"] for p in events + stations]
    return jsonify({
        "events": events, "stations": stations,
        "depth_min": min(depths), "depth_max": max(depths),
        "lon_min": min(lons), "lon_max": max(lons),
        "lat_min": min(lats), "lat_max": max(lats),
    })


@app.route("/api/imaging/<job_id>/raypaths", methods=["GET"])
def imaging_raypaths(job_id):
    """Event-station raypaths for the Imaging 3D viewer, as 3D pseudo-bent
    curves, same synthetic bending formula as SimulFlow's
    plot_curved_raypaths() (see Curved_Raypaths_Synthetic.png), just emitted
    as JSON polylines instead of a matplotlib LineCollection. Not a true
    SIMUL2000-traced ray path; a visual approximation of source-to-station bend."""
    job_dir = PIPE_DIR / job_id
    sim_dir = job_dir / "simulout"
    if not sim_dir.is_dir():
        return jsonify({"error": "no simulout dir for this job"}), 404

    max_dist = float(request.args.get("max_distance_km", 250.0))
    max_rays = int(request.args.get("max_rays", 3000))

    plotter = _imaging_plotter(sim_dir)
    events, stations, raw = _imaging_events_stations(plotter, sim_dir)
    eq_lats, eq_lons, eq_depths, sta_lats, sta_lons = raw
    if not events or not stations:
        return jsonify({"error": "EQKS/STNS not found or empty"}), 404

    pairs = []
    for i in range(len(eq_lons)):
        for j in range(len(sta_lons)):
            dist = plotter._haversine(eq_lons[i], eq_lats[i], sta_lons[j], sta_lats[j])
            if dist <= max_dist:
                pairs.append((i, j, dist))

    n_total = len(pairs)
    if n_total > max_rays:
        stride = n_total / max_rays
        pairs = [pairs[int(k * stride)] for k in range(max_rays)]

    n_segs = 14
    ts = [k / (n_segs - 1) for k in range(n_segs)]
    rays = []
    for i, j, dist in pairs:
        lon0, lat0, dep0 = eq_lons[i], eq_lats[i], eq_depths[i]
        lon1, lat1 = sta_lons[j], sta_lats[j]
        curvature = dist / 300.0
        ray = []
        for t in ts:
            lon = lon0 + (lon1 - lon0) * t
            lat = lat0 + (lat1 - lat0) * t
            linear_z = dep0 * (1 - t)
            bend_z = linear_z + curvature * dep0 * 4 * t * (1 - t)
            ray.append([lon, lat, bend_z])
        rays.append(ray)

    return jsonify({
        "n_total": n_total, "n_shown": len(rays), "max_distance_km": max_dist,
        "rays": rays, "events": events, "stations": stations,
    })


@app.route("/api/pipeline/jobs/<job_id>/download", methods=["GET"])
def download_pipe_file(job_id):
    """Serve one job artifact by its path relative to the job dir. Used by the
    client auto-sync to mirror finished-job outputs (catalog CSV, logs)."""
    job_dir = (PIPE_DIR / job_id).resolve()
    rel = (request.args.get("name", "") or "").strip()
    if not rel:
        return jsonify({"error": "name required"}), 400
    target = (job_dir / rel).resolve()
    # Path-traversal guard: target must stay inside the job dir.
    if not str(target).startswith(str(job_dir)) or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(target), as_attachment=True, download_name=target.name)


# ── Unified output browser (folder-per-run + file viewer) ───────────────────────
# Every run (pick / associate / locate / velocity / relocation / detection) leaves
# a job folder. These endpoints expose those folders and their files so the Output
# page can show a folder-icon space and open any file in a modal with its content
# and full path.
_OUTPUT_BASES = (("pipeline", lambda: PIPE_DIR), ("picking", lambda: PICK_DIR))


def _find_job_dir(job_id: str):
    """Return (kind, job_dir) for a job id across pipeline + picking, else (None, None)."""
    for kind, base in _OUTPUT_BASES:
        jd = (base() / job_id).resolve()
        if jd.is_dir() and str(jd).startswith(str(base().resolve())):
            return kind, jd
    return None, None


@app.route("/api/output/folders", methods=["GET"])
def output_folders():
    """One entry per run folder (pipeline + picking) for a config, newest first."""
    filter_cfg = request.args.get("cfg_id", "").strip()
    if not filter_cfg:
        return jsonify({"error": "cfg_id required"}), 400
    out = []
    for kind, base in _OUTPUT_BASES:
        d0 = base()
        if not d0.exists():
            continue
        for d in d0.iterdir():
            sf = d / "status.json"
            if not sf.is_file():
                continue
            try:
                s = json.loads(sf.read_text())
            except Exception:
                s = {}
            if filter_cfg and s.get("cfg_id") != filter_cfg:
                continue
            n_files = sum(1 for f in d.rglob("*") if f.is_file())
            out.append({
                "job_id"  : d.name,
                "kind"    : kind,
                "step"    : s.get("step", kind),
                "method"  : s.get("mode") or s.get("method", ""),
                "state"   : s.get("state", ""),
                "finished": s.get("finished", "") or s.get("started", ""),
                "n_files" : n_files,
                "dir"     : str(d),
            })
    out.sort(key=lambda x: x.get("finished", ""), reverse=True)
    return jsonify(out)


@app.route("/api/output/files", methods=["GET"])
def output_files():
    """List files inside one run folder (relative path, size, mtime)."""
    job_id = request.args.get("job", "").strip()
    kind, jd = _find_job_dir(job_id)
    if not jd:
        return jsonify({"error": "job not found"}), 404
    files = []
    for f in sorted(jd.rglob("*")):
        if f.is_file():
            st = f.stat()
            files.append({
                "name" : f.name,
                "rel"  : str(f.relative_to(jd)),
                "size" : st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            })
    return jsonify({"job_id": job_id, "kind": kind, "dir": str(jd), "files": files})


@app.route("/api/output/file", methods=["GET"])
def output_file_content():
    """Return one file's text content (for the viewer modal) + its full path."""
    job_id = request.args.get("job", "").strip()
    rel    = (request.args.get("name", "") or "").strip()
    _kind, jd = _find_job_dir(job_id)
    if not jd or not rel:
        return jsonify({"error": "job and name required"}), 400
    target = (jd / rel).resolve()
    if not str(target).startswith(str(jd)) or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    size = target.stat().st_size
    MAXB = 1024 * 1024                          # 1 MB cap for inline view
    raw  = target.read_bytes()[:MAXB]
    # FORTRAN mixed-binary/text files (e.g. main.OUT, *.OUT) contain null bytes
    # in record separators but are mostly readable text.  Decode first with
    # errors='replace' and only flag truly binary (images, archives, etc.) when
    # >30 % of the sampled bytes are non-printable (not tab/LF/CR/space/ASCII).
    text = raw.decode("utf-8", errors="replace")
    sample = raw[:8192]
    n_nonprint = sum(
        1 for b in sample
        if b < 9 or (13 < b < 32) or b == 127
    )
    is_bin = len(sample) > 0 and (n_nonprint / len(sample)) > 0.30
    return jsonify({
        "path"     : str(target),
        "name"     : target.name,
        "size"     : size,
        "binary"   : is_bin,
        "truncated": size > MAXB,
        "content"  : "" if is_bin else text,
    })


@app.route("/api/output/download", methods=["GET"])
def output_download():
    """Download one run-folder file (works for both pipeline and picking)."""
    job_id = request.args.get("job", "").strip()
    rel    = (request.args.get("name", "") or "").strip()
    _kind, jd = _find_job_dir(job_id)
    if not jd or not rel:
        return jsonify({"error": "job and name required"}), 400
    target = (jd / rel).resolve()
    if not str(target).startswith(str(jd)) or not target.is_file():
        return jsonify({"error": "file not found"}), 404
    return send_file(str(target), as_attachment=True, download_name=target.name)


@app.route("/api/pipeline/jobs/<job_id>/preview", methods=["GET"])
def preview_pipe_result(job_id):
    """Return first N rows of result CSV as JSON for inline table display."""
    job_dir = PIPE_DIR / job_id
    # Prefer result_file recorded in status; fallback to first .csv
    sf = job_dir / "status.json"
    result_csv = None
    if sf.exists():
        try:
            result_csv = json.loads(sf.read_text()).get("result_file", "")
        except Exception:
            pass
    if not result_csv:
        csvs = list(job_dir.glob("*.csv"))
        result_csv = str(csvs[0]) if csvs else ""
    if not result_csv or not Path(result_csv).exists():
        # Fallback: look in local sync dir
        sd = _offline_job_dir(job_id)
        if sd:
            csvs = sorted(sd.glob("catalog_*.csv"), key=lambda f: f.stat().st_size, reverse=True)
            if not csvs:
                csvs = [f for f in sd.glob("*.csv") if "pick" not in f.name]
            result_csv = str(csvs[0]) if csvs else ""
    if not result_csv or not Path(result_csv).exists():
        return jsonify({"error": "No result file found"}), 404

    row_offset = int(request.args.get("offset", 0))
    row_limit  = min(int(request.args.get("limit", int(request.args.get("rows", 200)))), 2000)
    try:
        import csv as _csv
        columns: list = []
        rows: list    = []
        total = 0
        with open(result_csv, newline="", encoding="utf-8") as fh:
            for i, row in enumerate(_csv.reader(fh)):
                if i == 0:
                    columns = row
                    continue
                total += 1
                data_i = total - 1
                if data_i >= row_offset and len(rows) < row_limit:
                    rows.append(row)
        return jsonify({
            "columns" : columns,
            "rows"    : rows,
            "total"   : total,
            "offset"  : row_offset,
            "showing" : len(rows),
            "has_more": row_offset + row_limit < total,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/pipeline/jobs/<job_id>/assoc_stats", methods=["GET"])
def api_assoc_stats(job_id):
    """Compute association statistics for the stats modal:
    phase counts, RMS, magnitude distribution, depth, Mc, Wadati diagram."""
    import numpy as np
    import pandas as pd
    from scipy.stats import linregress

    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        job_dir = _offline_job_dir(job_id) or job_dir
    if not job_dir.exists():
        return jsonify({"error": "Job not found"}), 404

    # ── Load catalog ──────────────────────────────────────────────────────────
    # Prefer result_file from status.json (e.g. _filter catalog named
    # catalog_<method>_filter.csv), then fall back to standard name + glob *_filter.
    cat_df = pd.DataFrame()
    candidates: list[str] = []
    sf = job_dir / "status.json"
    if sf.exists():
        try:
            rf = json.loads(sf.read_text()).get("result_file", "")
            if rf and Path(rf).exists():
                candidates.append(Path(rf).name)
        except Exception:
            pass
    candidates += ["catalog_associated.csv", "catalog_gamma.csv", "catalog_real.csv",
                   "catalog_nlloc.csv", "catalog_locsat.csv"]
    candidates += [p.name for p in sorted(job_dir.glob("catalog_*_filter.csv"))]
    for nm in candidates:
        p = job_dir / nm
        if p.exists():
            cat_df = pd.read_csv(p)
            break
    if cat_df.empty:
        return jsonify({"error": "Catalog not found"}), 404

    # ── Load picks ────────────────────────────────────────────────────────────
    pick_df = pd.DataFrame()
    for nm in ("picks_associated.csv", "picks_gamma.csv", "picks_real.csv"):
        p = job_dir / nm
        if p.exists():
            pick_df = pd.read_csv(p)
            break

    # ── Normalize picks to unified columns (phase, pick_time, event_id) ──────
    if not pick_df.empty:
        if "type" in pick_df.columns and "event_index" in pick_df.columns:
            # GaMMA format
            pick_df = pick_df.rename(columns={"type": "phase", "timestamp": "pick_time"})
            pick_df = pick_df[pd.to_numeric(pick_df["event_index"],
                                             errors="coerce").fillna(-1) >= 0].copy()
            pick_df["event_id"] = pick_df["event_index"].astype(str)
            cat_df["event_id"]  = cat_df["event_id"].astype(str)
        else:
            pick_df["event_id"] = pick_df["event_id"].astype(str)
            cat_df["event_id"]  = cat_df["event_id"].astype(str)

    # ── Phase counts ──────────────────────────────────────────────────────────
    n_p = int((pick_df["phase"].str.upper() == "P").sum()) if not pick_df.empty else 0
    n_s = int((pick_df["phase"].str.upper() == "S").sum()) if not pick_df.empty else 0

    # ── Time series: events per day ───────────────────────────────────────────
    cat_df["dt"] = pd.to_datetime(cat_df["datetime"], utc=True, errors="coerce")
    cat_df["date"] = cat_df["dt"].dt.strftime("%Y-%m-%d")
    ts = cat_df.groupby("date").size().reset_index(name="count")
    ts_dates  = ts["date"].tolist()
    ts_counts = ts["count"].tolist()

    # ── RMS ───────────────────────────────────────────────────────────────────
    rms_vals = cat_df["rms"].dropna().tolist() if "rms" in cat_df else []

    # ── Magnitude distribution ────────────────────────────────────────────────
    mags = cat_df["mag"].dropna()
    mag_vals = mags.tolist()

    # ── Depth distribution ────────────────────────────────────────────────────
    dep_vals = cat_df["depth_km"].dropna().tolist() if "depth_km" in cat_df else []

    # ── Magnitude Completeness (Maximum Curvature) + GR b-value ──────────────
    mc = None
    gr_a = gr_b = None
    fmd_m = fmd_n_cum = fmd_n_inc = []
    if len(mags) > 10:
        m_min = float(np.floor(mags.min() * 10) / 10)
        m_max = float(np.ceil(mags.max()  * 10) / 10) + 0.05
        edges = np.arange(m_min, m_max + 0.1, 0.1)
        hist, _ = np.histogram(mags, bins=edges)
        centers = (edges[:-1] + edges[1:]) / 2
        # MAXC: Mc = center of bin with highest frequency
        mc_idx = int(np.argmax(hist))
        mc = float(round(centers[mc_idx], 1))
        # Cumulative FMD
        n_cum = np.array([int((mags >= m).sum()) for m in centers])
        fmd_m     = centers.tolist()
        fmd_n_cum = n_cum.tolist()
        fmd_n_inc = hist.tolist()
        # GR b-value: least-squares fit for M >= Mc
        mask = (centers >= mc) & (n_cum > 0)
        if mask.sum() >= 2:
            log_n = np.log10(n_cum[mask].astype(float))
            slope, intercept, *_ = linregress(centers[mask], log_n)
            gr_b = float(round(-slope, 3))
            gr_a = float(round(intercept, 3))

    # ── Wadati diagram ────────────────────────────────────────────────────────
    wadati_tp = wadati_ts_tp = []
    vp_vs = vp_vs_r2 = None
    wadati_fit_tp = wadati_fit_y = []

    if not pick_df.empty and "phase" in pick_df.columns and "pick_time" in pick_df.columns:
        try:
            pick_df["pick_dt"] = pd.to_datetime(pick_df["pick_time"], utc=True, errors="coerce")
            p_picks = (pick_df[pick_df["phase"].str.upper() == "P"]
                       [["event_id", "station", "pick_dt"]]
                       .rename(columns={"pick_dt": "tp_abs"}))
            s_picks = (pick_df[pick_df["phase"].str.upper() == "S"]
                       [["event_id", "station", "pick_dt"]]
                       .rename(columns={"pick_dt": "ts_abs"}))
            ps = p_picks.merge(s_picks, on=["event_id", "station"])
            cat_slim = cat_df[["event_id", "dt"]].dropna(subset=["dt"])
            ps = ps.merge(cat_slim, on="event_id")
            ps["tp"]    = (ps["tp_abs"] - ps["dt"]).dt.total_seconds()
            ps["ts_tp"] = (ps["ts_abs"] - ps["tp_abs"]).dt.total_seconds()
            ps = ps[(ps["tp"] > 0) & (ps["ts_tp"] > 0) &
                    (ps["tp"] < 300) & (ps["ts_tp"] < 300)].dropna(subset=["tp", "ts_tp"])
            if len(ps) >= 5:
                # Force-through-origin: Vp/Vs - 1 = Σ(tp*ts_tp) / Σ(tp²)
                tp_arr  = ps["tp"].to_numpy()
                dtp_arr = ps["ts_tp"].to_numpy()
                slope_orig = float(np.dot(tp_arr, dtp_arr) / np.dot(tp_arr, tp_arr))
                # With intercept (for quality check)
                sl, ic, r, *_ = linregress(tp_arr, dtp_arr)
                vp_vs    = float(round(slope_orig + 1.0, 3))
                vp_vs_r2 = float(round(r ** 2, 3))
                # Subsample for payload (max 4000 pts)
                if len(ps) > 4000:
                    ps = ps.sample(4000, random_state=42)
                wadati_tp    = ps["tp"].round(3).tolist()
                wadati_ts_tp = ps["ts_tp"].round(3).tolist()
                tp_fit = [0.0, float(np.percentile(tp_arr, 98))]
                wadati_fit_tp = tp_fit
                wadati_fit_y  = [slope_orig * t for t in tp_fit]
        except Exception as _we:
            pass

    return jsonify({
        "n_events"    : int(len(cat_df)),
        "n_picks_p"   : n_p,
        "n_picks_s"   : n_s,
        "ts_dates"    : ts_dates,
        "ts_counts"   : ts_counts,
        "rms"         : [round(v, 4) for v in rms_vals],
        "mags"        : [round(v, 2) for v in mag_vals],
        "depths"      : [round(v, 2) for v in dep_vals],
        "mc"          : mc,
        "gr_a"        : gr_a,
        "gr_b"        : gr_b,
        "fmd_m"       : [round(v, 1) for v in fmd_m],
        "fmd_n_cum"   : fmd_n_cum,
        "fmd_n_inc"   : fmd_n_inc,
        "wadati_tp"   : wadati_tp,
        "wadati_ts_tp": wadati_ts_tp,
        "vp_vs"       : vp_vs,
        "vp_vs_r2"    : vp_vs_r2,
        "wadati_fit_tp": wadati_fit_tp,
        "wadati_fit_y" : wadati_fit_y,
        "method"       : str(cat_df["method"].iloc[0]) if "method" in cat_df.columns and len(cat_df) else "",
        # lat/lon/depth/mag for 2D & 3D map (max 5000 pts, sub-sampled)
        "lons"  : cat_df["lon"].dropna().round(4).tolist()[:5000]
                  if "lon" in cat_df.columns else [],
        "lats"  : cat_df["lat"].dropna().round(4).tolist()[:5000]
                  if "lat" in cat_df.columns else [],
        "map_depths": cat_df["depth_km"].dropna().round(2).tolist()[:5000]
                      if "depth_km" in cat_df.columns else [],
        "map_mags"  : cat_df["mag"].dropna().round(2).tolist()[:5000]
                      if "mag" in cat_df.columns else [],
    })


@app.route("/api/pipeline/jobs/<job_id>/reloc_stats", methods=["GET"])
def api_reloc_stats(job_id):
    """HypoDD residual statistics from hypoDD.res, mirrors the analysis in
    run_hypodd_rekomendasi.ipynb (per P/S phase: N, Mean, Median, Std, RMS,
    Skewness, Kurtosis, %|res|>0.3s) + histogram + RMS convergence per iteration."""
    import numpy as np
    import pandas as pd
    from scipy import stats as _st

    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        job_dir = _offline_job_dir(job_id) or job_dir
    if not job_dir.exists():
        return jsonify({"error": "Job not found"}), 404

    res_path = job_dir / "hypoDD.res"
    if not res_path.exists():
        return jsonify({"error": "hypoDD.res not found — relocation produced no residuals "
                                 "(crosscorr/growclust mode or catalog too sparse)."}), 404

    # hypoDD.res has two variants: with header (STA DT C1 C2 IDX QUAL RES [ms]
    # WT OFFS) or without. Data rows ALWAYS have 9 columns by position:
    #   [0]STA [1]DT [2]EV1 [3]EV2 [4]PHA(3=P,4=S) [5]WT [6]RES_ms [7]CC [8]OFFS_m
    # Metric = actual RESIDUAL = RES column (idx 6, unit ms), converted to
    # seconds. Phase from idx 4, inter-event distance from last column / 1000.
    rows = []
    try:
        with open(res_path, errors="replace") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 7:
                    continue
                try:
                    res_s = float(parts[6]) / 1000.0   # RES [ms] -> seconds
                    ev1   = int(float(parts[2]))
                    ev2   = int(float(parts[3]))
                    pha   = int(float(parts[4]))        # 3=P, 4=S
                    offs  = float(parts[-1])
                except ValueError:
                    continue                            # header line: skip
                rows.append((parts[0], res_s, ev1, ev2, pha, offs))
    except Exception as e:
        return jsonify({"error": f"Failed to read hypoDD.res: {e}"}), 500

    if not rows:
        return jsonify({"error": "hypoDD.res is empty / unrecognized format"}), 404

    df = pd.DataFrame(rows, columns=["STA", "RES", "EV1", "EV2", "PHA", "OFFS"])
    df["DIST_KM"] = df["OFFS"] / 1000.0

    def _stats(d):
        d = d.dropna()
        if len(d) == 0:
            return None
        rms = float(np.sqrt(np.mean(d ** 2)))
        return {
            "n"        : int(len(d)),
            "mean"     : float(d.mean()),
            "median"   : float(d.median()),
            "std"      : float(d.std()),
            "rms"      : rms,
            "skew"     : float(_st.skew(d)) if len(d) >= 3 else None,
            "kurtosis" : float(_st.kurtosis(d)) if len(d) >= 3 else None,
            "pct_out"  : float((d.abs() > 0.3).mean() * 100),
        }

    df_p = df[df["PHA"] == 3]
    df_s = df[df["PHA"] == 4]

    # Histogram (frequency) for every phase, 1-99 percentile range (as in the notebook)
    def _hist(d, nbins=44):
        d = d.dropna().values
        if len(d) == 0:
            return {"x": [], "y": []}
        q_lo, q_hi = np.percentile(d, 1), np.percentile(d, 99)
        lim = max(abs(q_lo), abs(q_hi)) * 1.15 or 1.0
        edges = np.linspace(-lim, lim, nbins + 1)
        y, _ = np.histogram(d, bins=edges)
        x = ((edges[:-1] + edges[1:]) / 2).round(4)
        return {"x": x.tolist(), "y": y.tolist()}

    # Box plot of residuals per station (notebook sec 12); quartiles computed
    # server-side to keep it lightweight; sorted by median residual.
    box = []
    for sta, g in df.groupby("STA"):
        v = g["RES"].dropna().values
        if len(v) == 0:
            continue
        q1, med, q3 = (float(x) for x in np.percentile(v, [25, 50, 75]))
        iqr = q3 - q1
        lo  = float(v[v >= q1 - 1.5 * iqr].min()) if len(v) else q1
        hi  = float(v[v <= q3 + 1.5 * iqr].max()) if len(v) else q3
        box.append({"sta": str(sta), "n": int(len(v)),
                    "q1": q1, "median": med, "q3": q3,
                    "lo": lo, "hi": hi, "mean": float(v.mean())})
    box.sort(key=lambda b: b["median"])

    n_ev    = len(set(df["EV1"].tolist() + df["EV2"].tolist()))
    n_pairs = int(df.groupby(["EV1", "EV2"]).ngroups)

    # Relocated event coordinates (for 2D/3D map), parsed from hypoDD.reloc
    lons = lats = map_depths = map_mags = []
    sta_lon = sta_lat = sta_name = []
    for nm in ("hypodd_reloc.csv", "catalog_relocated.csv", "catalog_hypodd_cat.csv"):
        cp = job_dir / nm
        if cp.exists():
            try:
                cat = pd.read_csv(cp)
                if {"lat", "lon"}.issubset(cat.columns):
                    cat = cat.dropna(subset=["lat", "lon"]).head(8000)
                    lons = cat["lon"].round(4).tolist()
                    lats = cat["lat"].round(4).tolist()
                    map_depths = cat.get("depth_km", pd.Series([0] * len(cat))).fillna(0).round(2).tolist()
                    map_mags   = cat.get("mag", pd.Series([0] * len(cat))).fillna(0).round(2).tolist()
                    break
            except Exception:
                pass

    # Station coordinates (map overlay) from station file in job's run_config.yaml
    try:
        from seiswork.utils.converter import _load_station_df
        rc = job_dir / "run_config.yaml"
        sf = ""
        if rc.exists():
            rcfg = yaml.safe_load(rc.read_text()) or {}
            sf = (rcfg.get("data", {}) or {}).get("station_file", "")
            if sf and not os.path.isabs(sf):
                sf = str(BASE_DIR / sf)
        if sf and os.path.exists(sf):
            sdf = _load_station_df(sf)
            if not sdf.empty and {"lat", "lon"}.issubset(sdf.columns):
                sta_lon  = sdf["lon"].round(4).tolist()
                sta_lat  = sdf["lat"].round(4).tolist()
                sta_name = sdf.get("station", pd.Series([""] * len(sdf))).astype(str).tolist()
    except Exception:
        pass

    return jsonify({
        "n_obs"     : int(len(df)),
        "n_p"       : int(len(df_p)),
        "n_s"       : int(len(df_s)),
        "n_events"  : int(n_ev),
        "n_pairs"   : n_pairs,
        "n_sta"     : int(df["STA"].nunique()),
        "stations"  : sorted(df["STA"].dropna().unique().tolist()),
        "dist_min"  : float(df["DIST_KM"].min()),
        "dist_max"  : float(df["DIST_KM"].max()),
        "all"       : _stats(df["RES"]),
        "p"         : _stats(df_p["RES"]),
        "s"         : _stats(df_s["RES"]),
        "hist_all"  : _hist(df["RES"]),
        "box_station": box,
        "lons"      : lons,
        "lats"      : lats,
        "map_depths": map_depths,
        "map_mags"  : map_mags,
        "sta_lon"   : sta_lon,
        "sta_lat"   : sta_lat,
        "sta_name"  : sta_name,
    })


@app.route("/api/pipeline/jobs/<job_id>/crosscorr_report", methods=["GET"])
def api_crosscorr_report(job_id):
    """Cross-correlation statistics from dt.cc (FDTCC/hypoDD crosscorr or GrowClust).

    Return: summary chip, CC histogram, per-station CC, CC-vs-dist scatter, top pairs.
    """
    from seiswork.web._crosscorr import crosscorr_report
    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        return jsonify({"ok": False, "error": "Job not found"}), 404
    try:
        report = crosscorr_report(str(job_dir))
        return jsonify(report)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"CC analysis failed: {exc}"}), 500


@app.route("/api/pipeline/jobs/<job_id>/crosscorr_waveforms", methods=["GET"])
def api_crosscorr_waveforms(job_id):
    """Read SAC waveform pair for one (ev1, ev2, sta, phase).

    Query params: ev1=int, ev2=int, sta=str, phase=P|S
    Falls back to the SDS archive from the job config if waveforms/ is absent.
    """
    from seiswork.web._crosscorr import crosscorr_waveforms, list_stations_for_pair
    job_dir = PIPE_DIR / job_id
    if not job_dir.exists():
        return jsonify({"ok": False, "error": "Job not found"}), 404

    ev1   = request.args.get("ev1", type=int)
    ev2   = request.args.get("ev2", type=int)
    sta   = request.args.get("sta", "")
    phase = request.args.get("phase", "P").upper()

    # Resolve SDS root from the config used by this job
    sds_root = ""
    try:
        import json as _json
        st_file = job_dir / "status.json"
        if st_file.exists():
            cfg_id = _json.loads(st_file.read_text()).get("cfg_id", "")
            sds_root = _resolve_sds_root(cfg_id)
    except Exception:
        pass

    if ev1 is None or ev2 is None:
        # return list of available station+phase pairs for this event pair
        pairs_q = request.args.get("list_sta", "")
        if pairs_q:
            ev1_l = request.args.get("ev1", type=int, default=0)
            ev2_l = request.args.get("ev2", type=int, default=0)
            from seiswork.web._crosscorr import list_stations_for_pair as _lsp
            return jsonify({"ok": True, "stations": _lsp(str(job_dir), ev1_l, ev2_l)})
        return jsonify({"ok": False, "error": "Parameters ev1 and ev2 are required"}), 400

    if not sta:
        from seiswork.web._crosscorr import list_stations_for_pair as _lsp
        stas = _lsp(str(job_dir), ev1, ev2)
        return jsonify({"ok": True, "stations": stas})

    try:
        data = crosscorr_waveforms(str(job_dir), ev1, ev2, sta, phase,
                                   sds_root=sds_root or None)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/pipeline/import-picks", methods=["POST"])
def import_external_picks():
    """Register an external picks.csv (e.g. PhaseNet / EQT output) as a completed picking job."""
    import shutil, uuid, pandas as pd
    data  = request.get_json(force=True, silent=True) or {}
    src   = data.get("path", "").strip()
    label = data.get("label", "external").strip() or "external"
    if not src:
        return jsonify({"error": "path is required"}), 400
    src_path = Path(src)
    if not src_path.exists():
        return jsonify({"error": f"File not found: {src}"}), 404
    # Validate required columns
    REQUIRED = {"network", "station", "phase_hint", "phase_time"}
    try:
        df = pd.read_csv(src_path, nrows=5)
        missing = REQUIRED - set(df.columns)
        if missing:
            return jsonify({"error": f"Missing columns: {', '.join(sorted(missing))}"}), 400
        total_rows = sum(1 for _ in open(src_path)) - 1
    except Exception as exc:
        return jsonify({"error": f"Failed to read the CSV: {exc}"}), 400
    # Create job directory in PICK_DIR
    job_id  = f"ext_{uuid.uuid4().hex[:8]}"
    job_dir = PICK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / "picks.csv"
    try:
        shutil.copy2(src_path, dest)
    except Exception as exc:
        return jsonify({"error": f"Failed to copy the file: {exc}"}), 500
    # Count P/S picks
    try:
        df_all = pd.read_csv(dest)
        n_p = int((df_all["phase_hint"].str.upper() == "P").sum())
        n_s = int((df_all["phase_hint"].str.upper() == "S").sum())
    except Exception:
        n_p = n_s = 0
    cfg_id = data.get("cfg_id", "").strip()
    status = {
        "id"      : job_id,   # frontend & all other endpoints use "id"
        "job_id"  : job_id,
        "cfg_id"  : cfg_id,
        "state"   : "done",
        "method"  : "external",
        "label"   : label,
        "src_path": str(src_path),
        "started" : datetime.now().isoformat(),
        "finished": datetime.now().isoformat(),
        "picks"   : {"total": total_rows, "P": n_p, "S": n_s},
    }
    (job_dir / "status.json").write_text(json.dumps(status, indent=2))
    return jsonify({"job_id": job_id, "picks": status["picks"], "label": label})


@app.route("/api/pipeline/import-catalog", methods=["POST"])
def import_external_catalog():
    """Register an external catalog.csv (e.g. quakelink2catalog.py output, or
    any already-located catalog) as a completed assoc-step pipeline job, so it
    becomes selectable input for the Locate / Velocity / Relocation dropdowns
    (see prev_steps mapping in list_catalog_files_for_pipeline).

    A companion picks file (picks_associated.csv / picks_gamma.csv /
    picks_real.csv) is auto-detected next to the source catalog, or given
    explicitly via "picks_path", and copied alongside so VelestVelocity /
    HypoDDRelocation find it the same way they do for GaMMA/REAL output.
    """
    import shutil, uuid, pandas as pd
    data  = request.get_json(force=True, silent=True) or {}
    src   = data.get("path", "").strip()
    label = data.get("label", "external").strip() or "external"
    if not src:
        return jsonify({"error": "path is required"}), 400
    src_path = Path(src)
    if not src_path.exists():
        return jsonify({"error": f"File not found: {src}"}), 404
    REQUIRED = {"event_id", "datetime", "lat", "lon"}
    try:
        df = pd.read_csv(src_path, nrows=5)
        missing = REQUIRED - set(df.columns)
        if missing:
            return jsonify({"error": f"Missing columns: {', '.join(sorted(missing))}"}), 400
        n_events = sum(1 for _ in open(src_path)) - 1
    except Exception as exc:
        return jsonify({"error": f"Failed to read the CSV: {exc}"}), 400

    job_id  = f"ext_{uuid.uuid4().hex[:8]}"
    job_dir = PIPE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / "catalog.csv"
    try:
        shutil.copy2(src_path, dest)
    except Exception as exc:
        return jsonify({"error": f"Failed to copy the file: {exc}"}), 500

    picks_src = (data.get("picks_path") or "").strip()
    if not picks_src:
        for nm in ("picks_associated.csv", "picks_gamma.csv", "picks_real.csv"):
            cand = src_path.parent / nm
            if cand.exists():
                picks_src = str(cand)
                break
    picks_imported = False
    if picks_src and Path(picks_src).exists():
        try:
            shutil.copy2(picks_src, job_dir / "picks_associated.csv")
            picks_imported = True
        except Exception:
            pass

    cfg_id = data.get("cfg_id", "").strip()
    status = {
        "id"         : job_id,
        "job_id"     : job_id,
        "cfg_id"     : cfg_id,
        "state"      : "done",
        "step"       : "assoc",
        "method"     : "external",
        "label"      : label,
        "src_path"   : str(src_path),
        "result_file": str(dest),
        "events"     : n_events,
        "started"    : datetime.now().isoformat(),
        "finished"   : datetime.now().isoformat(),
    }
    (job_dir / "status.json").write_text(json.dumps(status, indent=2))
    return jsonify({"job_id": job_id, "events": n_events, "label": label,
                     "picks_imported": picks_imported})


@app.route("/api/pipeline/picks-files", methods=["GET"])
def list_pick_files_for_pipeline():
    """Return list of picks.csv from completed picking jobs, for input to pipeline steps."""
    filter_cfg = request.args.get("cfg_id", "").strip()
    if not filter_cfg:
        return jsonify({"error": "cfg_id required"}), 400
    result = []
    if PICK_DIR.exists():
        for d in sorted(PICK_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            p = d / "picks.csv"
            sf = d / "status.json"
            if p.exists() and sf.exists():
                try:
                    s = json.loads(sf.read_text())
                    if s.get("state") != "done":
                        continue
                    if filter_cfg and s.get("cfg_id") != filter_cfg:
                        continue
                    result.append({
                        "job_id": d.name,
                        "path"  : str(p),
                        "method": s.get("method", ""),
                        "picks" : s.get("picks", {}),
                        "ts"    : s.get("finished", ""),
                    })
                except Exception:
                    pass
    return jsonify(result)


@app.route("/api/pipeline/catalog-files", methods=["GET"])
def list_catalog_files_for_pipeline():
    """Return list of result CSVs from completed pipeline jobs, for chained input.
    Optional ?step= and ?method= filters for source step/method."""
    step          = request.args.get("step",   "")
    filter_method = request.args.get("method", "").strip()
    filter_cfg    = request.args.get("cfg_id", "").strip()
    if not filter_cfg:
        return jsonify({"error": "cfg_id required"}), 400
    # Magnitude is now merged into assoc/locate (mag column), not a separate step.
    prev_steps = {
        "locate"    : ["assoc"],
        "velocity"  : ["locate", "assoc"],
        "relocation": ["locate", "assoc", "velocity"],
        "detect"    : ["relocation"],
        "mechanism" : ["assoc", "locate"],
    }
    allowed = prev_steps.get(step, []) if step else None
    result = []
    if PIPE_DIR.exists():
        for d in sorted(PIPE_DIR.iterdir(),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            sf = d / "status.json"
            if sf.exists():
                try:
                    s = json.loads(sf.read_text())
                    if s.get("state") != "done":
                        continue
                    if filter_cfg and s.get("cfg_id") != filter_cfg:
                        continue
                    if allowed is not None and s.get("step") not in allowed:
                        continue
                    if filter_method and s.get("method") != filter_method:
                        continue
                    rf = s.get("result_file", "")
                    if rf and Path(rf).exists():
                        result.append({
                            "job_id"  : d.name,
                            "path"    : rf,
                            "step"    : s.get("step", ""),
                            "method"  : s.get("method", ""),
                            "events"  : s.get("events", 0),
                            "ts"      : s.get("finished", ""),
                            "filtered": bool(s.get("filtered")),
                            "label"   : s.get("label", ""),
                        })
                except Exception:
                    pass
    return jsonify(result)


@app.route("/api/pipeline/filter-catalog", methods=["POST"])
def filter_catalog_session():
    """Filter an association/location catalog (adapted from simulflow filter_pha) and
    save it as a new session, identical to other catalogs but flagged `filtered`
    + label `_filter` so it can be consumed by all downstream functions.

    Body: {cfg_id, job_id, criteria{min_mag,max_mag,max_rms,min_phase,max_gap,
           min_lat,max_lat,min_lon,max_lon,min_depth,max_depth,start_time,end_time}}
    """
    data    = request.get_json(force=True)
    cfg_id  = (data.get("cfg_id") or "").strip()
    src_job = (data.get("job_id") or "").strip()
    crit    = data.get("criteria", {}) or {}
    if not src_job:
        return jsonify({"error": "job_id required"}), 400
    if not cfg_id:
        return jsonify({"error": "cfg_id required"}), 400

    src_dir = PIPE_DIR / src_job
    sf = src_dir / "status.json"
    if not sf.exists():
        return jsonify({"error": f"Job {src_job} not found"}), 404
    s = json.loads(sf.read_text())
    # Ownership check: job_id must belong to the cfg_id the caller claims,
    # otherwise a stale/forged job_id could pull another project's catalog.
    if s.get("cfg_id", "") != cfg_id:
        return jsonify({"error": f"Job {src_job} does not belong to cfg_id {cfg_id}"}), 403
    step   = s.get("step", "assoc")
    method = s.get("method", "")

    # Source catalog + picks
    src_cat = s.get("result_file", "")
    if not (src_cat and Path(src_cat).exists()):
        for nm in ("catalog_associated.csv", "catalog_gamma.csv", "catalog_real.csv",
                   "catalog_nlloc.csv", "catalog_locsat.csv", "catalog_located.csv"):
            p = src_dir / nm
            if p.exists():
                src_cat = str(p); break
    if not (src_cat and Path(src_cat).exists()):
        return jsonify({"error": "Source catalog not found"}), 404
    src_picks = None
    for nm in ("picks_associated.csv", "picks_gamma.csv", "picks_real.csv"):
        p = src_dir / nm
        if p.exists():
            src_picks = str(p); break

    # Station coordinates (for gap calculation when gap column is empty)
    # + SDS waveform path (for the ghost-event cross-correlation QC, min_cc)
    sta_coords = {}
    sds_path = ""
    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            for st_ in meta.get("stations", []):
                try:
                    sta_coords[str(st_["station"])] = (float(st_["lat"]), float(st_["lon"]))
                except (KeyError, ValueError, TypeError):
                    pass
            wv_cfg = meta.get("waveform", {})
            if wv_cfg.get("path_type", "sds") == "sds":
                sds_path = wv_cfg.get("path") or wv_cfg.get("fdsn", {}).get("output_path", "")
        except Exception:
            pass

    # New session
    new_job = str(uuid.uuid4())[:8]
    job_dir = PIPE_DIR / new_job
    job_dir.mkdir(parents=True, exist_ok=True)
    out_cat   = job_dir / f"catalog_{method or 'associated'}_filter.csv"
    out_picks = job_dir / "picks_associated.csv"

    def _f(k):
        v = crit.get(k)
        if v in (None, "", "null"):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return v   # time value (string)

    if _f("min_cc") is not None and not sds_path:
        return jsonify({"error": "Min CC requires an SDS waveform archive, but this "
                                  "config has none configured (path_type must be 'sds')."}), 400

    try:
        from seiswork.utils.catalog_filter import filter_catalog
        stats = filter_catalog(
            src_cat, str(out_cat), picks_csv=src_picks, out_picks=str(out_picks),
            sta_coords=sta_coords or None,
            min_lat=_f("min_lat"), max_lat=_f("max_lat"),
            min_lon=_f("min_lon"), max_lon=_f("max_lon"),
            min_depth=_f("min_depth"), max_depth=_f("max_depth"),
            min_mag=_f("min_mag"), max_mag=_f("max_mag"),
            max_rms=_f("max_rms"),
            min_phase=int(_f("min_phase")) if _f("min_phase") is not None else None,
            max_gap=_f("max_gap"),
            start_time=crit.get("start_time") or None,
            end_time=crit.get("end_time") or None,
            min_cc=_f("min_cc"), sds_path=sds_path or None,
        )
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": f"Filter failed: {exc}"}), 500

    # HypoDD phase for filtered result (best-effort)
    try:
        import pandas as pd
        from seiswork.utils.converter import catalog_picks_to_hypodd_phase
        cat_df = pd.read_csv(out_cat)
        pick_df = pd.read_csv(out_picks) if out_picks.exists() else pd.DataFrame()
        if len(cat_df) and len(pick_df):
            catalog_picks_to_hypodd_phase(cat_df, pick_df, str(job_dir / "hypodd_phase.pha"))
    except Exception as e:
        print(f"[filter] HypoDD phase export skip: {e}", flush=True)

    now = datetime.now().isoformat(timespec="seconds")
    status = {
        "id": new_job, "step": step, "method": method, "mode": method,
        "cfg_id": cfg_id, "state": "done",
        "filtered": True, "label": "_filter", "source_job": src_job,
        "filter_criteria": {k: v for k, v in crit.items() if v not in (None, "")},
        "events": stats["passed"], "result_file": str(out_cat),
        "started": now, "finished": now,
    }
    (job_dir / "status.json").write_text(json.dumps(status, indent=2))
    with open(job_dir / "output.log", "w") as lf:
        lf.write(f"[filter] Source: {src_job} ({method}, {step})\n"
                 f"[filter] Criteria: {status['filter_criteria']}\n"
                 f"[filter] {stats['passed']} / {stats['total']} events passed filter.\n")
        if stats.get("dropped_by_cc"):
            lf.write(f"[filter] {stats['dropped_by_cc']} event(s) dropped by min_cc "
                     f"(inter-station cross-correlation ghost-event QC).\n")
        lf.write(f"[filter] Catalog: {out_cat}\n")

    return jsonify({"job_id": new_job, "step": step, "method": method,
                    "total": stats["total"], "passed": stats["passed"],
                    "result_file": str(out_cat)})


# ── Result Viewer API ──────────────────────────────────────────────────────────

_RESULT_CAT_CACHE = {}   # cfg_id -> {"sig": <signature>, "payload": <dict>}


@app.route("/api/result/<cfg_id>/catalog", methods=["GET"])
def result_catalog(cfg_id):
    """Aggregate all completed pipeline job results for this config.

    Performance: results are cached per cfg_id and only rebuilt when a new job
    appears or status.json changes (signature = dir name + status mtime). Each
    CSV is serialized vectorized (df.where -> to_dict), not iterrows, which is
    O(n) and very slow for large catalogs (e.g. 21k events).
    """
    import pandas as pd
    import numpy as np

    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    stations = []
    region = {}
    if meta_file.exists():
        try:
            meta = json.loads(meta_file.read_text())
            stations = meta.get("stations", [])
            region = meta.get("region", {})
        except Exception:
            pass

    # Collect completed jobs for this cfg + signature for cache invalidation
    done = []   # (dir, status_dict)
    for d in sorted(PIPE_DIR.iterdir()):
        sf = d / "status.json"
        if not sf.exists():
            continue
        try:
            s = json.loads(sf.read_text())
        except Exception:
            continue
        if s.get("cfg_id") != cfg_id or s.get("state") != "done":
            continue
        rf = s.get("result_file", "")
        if not rf or not Path(rf).exists():
            continue
        done.append((d, s))

    sig = tuple((d.name, (d / "status.json").stat().st_mtime) for d, _ in done)
    cached = _RESULT_CAT_CACHE.get(cfg_id)
    if cached and cached["sig"] == sig:
        # stations/region can change without a new job: cheap refresh
        p = dict(cached["payload"])
        p["stations"], p["region"] = stations, region
        return jsonify(p)

    jobs = []
    for d, s in done:
        rf = s.get("result_file", "")
        try:
            df = pd.read_csv(rf)
            # NaN/inf -> None via vectorized replace (much faster than iterrows)
            df = df.replace([np.inf, -np.inf], np.nan).astype(object)
            df = df.where(pd.notnull(df), None)
            events = df.to_dict("records")
            # Add nearest_city to each event (using lat/lon from the CSV)
            try:
                from seiswork.web._realtime_pipeline import nearest_city_info
                for ev in events:
                    if ev.get("lat") is not None and ev.get("lon") is not None:
                        ev["nearest_city"] = nearest_city_info(
                            float(ev["lat"]), float(ev["lon"]))
            except Exception:
                pass
        except Exception:
            continue

        jobs.append({
            "job_id"     : d.name,
            "step"       : s.get("step", ""),
            "method"     : s.get("method", ""),
            "mode"       : s.get("mode", "") or s.get("method", ""),
            "started"    : s.get("started", ""),
            "finished"   : s.get("finished", ""),
            "n_events"   : len(events),
            "events"     : events,
            "filtered"   : bool(s.get("filtered")),
            "result_file": s.get("result_file", ""),
        })

    # Fallback: add results from locally synced jobs not already included
    sync_dir = _offline_sync_dir()
    if sync_dir:
        existing_ids = {j["job_id"] for j in jobs}
        for d in sorted(sync_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if d.name in existing_ids or not d.is_dir():
                continue
            sf = d / "status.json"
            if not sf.exists():
                continue
            try:
                s = json.loads(sf.read_text())
                if s.get("cfg_id") != cfg_id or s.get("state") != "done":
                    continue
                # Pick the most informative catalog CSV available
                csv_candidates = sorted(d.glob("catalog_*.csv"),
                                        key=lambda f: f.stat().st_size, reverse=True)
                if not csv_candidates:
                    csv_candidates = [f for f in d.glob("*.csv")
                                      if "pick" not in f.name]
                if not csv_candidates:
                    continue
                df = pd.read_csv(str(csv_candidates[0]))
                df = df.replace([np.inf, -np.inf], np.nan).astype(object)
                df = df.where(pd.notnull(df), None)
                events = df.to_dict("records")
                try:
                    from seiswork.web._realtime_pipeline import nearest_city_info
                    for ev in events:
                        if ev.get("lat") is not None and ev.get("lon") is not None:
                            ev["nearest_city"] = nearest_city_info(
                                float(ev["lat"]), float(ev["lon"]))
                except Exception:
                    pass
                jobs.append({
                    "job_id"     : d.name,
                    "step"       : s.get("step", ""),
                    "method"     : s.get("method", ""),
                    "mode"       : s.get("mode", "") or s.get("method", ""),
                    "started"    : s.get("started", ""),
                    "finished"   : s.get("finished", ""),
                    "n_events"   : len(events),
                    "events"     : events,
                    "filtered"   : bool(s.get("filtered")),
                    "result_file": s.get("result_file", ""),
                })
            except Exception:
                continue

    payload = {"jobs": jobs, "stations": stations, "region": region}
    _RESULT_CAT_CACHE[cfg_id] = {"sig": sig, "payload": payload}
    return jsonify(payload)


@app.route("/api/result/<cfg_id>/residual", methods=["GET"])
def result_residual(cfg_id):
    """HypoDD residual diagnostics for the latest relocation job of this config.

    Reproduces residual_hypodd.ipynb: residual stats table (per phase),
    histograms, residual-vs-distance, per-station box stats, RMS convergence,
    automatic diagnosis, and the ph2dt/hypoDD.inp parameters that produced it.
    Optional ?job_id=<id> selects a specific relocation job.
    """
    from seiswork.web._residual import compute_residual_report

    want_job = request.args.get("job_id", "").strip()

    # Collect candidate relocation jobs (this cfg) that have a hypoDD.res.
    candidates = []
    _seen_ids: set = set()
    for _base in (PIPE_DIR, _offline_sync_dir()):
        if not _base or not _base.exists():
            continue
        for d in _base.iterdir():
            if d.name in _seen_ids or not d.is_dir():
                continue
            sf = d / "status.json"
            if not sf.exists() or not (d / "hypoDD.res").exists():
                continue
            try:
                s = json.loads(sf.read_text())
            except Exception:
                continue
            if s.get("cfg_id") != cfg_id or s.get("step") != "relocation":
                continue
            _seen_ids.add(d.name)
            candidates.append((d, s))

    if not candidates:
        return jsonify({"ok": False,
                        "error": "No relocation job (hypoDD.res) found for this project."}), 404

    if want_job:
        chosen = next((c for c in candidates if c[0].name == want_job), None)
        if chosen is None:
            return jsonify({"ok": False, "error": f"Job {want_job} has no hypoDD.res."}), 404
    else:
        chosen = max(candidates, key=lambda c: c[1].get("finished", "") or c[1].get("started", ""))

    job_dir, status = chosen
    try:
        report = compute_residual_report(str(job_dir))
    except FileNotFoundError:
        return jsonify({"ok": False, "error": "hypoDD.res not found in job dir."}), 404
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Failed to parse the residual: {exc}"}), 500

    report["job"] = {
        "job_id"  : job_dir.name,
        "mode"    : status.get("mode", "") or status.get("method", ""),
        "finished": status.get("finished", ""),
        "started" : status.get("started", ""),
    }
    report["jobs_available"] = sorted(
        ({"job_id": d.name,
          "mode": s.get("mode", "") or s.get("method", ""),
          "finished": s.get("finished", "")} for d, s in candidates),
        key=lambda j: j["finished"], reverse=True)
    return jsonify(report)


# ── Cached per-event picks index for the Result-viewer waveform/spectrogram ────
# The associated picks CSV for a full-year run is ~3.5M rows (~8s just to parse).
# Re-reading it on every waveform/spectrogram click produced ~14s latency that
# looked like the panel was "empty"/hung. We parse the file once per (file, mtime),
# keep only associated picks (event_index/event_id present, not -1) into a small
# index {event_id: {sta: {"net":, "phases": {PHASE: datetime}}}}, then serve clicks O(1).
_RESULT_PICKS_CACHE: dict = {}
_RESULT_PICKS_LOCK = threading.Lock()

# REAL phase picks indexed by origin time (per config). Relocation/velocity jobs
# (velest, hypodd, etc.) carry no picks file of their own, only catalogs whose
# event_ids differ from the REAL ids, so their waveform overlay would fall back
# to bare stations. This lets such catalogs borrow the original REAL phase picks
# (real.pha -> picks_real.csv) matched by nearest origin time.
_REAL_TIMEIDX_CACHE: dict = {}
_REAL_TIMEIDX_LOCK = threading.Lock()

# hypodd_phase.pha picks indexed by event number (string).  Keyed by pha file path.
_PHA_PICKS_CACHE: dict = {}
_PHA_PICKS_LOCK = threading.Lock()

# VELEST final.CNV picks indexed by sequential event index (string, 0-based).
_CNV_PICKS_CACHE: dict = {}
_CNV_PICKS_LOCK = threading.Lock()


def _result_picks_file(job_dir, method):
    for name in (f"picks_{method}.csv", "picks_associated.csv"):
        p = job_dir / name
        if p.exists():
            return p
    return None


def _picks_idx_add(index, eid, sta, net, phase, ts):
    from datetime import datetime as _dt
    sta = (sta or "").strip()
    if not sta or not eid or eid == "-1":
        return
    try:
        pt = _dt.fromisoformat(ts.split("+")[0].replace("Z", ""))
    except Exception:
        return
    rec = index.setdefault(eid, {}).setdefault(sta, {"net": (net or "7G").strip(), "phases": {}})
    rec["phases"][(phase or "").strip().upper()] = pt


def _result_picks_index(job_dir, method):
    """{event_id_str: {sta: {'net':, 'phases': {PHASE: datetime}}}}, cached per mtime."""
    import pandas as pd
    pf = _result_picks_file(job_dir, method)
    if pf is None:
        return {}
    key = str(pf)
    try:
        mt = pf.stat().st_mtime
    except OSError:
        return {}
    with _RESULT_PICKS_LOCK:
        hit = _RESULT_PICKS_CACHE.get(key)
        if hit and hit[0] == mt:
            return hit[1]
    index: dict = {}
    try:
        cols = set(pd.read_csv(pf, nrows=0).columns)
        if "event_index" in cols:           # GaMMA / associated: event_index, type, timestamp
            uc = [c for c in ("network", "station", "type", "timestamp", "event_index") if c in cols]
            df = pd.read_csv(pf, usecols=uc)
            df = df[df["event_index"].astype(str) != "-1"]
            for r in df.itertuples(index=False):
                _picks_idx_add(index, str(r.event_index), str(getattr(r, "station", "")),
                               str(getattr(r, "network", "7G")), str(getattr(r, "type", "")),
                               str(getattr(r, "timestamp", "")))
        elif "event_id" in cols:            # REAL: event_id, phase|phase_hint, pick_time
            ph_col = "phase" if "phase" in cols else ("phase_hint" if "phase_hint" in cols else None)
            tcol = "pick_time" if "pick_time" in cols else ("timestamp" if "timestamp" in cols else None)
            uc = [c for c in ("network", "station", ph_col, tcol, "event_id") if c]
            df = pd.read_csv(pf, usecols=uc)
            for r in df.itertuples(index=False):
                _picks_idx_add(index, str(r.event_id), str(getattr(r, "station", "")),
                               str(getattr(r, "network", "7G")),
                               str(getattr(r, ph_col, "P")) if ph_col else "P",
                               str(getattr(r, tcol, "")) if tcol else "")
    except Exception:
        return {}
    with _RESULT_PICKS_LOCK:
        _RESULT_PICKS_CACHE[key] = (mt, index)
    return index


def _real_job_dir_for_cfg(cfg_id):
    """Most recent REAL-associated job dir for a config: the one whose picks come
    from real.pha (picks_real.csv). Used so relocation/velocity catalogs can borrow
    its phase picks for the waveform overlay."""
    if not PIPE_DIR.exists():
        return None
    best = None
    for d in PIPE_DIR.iterdir():
        sf = d / "status.json"
        if not sf.exists():
            continue
        try:
            s = json.loads(sf.read_text())
        except Exception:
            continue
        if s.get("cfg_id") != cfg_id:
            continue
        if s.get("method") == "real" or s.get("mode") == "real" or (d / "picks_real.csv").exists():
            fin = s.get("finished", "")
            if best is None or fin >= best[0]:
                best = (fin, d)
    return best[1] if best else None


def _real_picks_timeindex(cfg_id):
    """[(origin_dt, lat, lon, {sta: {'net':, 'phases': {PHASE: pick_dt}}}), …] sorted
    by origin time, built from the REAL job's picks_real.csv + catalog_real.csv.
    Cached per (picks+catalog) mtime."""
    jd = _real_job_dir_for_cfg(cfg_id)
    if jd is None:
        return []
    pf = _result_picks_file(jd, "real")
    cf = jd / "catalog_real.csv"
    if not cf.exists():
        cf = jd / "catalog_associated.csv"
    if pf is None or not cf.exists():
        return []
    key = f"{pf}|{cf}"
    try:
        mt = pf.stat().st_mtime + cf.stat().st_mtime
    except OSError:
        return []
    with _REAL_TIMEIDX_LOCK:
        hit = _REAL_TIMEIDX_CACHE.get(key)
        if hit and hit[0] == mt:
            return hit[1]
    import pandas as pd
    from datetime import datetime as _dt
    pidx = _result_picks_index(jd, "real")        # {event_id: {sta: {net, phases}}}
    try:
        cat = pd.read_csv(cf, usecols=["event_id", "datetime", "lat", "lon"])
    except Exception:
        return []
    out = []
    for r in cat.itertuples(index=False):
        picks = pidx.get(str(r.event_id))
        if not picks:
            continue
        try:
            odt = _dt.fromisoformat(str(r.datetime).split("+")[0].replace("Z", ""))
        except Exception:
            continue
        try:
            la, lo = float(r.lat), float(r.lon)
        except Exception:
            la = lo = None
        out.append((odt, la, lo, picks))
    out.sort(key=lambda x: x[0])
    with _REAL_TIMEIDX_LOCK:
        _REAL_TIMEIDX_CACHE[key] = (mt, out)
    return out


def _real_picks_for_origin(cfg_id, ot, ev_lat=None, ev_lon=None, tol_s=120.0):
    """Borrow REAL picks for the REAL event nearest `ot` in time. Velest/hypodd
    recompute origin times (offsets of tens of seconds vs REAL), so the tolerance is
    wide; an optional spatial guard (≤0.5° from the clicked epicenter) rejects a rare
    mismatch in a clustered sequence. Returns (real_origin_dt, picks) or (None, {}).
    Picks = {sta: {'net':, 'phases': {PHASE: pick_dt}}}; the caller should re-center
    the waveform window on real_origin_dt so the (absolute) picks stay in frame."""
    import bisect
    idx = _real_picks_timeindex(cfg_id)
    if not idx:
        return None, {}
    times = [t[0] for t in idx]
    i = bisect.bisect_left(times, ot)
    best, bestd = None, tol_s
    for j in (i - 1, i, i + 1):
        if 0 <= j < len(idx):
            odt, la, lo, picks = idx[j]
            d = abs((odt - ot).total_seconds())
            if d > bestd:
                continue
            if (ev_lat is not None and ev_lon is not None and la is not None
                    and abs(la - ev_lat) + abs(lo - ev_lon) > 0.5):
                continue                 # spatial guard: not the same event
            bestd, best = d, (odt, picks)
    return best if best else (None, {})


def _cfg_stations_nearest(meta, ev, n=20):
    """Config stations sorted nearest-first to the event epicenter; fallback when no
    picks match (e.g. a manually-injected relocation catalog whose ids/origin-times
    don't line up with the picks file). Returns [(station, network), ...]."""
    stas = meta.get("stations", []) or []
    try:
        elat, elon = float(ev.get("lat")), float(ev.get("lon"))
    except Exception:
        elat = elon = None
    if elat is not None and elon is not None:
        def d2(st):
            try:
                return (float(st.get("lat", 0)) - elat) ** 2 + (float(st.get("lon", 0)) - elon) ** 2
            except Exception:
                return 1e9
        stas = sorted(stas, key=d2)
    out = []
    for st in stas[:n]:
        sid = str(st.get("station", "")).strip()
        if sid:
            out.append((sid, str(st.get("network", "7G")).strip()))
    return out


def _pha_picks_index(pha_path):
    """Parse hypodd_phase.pha (HYPOINVERSE format) -> {event_num_str: (origin_dt, picks)}
    where picks = {sta: {'net': '7G', 'phases': {P|S: abs_datetime}}}.
    Travel times in the file are relative to the original (pre-relocation) origin.
    Cached per (path, mtime)."""
    from datetime import datetime as _dt, timedelta as _td
    pf = Path(pha_path)
    if not pf.exists():
        return {}
    try:
        mt = pf.stat().st_mtime
    except OSError:
        return {}
    key = str(pf)
    with _PHA_PICKS_LOCK:
        hit = _PHA_PICKS_CACHE.get(key)
        if hit and hit[0] == mt:
            return hit[1]

    out = {}
    current_eid = None
    current_ot = None
    current_picks: dict = {}

    with open(pf, errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if line.startswith("#"):
                if current_eid is not None and current_picks:
                    out[current_eid] = (current_ot, current_picks)
                current_eid = None
                parts = line[1:].split()
                if len(parts) < 6:
                    continue
                try:
                    yr, mo, dd = int(parts[0]), int(parts[1]), int(parts[2])
                    hh, mn = int(parts[3]), int(parts[4])
                    ss = float(parts[5])
                    si, us = int(ss), round((ss % 1) * 1_000_000)
                    if us >= 1_000_000:
                        si += 1; us -= 1_000_000
                    ot = _dt(yr, mo, dd, hh, mn, si, us)
                    current_eid = str(int(parts[-1]))
                    current_ot = ot
                    current_picks = {}
                except Exception:
                    pass
            else:
                if current_eid is None:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                sta = parts[0]
                try:
                    tt = float(parts[1])
                    ph = parts[3].upper()
                except Exception:
                    continue
                if ph not in ("P", "S"):
                    continue
                abs_dt = current_ot + _td(seconds=tt)
                if sta not in current_picks:
                    current_picks[sta] = {"net": "7G", "phases": {}}
                current_picks[sta]["phases"][ph] = abs_dt

    if current_eid is not None and current_picks:
        out[current_eid] = (current_ot, current_picks)

    with _PHA_PICKS_LOCK:
        _PHA_PICKS_CACHE[key] = (mt, out)
    return out


def _cnv_picks_index(cnv_path):
    """Parse VELEST final.CNV -> {idx_str: (origin_dt, picks)} where idx_str is the
    0-based sequential event index as a string (matches int('velest_NNNNNN')).
    picks = {sta: {'net': '7G', 'phases': {P|S: abs_datetime}}}.

    CNV fixed-column header: yr[0:2] mon[2:4] day[4:6] space hr[7:9] min[9:11]
    space sec[12:17]; remaining fields are space-separated (lat, lon, dep, mag …).
    Pick lines: 14-char records each = sta(4) + blank(2) + phase(1) + wt(1) +
    blank(2) + tt(f4.2); multiple records concatenated per line.
    Events are separated by blank lines.  Cached per (path, mtime)."""
    from datetime import datetime as _dt, timedelta as _td
    pf = Path(cnv_path)
    if not pf.exists():
        return {}
    try:
        mt = pf.stat().st_mtime
    except OSError:
        return {}
    key = str(pf)
    with _CNV_PICKS_LOCK:
        hit = _CNV_PICKS_CACHE.get(key)
        if hit and hit[0] == mt:
            return hit[1]

    out = {}
    ev_idx = -1
    current_ot = None
    current_picks: dict = {}

    def _flush():
        if ev_idx >= 0 and current_picks:
            out[str(ev_idx)] = (current_ot, dict(current_picks))

    with open(pf, errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            # blank line: end of current event
            if not line.strip():
                _flush()
                current_ot = None
                current_picks = {}
                continue
            # Detect header: first char digit and length ≥ 40 (picks start later)
            # Robust heuristic: header has lat/lon 'N'/'S'/'E'/'W' characters
            if ("N" in line or "S" in line) and ("E" in line or "W" in line):
                _flush()
                current_picks = {}
                try:
                    yr  = int(line[0:2])
                    mon = int(line[2:4])
                    day = int(line[4:6])
                    hr  = int(line[7:9])
                    mn  = int(line[9:11])
                    ss  = float(line[12:17])
                    si  = int(ss)
                    us  = round((ss % 1) * 1_000_000)
                    if us >= 1_000_000:
                        si += 1; us -= 1_000_000
                    # Build datetime; handle minute/hour overflow from VELEST
                    base = _dt(2000 + yr, mon, day, 0, 0, 0)
                    current_ot = base + _td(hours=hr, minutes=mn, seconds=si, microseconds=us)
                    ev_idx += 1
                except Exception:
                    current_ot = None
            else:
                # Pick line: 14-char records
                if current_ot is None:
                    continue
                i = 0
                while i + 13 <= len(line):
                    rec = line[i:i+14]
                    sta = rec[0:4].strip()
                    ph  = rec[6:7].strip().upper()
                    try:
                        tt = float(rec[10:14])
                    except Exception:
                        i += 14
                        continue
                    if sta and ph in ("P", "S"):
                        abs_dt = current_ot + _td(seconds=tt)
                        if sta not in current_picks:
                            current_picks[sta] = {"net": "7G", "phases": {}}
                        current_picks[sta]["phases"][ph] = abs_dt
                    i += 14

    _flush()

    with _CNV_PICKS_LOCK:
        _CNV_PICKS_CACHE[key] = (mt, out)
    return out


# Global velocity model (IASP91, same model used by the HypoDD relocation buttons
# elsewhere in the GUI) built once into a TauP model and reused for predicted P/S
# arrival overlays in the Result Viewer waveform plot, a QC reference like the
# eqcorrscan record-section notebook's syn_P/syn_S curves.
_LOCAL_TAUP_MODEL = None
_LOCAL_TAUP_LOCK = threading.Lock()


def _local_taup_model():
    global _LOCAL_TAUP_MODEL
    if _LOCAL_TAUP_MODEL is not None:
        return _LOCAL_TAUP_MODEL
    with _LOCAL_TAUP_LOCK:
        if _LOCAL_TAUP_MODEL is not None:
            return _LOCAL_TAUP_MODEL
        try:
            from obspy.taup import TauPyModel
            _LOCAL_TAUP_MODEL = TauPyModel(model="iasp91")
        except Exception as ex:
            print(f"[result_waveform] TauP model load failed: {ex}")
            _LOCAL_TAUP_MODEL = False
        return _LOCAL_TAUP_MODEL


def _safe_float(v):
    import math as _math
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (_math.isnan(f) or _math.isinf(f)) else f


def _resolve_event_picks(cfg_id, job_id, event_id):
    """Shared event/pick resolution used by all `/api/result/<cfg_id>/waveform*`
    endpoints: reads the job's catalog row + resolves picks_by_sta with the same
    fallback chain (job's own picks, final.CNV, hypodd_phase.pha, REAL nearest
    origin, bare nearest stations).

    Returns either `(None, (error_dict, status_code))` or
    `(ctx, None)` where ctx is a dict with keys: meta, sds_path, job_dir, s, method,
    ev, ot, picks_by_sta, picks_fallback, picks_from_real, picks_from_pha, picks_from_cnv.
    """
    import pandas as pd

    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if not meta_file.exists():
        return None, ({"error": "Config not found"}, 404)
    meta     = json.loads(meta_file.read_text())
    wv_cfg   = meta.get("waveform", {})
    sds_path = wv_cfg.get("path") or wv_cfg.get("fdsn", {}).get("output_path", "")
    if not sds_path or wv_cfg.get("path_type", "sds") != "sds":
        return None, ({"error": "SDS waveform path not configured for this config"}, 400)

    job_dir = PIPE_DIR / job_id
    sf = job_dir / "status.json"
    if not sf.exists():
        return None, ({"error": "Job not found"}, 404)
    s = json.loads(sf.read_text())
    rf = s.get("result_file", "")
    if not rf or not Path(rf).exists():
        return None, ({"error": "Job has no result file"}, 404)

    try:
        df = pd.read_csv(rf)
        ev_row = df[df["event_id"].astype(str) == str(event_id)]
        if ev_row.empty:
            return None, ({"error": f"Event {event_id} not found in job {job_id}"}, 404)
        ev = ev_row.iloc[0]
    except Exception as ex:
        return None, ({"error": f"Catalog read error: {ex}"}, 500)

    ot_str = str(ev.get("datetime", ""))
    try:
        from datetime import datetime as _dt
        ot = _dt.fromisoformat(ot_str.replace("Z", "").split("+")[0])
    except Exception:
        return None, ({"error": f"Cannot parse event time: {ot_str}"}, 400)

    # Build picks_by_sta from the cached per-event picks index (parsed once/mtime).
    method = s.get("method", "")
    picks_by_sta: dict = {}
    for sta, info in _result_picks_index(job_dir, method).get(str(event_id), {}).items():
        picks_by_sta[sta] = {
            "net": info["net"],
            "phases": {ph: round((pt - ot).total_seconds(), 2) for ph, pt in info["phases"].items()},
        }

    # No picks of this job's own (e.g. velest/hypodd catalog), so try:
    # 1) final.CNV exact match by sequential index (velest jobs: velest_NNNNNN -> idx N)
    # 2) hypodd_phase.pha exact match by event_id (sequential number in header)
    # 3) REAL picks matched by nearest origin time (re-center window on REAL origin)
    # Last resort: bare nearest stations (no overlay).
    picks_fallback = False
    picks_from_real = False
    picks_from_pha  = False
    picks_from_cnv  = False
    if not picks_by_sta:
        cnv_file = job_dir / "final.CNV"
        if cnv_file.exists() and str(event_id).startswith("velest_"):
            try:
                cnv_idx_str = str(int(str(event_id).split("_")[1]))
            except Exception:
                cnv_idx_str = None
            if cnv_idx_str is not None:
                cnv_entry = _cnv_picks_index(cnv_file).get(cnv_idx_str)
                if cnv_entry:
                    cnv_ot, cnv_picks = cnv_entry
                    picks_from_cnv = True
                    for sta, info in cnv_picks.items():
                        picks_by_sta[sta] = {
                            "net": info["net"],
                            "phases": {ph: round((pt - ot).total_seconds(), 2)
                                       for ph, pt in info["phases"].items()},
                        }
    if not picks_by_sta:
        pha_file = job_dir / "hypodd_phase.pha"
        if pha_file.exists():
            pha_idx = _pha_picks_index(pha_file)
            pha_entry = pha_idx.get(str(event_id))
            if pha_entry:
                pha_ot, pha_picks = pha_entry
                picks_from_pha = True
                for sta, info in pha_picks.items():
                    picks_by_sta[sta] = {
                        "net": info["net"],
                        "phases": {ph: round((pt - ot).total_seconds(), 2)
                                   for ph, pt in info["phases"].items()},
                    }
    if not picks_by_sta:
        rot, real_picks = _real_picks_for_origin(
            cfg_id, ot, ev_lat=float(ev.get("lat", 0)), ev_lon=float(ev.get("lon", 0)))
        if real_picks:
            picks_from_real = True
            ot = rot                       # re-center window/trace axis on REAL origin
            for sta, info in real_picks.items():
                picks_by_sta[sta] = {
                    "net": info["net"],
                    "phases": {ph: round((pt - rot).total_seconds(), 2) for ph, pt in info["phases"].items()},
                }
    if not picks_by_sta:
        picks_fallback = True
        for sid, net in _cfg_stations_nearest(meta, ev, 20):
            picks_by_sta[sid] = {"net": net, "phases": {}}

    return {
        "meta": meta, "sds_path": sds_path, "job_dir": job_dir, "s": s, "method": method,
        "ev": ev, "ot": ot, "picks_by_sta": picks_by_sta,
        "picks_fallback": picks_fallback, "picks_from_real": picks_from_real,
        "picks_from_pha": picks_from_pha, "picks_from_cnv": picks_from_cnv,
    }, None


@app.route("/api/result/<cfg_id>/waveform", methods=["GET"])
def result_waveform(cfg_id):
    """Fetch SDS waveform for one event from a pipeline job result."""
    from datetime import timedelta
    from obspy import UTCDateTime
    from obspy.clients.filesystem.sds import Client as SDSClient

    import math as _math

    job_id   = request.args.get("job_id", "").strip()
    event_id = request.args.get("event_id", "").strip()
    if not job_id or not event_id:
        return jsonify({"error": "job_id and event_id required"}), 400

    ctx, err = _resolve_event_picks(cfg_id, job_id, event_id)
    if err:
        return jsonify(err[0]), err[1]
    meta, sds_path, method = ctx["meta"], ctx["sds_path"], ctx["method"]
    ev, ot, picks_by_sta = ctx["ev"], ctx["ot"], ctx["picks_by_sta"]
    picks_fallback  = ctx["picks_fallback"]
    picks_from_real = ctx["picks_from_real"]
    picks_from_pha  = ctx["picks_from_pha"]
    picks_from_cnv  = ctx["picks_from_cnv"]

    PRE_SEC  = 10
    POST_SEC = 80
    MAX_STA  = 20
    MAX_PTS  = 3000

    t0 = ot - timedelta(seconds=PRE_SEC)
    t1 = ot + timedelta(seconds=POST_SEC)

    try:
        sds = SDSClient(sds_path)
    except Exception as ex:
        return jsonify({"error": f"SDS init failed: {ex}"}), 500

    def _earliest_pick(item):
        phases = item[1].get("phases", {})
        times = [v for v in phases.values() if v is not None]
        return min(times) if times else float("inf")

    ordered = sorted(picks_by_sta.items(), key=_earliest_pick)[:MAX_STA]

    # Predicted P/S arrivals (global IASP91 model), a QC overlay next to the
    # actual picks, same idea as the eqcorrscan notebook's syn_P/syn_S curves but
    # per-station on this row waveform view instead of a distance record-section.
    elat = _safe_float(ev.get("lat"))
    elon = _safe_float(ev.get("lon"))
    edep_km = _safe_float(ev.get("depth_km"))
    if edep_km is None or edep_km < 0:
        edep_km = 10.0
    sta_coords = {}
    for st_ in meta.get("stations", []) or []:
        sid = str(st_.get("station", "")).strip()
        slat, slon = _safe_float(st_.get("lat")), _safe_float(st_.get("lon"))
        if sid and slat is not None and slon is not None:
            sta_coords[sid] = (slat, slon)
    taup = _local_taup_model() if (elat is not None and elon is not None) else None

    def _predicted_phases(sta):
        if not taup or sta not in sta_coords:
            return None
        try:
            from obspy.geodetics import gps2dist_azimuth
            dist_km = gps2dist_azimuth(elat, elon, *sta_coords[sta])[0] / 1000.0
            arrivals = taup.get_travel_times(
                source_depth_in_km=edep_km, distance_in_degree=dist_km / 111.195,
                phase_list=["P", "p", "S", "s"])
            out = {}
            for name, want in (("P", ("P", "p")), ("S", ("S", "s"))):
                a = next((a for a in arrivals if a.name in want), None)
                if a is not None:
                    out[name] = round(a.time, 2)
            return out or None
        except Exception:
            return None

    traces = []
    for sta, info in ordered:
        net   = info["net"]
        picks = info["phases"]
        syn_picks = _predicted_phases(sta)
        tr_found = None
        st, _cha = read_best_waveform(sds, net, sta, UTCDateTime(t0), UTCDateTime(t1))
        if st:
            sel = st.select(component="Z")
            tr_found = sel[0] if sel else st[0]
        if not tr_found:
            traces.append({"sta": sta, "net": net, "error": "no waveform in SDS"})
            continue
        try:
            tr = tr_found.copy()
            tr.detrend("demean")
            dt_s  = tr.stats.delta
            npts  = tr.stats.npts
            t0rel = (tr.stats.starttime.datetime - ot).total_seconds()
            # Gaps in the SDS archive can leave masked/NaN samples; scrub them to 0
            # so the response stays valid JSON (a literal NaN breaks JSON.parse).
            data  = [0.0 if (isinstance(v, float) and (_math.isnan(v) or _math.isinf(v))) else float(v)
                     for v in tr.data.tolist()]
            times = [round(t0rel + i * dt_s, 3) for i in range(npts)]
            if len(data) > MAX_PTS:
                step  = max(1, len(data) // MAX_PTS)
                data  = data[::step]
                times = times[::step]
            mx = max(abs(v) for v in data) if data else 1.0
            if mx > 0:
                data = [round(v / mx, 5) for v in data]
            traces.append({
                "sta": sta, "net": net, "cha": tr.stats.channel,
                "sr": round(tr.stats.sampling_rate, 1),
                "times": times, "data": data, "picks": picks,
                "syn_picks": syn_picks,
            })
        except Exception as ex:
            traces.append({"sta": sta, "net": net, "error": str(ex)})

    dep = ev.get("depth_km")
    mag = ev.get("mag")
    rms = ev.get("rms")
    return jsonify({
        "event_id"   : event_id,
        "origin_time": ot.strftime("%Y-%m-%d %H:%M:%S"),
        "lat" : _safe_float(ev.get("lat", 0)) or 0.0,
        "lon" : _safe_float(ev.get("lon", 0)) or 0.0,
        "depth": _safe_float(dep),
        "mag"  : _safe_float(mag),
        "rms"  : _safe_float(rms),
        "method": method,
        "n_sta" : int(ev.get("nsta", 0) or 0),
        "picks_fallback": picks_fallback,
        "picks_from_real": picks_from_real,
        "picks_from_pha" : picks_from_pha,
        "picks_from_cnv" : picks_from_cnv,
        "traces": traces,
    })


@app.route("/api/result/<cfg_id>/waveform/corr_stack", methods=["GET"])
def result_waveform_corr_stack(cfg_id):
    """Inter-station cross-correlation matrix + aligned/stacked waveform for one
    event, same QC as notebooks/event_station_stack_xcorr.ipynb and the min_cc
    ghost-event filter (seiswork/utils/catalog_filter.py), rendered below the
    event waveform in the Result Viewer instead of only being used as a filter."""
    from datetime import timedelta
    from obspy.clients.filesystem.sds import Client as SDSClient

    from seiswork.utils.catalog_filter import station_cc_matrix, station_stack

    job_id   = request.args.get("job_id", "").strip()
    event_id = request.args.get("event_id", "").strip()
    phase    = request.args.get("phase", "P").strip() or "P"
    if not job_id or not event_id:
        return jsonify({"error": "job_id and event_id required"}), 400

    ctx, err = _resolve_event_picks(cfg_id, job_id, event_id)
    if err:
        return jsonify(err[0]), err[1]
    sds_path, ot, picks_by_sta = ctx["sds_path"], ctx["ot"], ctx["picks_by_sta"]

    # picks_by_sta phases are seconds relative to `ot`; rebuild absolute pick times
    stations_picks = [
        (sta, info["net"], ot + timedelta(seconds=info["phases"][phase]))
        for sta, info in picks_by_sta.items()
        if phase in info.get("phases", {})
    ]
    if len(stations_picks) < 2:
        return jsonify({"error": f"Fewer than 2 stations with a {phase} pick for this event"}), 200

    try:
        sds = SDSClient(sds_path)
    except Exception as ex:
        return jsonify({"error": f"SDS init failed: {ex}"}), 500

    cc = station_cc_matrix(stations_picks, sds)
    stack = station_stack(stations_picks, sds)
    if cc is None and stack is None:
        return jsonify({"error": "No usable waveform for cross-correlation/stacking"}), 200

    return jsonify({"event_id": event_id, "phase": phase, "cc": cc, "stack": stack})


@app.route("/api/result/<cfg_id>/waveform/spectro", methods=["GET"])
def result_waveform_spectro(cfg_id):
    """Spectrogram for one station/channel around an event origin time."""
    import numpy as np
    import pandas as pd
    from obspy import UTCDateTime
    from obspy.clients.filesystem.sds import Client as SDSClient

    job_id   = request.args.get("job_id",   "").strip()
    event_id = request.args.get("event_id", "").strip()
    net      = request.args.get("net",      "").strip()
    sta      = request.args.get("sta",      "").strip()
    cha      = request.args.get("cha",      "HHZ").strip()
    pre_s    = float(request.args.get("pre",  10))
    post_s   = float(request.args.get("post", 90))

    if not all([job_id, event_id, net, sta]):
        return jsonify({"error": "job_id, event_id, net, sta required"}), 400

    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": "Config not found"}), 404
    meta     = json.loads(meta_file.read_text())
    wv_cfg   = meta.get("waveform", {})
    sds_path = wv_cfg.get("path") or wv_cfg.get("fdsn", {}).get("output_path", "")
    if not sds_path:
        return jsonify({"error": "No SDS path configured"}), 400

    job_dir = PIPE_DIR / job_id
    sf = job_dir / "status.json"
    if not sf.exists():
        return jsonify({"error": "Job not found"}), 404
    s  = json.loads(sf.read_text())
    rf = s.get("result_file", "")
    if not rf or not Path(rf).exists():
        return jsonify({"error": "No result file"}), 404

    try:
        df = pd.read_csv(rf)
        ev_row = df[df["event_id"].astype(str) == str(event_id)]
        if ev_row.empty:
            return jsonify({"error": f"Event {event_id} not found"}), 404
        ev = ev_row.iloc[0]
    except Exception as ex:
        return jsonify({"error": f"Catalog error: {ex}"}), 500

    ot_str = str(ev.get("datetime", ""))
    try:
        from datetime import datetime as _dt
        ot = _dt.fromisoformat(ot_str.replace("Z", "").split("+")[0])
    except Exception:
        return jsonify({"error": f"Cannot parse origin time: {ot_str}"}), 400

    t_start = UTCDateTime(ot) - pre_s
    t_end   = UTCDateTime(ot) + post_s
    try:
        st = SDSClient(sds_path).get_waveforms(net, sta, "*", cha, t_start, t_end)
        if not st:
            return jsonify({"error": f"No data for {net}.{sta}..{cha}"}), 404
        st.merge(method=1, fill_value=0)
        tr   = st[0]
        data = tr.data.astype(np.float64)
        sr   = tr.stats.sampling_rate

        from scipy.signal import spectrogram as _spgram
        nperseg  = min(int(sr * 4), 512)
        noverlap = nperseg * 3 // 4
        f, t, Sxx = _spgram(data, fs=sr, nperseg=nperseg, noverlap=noverlap, scaling="density")
        Sxx_db   = 10 * np.log10(Sxx + 1e-30)

        f_max  = min(50.0, sr / 2)
        f_mask = f <= f_max
        f_out  = f[f_mask]; S_out = Sxx_db[f_mask, :]

        fs = max(1, len(f_out) // 100); ts = max(1, S_out.shape[1] // 400)
        f_out = f_out[::fs]; t_out = t[::ts]; S_out = S_out[::fs, ::ts]

        return jsonify({
            "channel": tr.stats.channel,
            "station": tr.stats.station,
            "start"  : str(tr.stats.starttime)[:19],
            "end"    : str(tr.stats.endtime)[:19],
            "freqs"  : f_out.tolist(),
            "times"  : t_out.tolist(),
            "Sxx"    : S_out.tolist(),
            "vmin"   : float(np.percentile(S_out, 5)),
            "vmax"   : float(np.percentile(S_out, 99)),
        })
    except Exception as exc:
        return jsonify({"error": f"Spectrogram error: {exc}"}), 500


@app.route("/api/result/<cfg_id>/waveform/spectro_all", methods=["GET"])
def result_waveform_spectro_all(cfg_id):
    """Batch spectrogram: all stations that recorded this event, server-side scipy."""
    import numpy as np
    import pandas as pd
    from scipy.signal import spectrogram as _spgram
    from obspy import UTCDateTime
    from obspy.clients.filesystem.sds import Client as SDSClient

    job_id   = request.args.get("job_id",   "").strip()
    event_id = request.args.get("event_id", "").strip()
    pre_s    = float(request.args.get("pre",  10))
    post_s   = float(request.args.get("post", 70))

    if not job_id or not event_id:
        return jsonify({"error": "job_id and event_id required"}), 400

    meta_file = CONFIGS_DIR / cfg_id / "meta.json"
    if not meta_file.exists():
        return jsonify({"error": "Config not found"}), 404
    meta     = json.loads(meta_file.read_text())
    wv_cfg   = meta.get("waveform", {})
    sds_path = wv_cfg.get("path") or wv_cfg.get("fdsn", {}).get("output_path", "")
    if not sds_path:
        return jsonify({"error": "No SDS path configured"}), 400

    job_dir = PIPE_DIR / job_id
    sf = job_dir / "status.json"
    if not sf.exists():
        return jsonify({"error": "Job not found"}), 404
    s  = json.loads(sf.read_text())
    rf = s.get("result_file", "")
    if not rf or not Path(rf).exists():
        return jsonify({"error": "No result file"}), 404

    try:
        df = pd.read_csv(rf)
        ev_row = df[df["event_id"].astype(str) == str(event_id)]
        if ev_row.empty:
            return jsonify({"error": f"Event {event_id} not found"}), 404
        ev = ev_row.iloc[0]
    except Exception as ex:
        return jsonify({"error": f"Catalog error: {ex}"}), 500

    ot_str = str(ev.get("datetime", ""))
    try:
        from datetime import datetime as _dt
        ot = _dt.fromisoformat(ot_str.replace("Z", "").split("+")[0])
    except Exception:
        return jsonify({"error": f"Bad origin time: {ot_str}"}), 400

    # Collect station list from the cached per-event picks index (parsed once/mtime).
    method = s.get("method", "")
    picks_by_sta: dict = {}
    for sta, info in _result_picks_index(job_dir, method).get(str(event_id), {}).items():
        picks_by_sta[sta] = {"net": info["net"], "has_p": "P" in info["phases"]}

    # 1) final.CNV (velest), 2) hypodd_phase.pha, 3) REAL nearest time,
    # 4) bare nearest config stations.
    if not picks_by_sta:
        cnv_file = job_dir / "final.CNV"
        if cnv_file.exists() and str(event_id).startswith("velest_"):
            try:
                cnv_idx_str = str(int(str(event_id).split("_")[1]))
            except Exception:
                cnv_idx_str = None
            if cnv_idx_str is not None:
                cnv_entry = _cnv_picks_index(cnv_file).get(cnv_idx_str)
                if cnv_entry:
                    _, cnv_picks = cnv_entry
                    for sta, info in cnv_picks.items():
                        picks_by_sta[sta] = {"net": info["net"], "has_p": "P" in info["phases"]}
    if not picks_by_sta:
        pha_file = job_dir / "hypodd_phase.pha"
        if pha_file.exists():
            pha_entry = _pha_picks_index(pha_file).get(str(event_id))
            if pha_entry:
                _, pha_picks = pha_entry
                for sta, info in pha_picks.items():
                    picks_by_sta[sta] = {"net": info["net"], "has_p": "P" in info["phases"]}
    if not picks_by_sta:
        rot, real_picks = _real_picks_for_origin(
            cfg_id, ot, ev_lat=float(ev.get("lat", 0)), ev_lon=float(ev.get("lon", 0)))
        if real_picks:
            ot = rot
            for sta, info in real_picks.items():
                picks_by_sta[sta] = {"net": info["net"], "has_p": "P" in info["phases"]}
    if not picks_by_sta:
        for sid, net in _cfg_stations_nearest(meta, ev, 20):
            picks_by_sta[sid] = {"net": net, "has_p": False}

    if not picks_by_sta:
        return jsonify({"error": "No picks found for this event", "specs": []}), 200

    t_start = UTCDateTime(ot) - pre_s
    t_end   = UTCDateTime(ot) + post_s

    try:
        sds = SDSClient(sds_path)
    except Exception as ex:
        return jsonify({"error": f"SDS init: {ex}"}), 500

    # Sort stations by earliest pick time
    def _earliest_pick_spec(item):
        ph = item[1].get("phases", {})
        times = [v for v in ph.values() if v is not None]
        return min(times) if times else float("inf")

    ordered = sorted(picks_by_sta.items(), key=_earliest_pick_spec)[:20]

    specs = []
    for sta, info in ordered:
        net = info.get("net", "7G")
        for ch_pat in channel_search_order("Z"):
            try:
                st = sds.get_waveforms(net, sta, "*", ch_pat, t_start, t_end)
                if not st:
                    continue
                sel = st.select(component="Z")
                tr  = (sel[0] if sel else st[0]).copy()
                tr.detrend("demean")
                raw  = tr.data.astype(np.float64)
                sr   = tr.stats.sampling_rate

                nperseg  = int(2 ** np.floor(np.log2(max(4, min(int(sr * 4), 512)))))
                noverlap = nperseg * 3 // 4

                f, t_arr, Sxx = _spgram(raw, fs=sr, nperseg=nperseg, noverlap=noverlap, scaling="density")
                Sxx_db = 10 * np.log10(Sxx + 1e-30)

                # Convert times to origin-relative
                t_off  = (tr.stats.starttime.datetime - ot).total_seconds()
                t_rel  = (t_arr + t_off).tolist()

                f_max  = min(50.0, sr / 2)
                f_mask = f <= f_max
                f_out  = f[f_mask]
                S_out  = Sxx_db[f_mask, :]

                # Downsample to keep JSON small (≤80 freq bins, ≤300 time bins)
                fs = max(1, len(f_out) // 80)
                ts = max(1, S_out.shape[1] // 300)
                f_out  = f_out[::fs]
                t_rel  = t_rel[::ts]
                S_out  = S_out[::fs, ::ts]

                vmin = float(np.percentile(S_out, 5))
                vmax = float(np.percentile(S_out, 99))

                specs.append({
                    "sta"  : sta,
                    "net"  : net,
                    "cha"  : tr.stats.channel,
                    "freqs": f_out.tolist(),
                    "times": t_rel,
                    "Sxx"  : S_out.tolist(),
                    "vmin" : vmin,
                    "vmax" : vmax,
                })
                break
            except Exception:
                pass

    return jsonify({"specs": specs, "n_sta": len(specs), "pre_s": pre_s, "post_s": post_s})


# ── Slab2 reference seismicity ─────────────────────────────────────────────────

_SLAB2_BASE = Path(__file__).resolve().parent.parent.parent / "core" / "src" / "slab2"
_SLAB2_INPUTS = {
    "hal": _SLAB2_BASE / "slab2code" / "Input" / "09-21" / "hal_09-21_input.csv",
    "phi": _SLAB2_BASE / "slab2code" / "Input" / "09-21" / "phi_09-21_input.csv",
    "sul": _SLAB2_BASE / "slab2code" / "Input" / "09-21" / "sul_09-21_input.csv",
}
_SLAB2_CACHE: dict = {}

@app.route("/api/slab2/points")
def slab2_points():
    """Return Slab2 EQ input points in bbox for cross-section reference."""
    import pandas as pd, math as _math
    try:
        lon_min = float(request.args.get("lon_min", 120))
        lat_min = float(request.args.get("lat_min", -3))
        lon_max = float(request.args.get("lon_max", 130))
        lat_max = float(request.args.get("lat_max",  5))
    except Exception:
        return jsonify({"points": []}), 400

    cache_key = f"{lon_min:.2f},{lat_min:.2f},{lon_max:.2f},{lat_max:.2f}"
    if cache_key in _SLAB2_CACHE:
        return jsonify({"points": _SLAB2_CACHE[cache_key]})

    results = []
    for region, fpath in _SLAB2_INPUTS.items():
        if not fpath.exists():
            continue
        try:
            df = pd.read_csv(fpath, usecols=["lat", "lon", "depth", "etype", "mag"])
            sub = df[
                (df["lon"] >= lon_min) & (df["lon"] <= lon_max) &
                (df["lat"] >= lat_min) & (df["lat"] <= lat_max) &
                (df["etype"].str.upper() == "EQ")
            ]
            for _, r in sub.iterrows():
                dep = r.get("depth")
                lat = r.get("lat")
                lon = r.get("lon")
                mag = r.get("mag")
                if any(isinstance(v, float) and (_math.isnan(v) or _math.isinf(v))
                       for v in [dep, lat, lon]):
                    continue
                results.append({
                    "lat": round(float(lat), 5),
                    "lon": round(float(lon), 5),
                    "dep": round(float(dep), 2),
                    "mag": None if (isinstance(mag, float) and (_math.isnan(mag) or _math.isinf(mag))) else round(float(mag), 2),
                    "src": region,
                })
        except Exception:
            continue

    _SLAB2_CACHE[cache_key] = results
    return jsonify({"points": results})


@app.route("/api/slab")
def slab_contours():
    """Auto-generate Slab2.0 depth contours for the active session AOI.

    Selects every Slab2 region intersecting the bbox, downloads the official
    USGS Slab2.0 depth grids (cached), clips to the AOI and contours them.
    Returns a GeoJSON FeatureCollection (LineString, properties.ELEV = depth km,
    negative downward), same schema as footages/Contour_slabs.geojson, feeding
    both the 2D cross-sections and the 3D mesh surface.
    """
    from seiswork.web import _slab
    try:
        lon_min = float(request.args.get("lon_min", 120))
        lat_min = float(request.args.get("lat_min", -3))
        lon_max = float(request.args.get("lon_max", 130))
        lat_max = float(request.args.get("lat_max", 5))
        cint    = int(float(request.args.get("cint", 25)))
    except Exception:
        return jsonify({"type": "FeatureCollection", "features": [],
                        "error": "invalid bbox"}), 400
    cint = max(5, min(100, cint))
    try:
        fc = _slab.generate(lon_min, lat_min, lon_max, lat_max, cint=cint)
    except Exception as e:
        return jsonify({"type": "FeatureCollection", "features": [],
                        "error": str(e)}), 500
    return jsonify(fc)


# ── Cartopy coastlines for 3D viewer ───────────────────────────────────────────

@app.route("/api/footages/coastlines")
def get_coastlines():
    """Return Natural Earth 10m coastlines clipped to bbox as line segment arrays."""
    try:
        lon_min = float(request.args.get("lon_min", 94))
        lat_min = float(request.args.get("lat_min", -11))
        lon_max = float(request.args.get("lon_max", 142))
        lat_max = float(request.args.get("lat_max",  8))
        pad = 1.0
        import cartopy.io.shapereader as shpreader
        from shapely.geometry import box as shpbox
        bbox = shpbox(lon_min - pad, lat_min - pad, lon_max + pad, lat_max + pad)
        shp_path = shpreader.natural_earth(resolution="10m", category="physical", name="coastline")
        reader   = shpreader.Reader(shp_path)
        lines = []
        for rec in reader.records():
            g = rec.geometry
            b = g.bounds
            if b[2] < lon_min - pad or b[0] > lon_max + pad: continue
            if b[3] < lat_min - pad or b[1] > lat_max + pad: continue
            clipped = g.intersection(bbox)
            if clipped.is_empty: continue
            parts = clipped.geoms if hasattr(clipped, "geoms") else [clipped]
            for part in parts:
                coords = list(part.coords)
                if coords:
                    lines.append([[c[0], c[1]] for c in coords])
        return jsonify({"lines": lines})
    except Exception as exc:
        return jsonify({"lines": [], "error": str(exc)}), 200


# ── Elevation profile for cross-sections ───────────────────────────────────────

_ELEV_CACHE: dict = {}

@app.route("/api/result/elevation-profile")
def get_elevation_profile():
    """Return elevation (m) for a list of lat,lon points via Open Elevation API."""
    import requests as _req, hashlib
    raw_lats = request.args.get("lats", "")
    raw_lons = request.args.get("lons", "")
    try:
        lats = [float(x) for x in raw_lats.split(",") if x.strip()]
        lons = [float(x) for x in raw_lons.split(",") if x.strip()]
    except Exception:
        return jsonify({"error": "invalid coords"}), 400
    if not lats or len(lats) != len(lons):
        return jsonify({"error": "lats/lons mismatch"}), 400
    cache_key = hashlib.md5((raw_lats + "|" + raw_lons).encode()).hexdigest()
    if cache_key in _ELEV_CACHE:
        return jsonify({"elevations": _ELEV_CACHE[cache_key], "source": "cache"})
    locations = [{"latitude": round(la, 5), "longitude": round(lo, 5)}
                 for la, lo in zip(lats, lons)]
    try:
        # open-elevation.com is frequently unreachable (consistent read timeouts);
        # keep a short timeout so we fail fast to the OpenTopoData fallback instead
        # of stalling every topo request ~12s.
        r = _req.post(
            "https://api.open-elevation.com/api/v1/lookup",
            json={"locations": locations},
            timeout=4,
        )
        r.raise_for_status()
        elevs = [x.get("elevation", 0) for x in r.json().get("results", [])]
        if len(elevs) == len(lats):
            _ELEV_CACHE[cache_key] = elevs
            return jsonify({"elevations": elevs, "source": "open-elevation"})
    except Exception:
        pass
    # Fallback: OpenTopoData SRTM90m. Its free tier caps at 100 locations/request
    # and 1 request/second, so chunk and pace; this lets the caller ask for a
    # finer grid (e.g. 16x16=256 pts) without getting an all-zero rejection.
    try:
        import time as _t
        CHUNK = 100
        elevs2 = []
        ok = True
        for s in range(0, len(lats), CHUNK):
            la_c = lats[s:s + CHUNK]
            lo_c = lons[s:s + CHUNK]
            loc_str = "|".join(f"{la},{lo}" for la, lo in zip(la_c, lo_c))
            if s > 0:
                _t.sleep(1.05)            # respect 1 req/sec free-tier limit
            r2 = _req.get(
                f"https://api.opentopodata.org/v1/srtm90m?locations={loc_str}",
                timeout=12,
            )
            r2.raise_for_status()
            part = [x.get("elevation") or 0 for x in r2.json().get("results", [])]
            if len(part) != len(la_c):
                ok = False
                break
            elevs2.extend(part)
        if ok and len(elevs2) == len(lats):
            _ELEV_CACHE[cache_key] = elevs2
            return jsonify({"elevations": elevs2, "source": "opentopodata"})
    except Exception:
        pass
    return jsonify({"elevations": [0] * len(lats), "source": "none"})


# ── Local DEM cache: helpers + 4 endpoints ───────────────────────────────────────

def _dem_npz_path(lon_min, lat_min, lon_max, lat_max, G):
    key = (f"{round(lon_min,2):.2f}_{round(lat_min,2):.2f}"
           f"_{round(lon_max,2):.2f}_{round(lat_max,2):.2f}_G{G}")
    return DEM_DIR / f"{key}.npz"


def _fetch_elev_raw(flat_lats, flat_lons):
    """Fetch elevation (m) for list of points via opentopodata SRTM90m."""
    import requests as _req, time as _t
    CHUNK, elevs = 100, []
    for s in range(0, len(flat_lats), CHUNK):
        la_c = flat_lats[s:s + CHUNK]
        lo_c = flat_lons[s:s + CHUNK]
        loc_str = "|".join(f"{la},{lo}" for la, lo in zip(la_c, lo_c))
        if s > 0:
            _t.sleep(1.05)
        try:
            r = _req.get(
                f"https://api.opentopodata.org/v1/srtm90m?locations={loc_str}",
                timeout=20,
            )
            r.raise_for_status()
            part = [x.get("elevation") or 0 for x in r.json().get("results", [])]
            elevs.extend(part if len(part) == len(la_c) else [0] * len(la_c))
        except Exception:
            elevs.extend([0] * len(la_c))
    return elevs


def _dem_find_covering(lon_min, lat_min, lon_max, lat_max, G):
    """Return (lons, lats, elev_m[][]) by interpolating a cached DEM that covers the bbox."""
    import numpy as np
    try:
        from scipy.interpolate import RegularGridInterpolator
    except ImportError:
        return None
    margin = 0.1
    for f in sorted(DEM_DIR.glob("*.npz"), key=lambda p: -p.stat().st_size):
        try:
            d = np.load(f)
            lons_c, lats_c = d["lons"], d["lats"]
            if not (lons_c[0] <= lon_min - margin and lons_c[-1] >= lon_max + margin and
                    lats_c[0] <= lat_min - margin and lats_c[-1] >= lat_max + margin):
                continue
            elev_c = d["elev"]  # meters, shape (len_lats, len_lons)
            interp = RegularGridInterpolator(
                (lats_c, lons_c), elev_c, method="linear",
                bounds_error=False, fill_value=0,
            )
            lons_q = [lon_min + i * (lon_max - lon_min) / (G - 1) for i in range(G)]
            lats_q = [lat_min + j * (lat_max - lat_min) / (G - 1) for j in range(G)]
            pts = [[la, lo] for la in lats_q for lo in lons_q]
            vals = interp(pts)
            elev_q = [[max(0.0, float(vals[j * G + i])) for i in range(G)] for j in range(G)]
            return lons_q, lats_q, elev_q
        except Exception:
            continue
    return None


@app.route("/api/topo/grid")
def api_topo_grid():
    """Return G×G elevation grid (m) for bbox. Uses local .npz cache first."""
    import numpy as np
    try:
        lon_min = float(request.args.get("lon_min", 127.2))
        lat_min = float(request.args.get("lat_min", 0.6))
        lon_max = float(request.args.get("lon_max", 128.0))
        lat_max = float(request.args.get("lat_max", 1.6))
        G       = max(4, min(64, int(request.args.get("G", 16))))
    except Exception:
        return jsonify({"error": "invalid params"}), 400

    npz = _dem_npz_path(lon_min, lat_min, lon_max, lat_max, G)

    # 1. Exact cache hit
    if npz.exists():
        d = np.load(npz)
        elev = d["elev"].tolist()
        peak = float(d["elev"].max())
        return jsonify({"lons": d["lons"].tolist(), "lats": d["lats"].tolist(),
                        "elev": elev, "peak_m": peak, "source": "cache", "G": G})

    # 2. Interpolate from a larger cached DEM
    covered = _dem_find_covering(lon_min, lat_min, lon_max, lat_max, G)
    if covered:
        lons, lats, elev = covered
        peak = max(v for row in elev for v in row)
        np.savez_compressed(npz, lons=np.array(lons), lats=np.array(lats), elev=np.array(elev))
        return jsonify({"lons": lons, "lats": lats, "elev": elev,
                        "peak_m": peak, "source": "local_cache", "G": G})

    # 3. Fetch from opentopodata API and cache to disk
    lons = [lon_min + i * (lon_max - lon_min) / (G - 1) for i in range(G)]
    lats = [lat_min + j * (lat_max - lat_min) / (G - 1) for j in range(G)]
    flat_lats = [la for la in lats for _ in lons]
    flat_lons = [lo for _ in lats for lo in lons]
    elevs = _fetch_elev_raw(flat_lats, flat_lons)
    if len(elevs) == G * G:
        elev = [[max(0.0, float(elevs[j * G + i])) for i in range(G)] for j in range(G)]
        peak = max(v for row in elev for v in row)
        np.savez_compressed(npz, lons=np.array(lons), lats=np.array(lats), elev=np.array(elev))
        return jsonify({"lons": lons, "lats": lats, "elev": elev,
                        "peak_m": peak, "source": "api", "G": G})
    return jsonify({"error": "failed to fetch elevation"}), 502


@app.route("/api/topo/download", methods=["POST"])
def api_topo_download():
    """Download & cache DEM for a region at custom G. Blocking (~few seconds–minutes)."""
    import numpy as np
    body = request.get_json(force=True) or {}
    try:
        lon_min = float(body.get("lon_min", 95.0))
        lat_min = float(body.get("lat_min", -11.0))
        lon_max = float(body.get("lon_max", 141.0))
        lat_max = float(body.get("lat_max", 6.0))
        G       = max(8, min(100, int(body.get("G", 32))))
        label   = str(body.get("label", "custom"))[:40]
    except Exception:
        return jsonify({"error": "invalid params"}), 400

    npz = _dem_npz_path(lon_min, lat_min, lon_max, lat_max, G)
    if npz.exists():
        d = np.load(npz)
        return jsonify({"status": "already_cached", "filename": npz.name,
                        "G": G, "points": G * G, "peak_m": float(d["elev"].max())})

    lons = [lon_min + i * (lon_max - lon_min) / (G - 1) for i in range(G)]
    lats = [lat_min + j * (lat_max - lat_min) / (G - 1) for j in range(G)]
    flat_lats = [la for la in lats for _ in lons]
    flat_lons = [lo for _ in lats for lo in lons]
    elevs = _fetch_elev_raw(flat_lats, flat_lons)
    if len(elevs) == G * G:
        elev = [[max(0.0, float(elevs[j * G + i])) for i in range(G)] for j in range(G)]
        peak = max(v for row in elev for v in row)
        np.savez_compressed(npz, lons=np.array(lons), lats=np.array(lats),
                            elev=np.array(elev), label=np.array([label]))
        return jsonify({"status": "ok", "filename": npz.name, "G": G,
                        "points": G * G, "peak_m": round(peak), "label": label})
    return jsonify({"error": "failed to fetch elevation"}), 502


@app.route("/api/topo/list")
def api_topo_list():
    """List all locally cached DEM .npz files."""
    import numpy as np
    result = []
    for f in sorted(DEM_DIR.glob("*.npz")):
        try:
            parts = f.stem.split("_G")
            coords = [float(x) for x in parts[0].split("_")]
            G_val  = int(parts[1]) if len(parts) > 1 else 0
            d      = np.load(f)
            lbl    = str(d["label"][0]) if "label" in d else ""
            peak   = round(float(d["elev"].max())) if "elev" in d else 0
            result.append({"key": f.stem, "filename": f.name,
                           "lon_min": coords[0], "lat_min": coords[1],
                           "lon_max": coords[2], "lat_max": coords[3],
                           "G": G_val, "label": lbl, "peak_m": peak,
                           "size_kb": round(f.stat().st_size / 1024, 1)})
        except Exception:
            pass
    return jsonify(result)


@app.route("/api/topo/delete", methods=["POST"])
def api_topo_delete():
    """Delete a specific cached DEM file by key (stem without .npz)."""
    body = request.get_json(force=True) or {}
    key  = str(body.get("key", ""))
    if not key or "/" in key or ".." in key:
        return jsonify({"error": "invalid key"}), 400
    f = DEM_DIR / f"{key}.npz"
    if f.exists():
        f.unlink()
        return jsonify({"status": "deleted", "key": key})
    return jsonify({"error": "not found"}), 404


# ── Server-side proxy (client -> local server -> remote SeisWork server) ────────
# Browser may not have direct network access to the remote server, but the local
# Flask process can. All /api/remote/* calls are forwarded server-side.

_PROXY_CFG: dict = {"url": "", "token": ""}   # set by /api/remote/connect

@app.route("/api/remote/connect", methods=["POST"])
def remote_connect():
    data  = request.get_json(force=True) or {}
    url   = (data.get("url") or "").rstrip("/")
    token = (data.get("token") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    try:
        hdrs = {"Authorization": f"Bearer {token}"} if token else {}
        r = requests.get(f"{url}/api/health", headers=hdrs, timeout=8)
        h = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    if not h.get("ok"):
        return jsonify({"error": "bad health response"}), 502
    if h.get("auth_required") and not token:
        return jsonify({"error": "server requires token"}), 403
    _PROXY_CFG["url"]   = url
    _PROXY_CFG["token"] = token
    return jsonify(h)

@app.route("/api/remote/disconnect", methods=["POST"])
def remote_disconnect():
    _PROXY_CFG["url"] = _PROXY_CFG["token"] = ""
    return jsonify({"ok": True})

@app.route("/api/remote/status", methods=["GET"])
def remote_status():
    return jsonify({"url": _PROXY_CFG["url"],
                    "connected": bool(_PROXY_CFG["url"])})

@app.route("/api/remote/<path:subpath>", methods=["GET","POST","PUT","DELETE","OPTIONS"])
def remote_proxy(subpath):
    """Forward /api/remote/<subpath> to remote_url/api/<subpath> server-side."""
    base = _PROXY_CFG.get("url")
    if not base:
        return jsonify({"error": "no remote server configured"}), 503
    token = _PROXY_CFG.get("token", "")
    hdrs  = {"Authorization": f"Bearer {token}"} if token else {}
    # Forward Content-Type for POST/PUT
    if request.content_type:
        hdrs["Content-Type"] = request.content_type
    target = f"{base}/api/{subpath}"
    if request.query_string:
        target += "?" + request.query_string.decode()
    try:
        r = requests.request(
            method  = request.method,
            url     = target,
            headers = hdrs,
            data    = request.get_data(),
            timeout = 60,
            stream  = True,
        )
        # Stream the response back (supports SSE / large files)
        return app.response_class(
            r.iter_content(chunk_size=4096),
            status      = r.status_code,
            content_type= r.headers.get("Content-Type", "application/json"),
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── Client Sync - manifest + bundle download ────────────────────────────────────
# Two endpoints that let a remote SeisWork client mirror server results to local
# storage. The manifest lists every job with a cheap content checksum so the
# client skips jobs that have not changed since the last pull. The bundle
# endpoint streams a ZIP of all job artifacts (catalogs, configs, logs, phases)
# excluding raw waveform binaries (.mseed/.seed), which are very large and
# already covered by the waveform-download module.

import hashlib
import io as _io
import zipfile

_SYNC_BUNDLE_SKIP_EXT = {".mseed", ".seed", ".sac"}
# Sub-folder names inside a job dir that are excluded from sync bundles.
# waveforms/: SDS-format raw waveforms stored by cross-corr/GrowClust jobs (can be 77 GB+)
_SYNC_BUNDLE_SKIP_DIRS = {"waveforms"}


def _sync_skip(f: Path, job_dir: Path) -> bool:
    """Return True if file *f* should be excluded from a sync bundle."""
    if f.suffix in _SYNC_BUNDLE_SKIP_EXT:
        return True
    # Skip if any ancestor directory name is in the skip-dirs set.
    try:
        parts = f.relative_to(job_dir).parts
        return bool(set(parts[:-1]) & _SYNC_BUNDLE_SKIP_DIRS)
    except ValueError:
        return True


def _sync_job_entry(kind: str, d: Path) -> dict | None:
    """Build one manifest entry for job dir *d*. Returns None if not a valid job."""
    sf = d / "status.json"
    if not sf.is_file():
        return None
    try:
        s = json.loads(sf.read_text())
    except Exception:
        return None
    n_files = 0
    total_bytes = 0
    msum = hashlib.md5()
    for f in sorted(d.rglob("*")):
        if not f.is_file() or _sync_skip(f, d):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        n_files += 1
        total_bytes += st.st_size
        msum.update(f"{f.relative_to(d)}:{st.st_size}:{st.st_mtime:.0f}".encode())
    return {
        "id"         : d.name,
        "kind"       : kind,
        "step"       : s.get("step", ""),
        "method"     : s.get("method", ""),
        "state"      : s.get("state", ""),
        "cfg_id"     : s.get("cfg_id", ""),
        "started"    : s.get("started", ""),
        "finished"   : s.get("finished", ""),
        "events"     : s.get("events", 0),
        "checksum"   : msum.hexdigest(),
        "n_files"    : n_files,
        "size_bytes" : total_bytes,
    }


@app.route("/api/sync/manifest", methods=["GET"])
def sync_manifest():
    """Return metadata + checksums for jobs belonging to one project (?cfg_id=
    required), authorized by that project's own sync token, never every
    project hosted on this server. See _cfg_sync_token / _check_sync_token."""
    cfg_id = request.args.get("cfg_id", "").strip()
    if not cfg_id:
        return jsonify({"error": "cfg_id required"}), 400
    if not (CONFIGS_DIR / cfg_id / "meta.json").exists():
        return jsonify({"error": f"Config {cfg_id} not found"}), 404
    if not _check_sync_token(cfg_id):
        return jsonify({"error": "unauthorized",
                        "detail": "missing or invalid sync token for this cfg_id"}), 401
    jobs = []
    bases = [("pipeline", PIPE_DIR), ("picking", PICK_DIR)]
    for kind, base in bases:
        if not base.exists():
            continue
        for d in sorted(base.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            entry = _sync_job_entry(kind, d)
            if entry and entry.get("cfg_id") == cfg_id:
                jobs.append(entry)
    return jsonify({
        "server_id"    : SERVER_ID,
        "cfg_id"       : cfg_id,
        "generated_at" : datetime.now().isoformat(timespec="seconds"),
        "jobs"         : jobs,
    })


@app.route("/api/sync/jobs/<job_id>/bundle", methods=["GET"])
def sync_job_bundle(job_id):
    """Stream a ZIP of all job artifacts for one job. ?kind=pipeline (default) or
    picking, ?cfg_id= required. The job must belong to that project and the
    caller must hold that project's own sync token (see sync_manifest).
    Raw waveform files (.mseed/.seed/.sac) are excluded to keep bundles small.
    The client extracts the ZIP to its local mirror directory."""
    kind   = request.args.get("kind", "pipeline").strip()
    cfg_id = request.args.get("cfg_id", "").strip()
    if not cfg_id:
        return jsonify({"error": "cfg_id required"}), 400
    if not _check_sync_token(cfg_id):
        return jsonify({"error": "unauthorized",
                        "detail": "missing or invalid sync token for this cfg_id"}), 401
    base = PIPE_DIR if kind == "pipeline" else PICK_DIR
    job_dir = (base / job_id).resolve()
    # Path-traversal guard
    if not str(job_dir).startswith(str(base.resolve())) or not job_dir.is_dir():
        return jsonify({"error": "job not found"}), 404
    sf = job_dir / "status.json"
    try:
        owner = json.loads(sf.read_text()).get("cfg_id", "") if sf.is_file() else ""
    except Exception:
        owner = ""
    if owner != cfg_id:
        return jsonify({"error": "job not found"}), 404
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for f in sorted(job_dir.rglob("*")):
            if not f.is_file() or _sync_skip(f, job_dir):
                continue
            try:
                zf.write(str(f), str(f.relative_to(job_dir)))
            except Exception:
                pass
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"seiswork_{job_id}.zip",
    )


# ── Local offline data: serve locally synced jobs when remote is unreachable ───
@app.route("/api/local/offline", methods=["GET"])
def local_offline_data():
    """Return locally synced jobs from ~/.seiswork/remote/, grouped by cfg_id.
    Called by the GUI when the remote server is unreachable (offline mode)."""
    from seiswork.client import SEISWORK_HOME
    sync_dir = SEISWORK_HOME / "remote"
    _step_order = {"picking": 0, "pick": 0, "assoc": 1, "locate": 2,
                   "magnitude": 3, "velocity": 4, "relocation": 5}
    configs: dict = {}
    for kind in ("pipeline", "picking"):
        kind_dir = sync_dir / kind
        if not kind_dir.exists():
            continue
        for job_dir in sorted(kind_dir.iterdir(),
                              key=lambda x: x.stat().st_mtime, reverse=True):
            if not job_dir.is_dir():
                continue
            sf = job_dir / "status.json"
            if not sf.exists():
                continue
            try:
                s = json.loads(sf.read_text())
            except Exception:
                continue
            cfg_id = s.get("cfg_id") or "unknown"
            if cfg_id not in configs:
                configs[cfg_id] = {"id": cfg_id, "name": cfg_id,
                                   "jobs": [], "n_jobs": 0, "local": True}
            files = [f.name for f in sorted(job_dir.iterdir())
                     if f.is_file() and f.name != ".sync_checksum"]
            configs[cfg_id]["jobs"].append({
                "id"      : job_dir.name,
                "kind"    : kind,
                "step"    : s.get("step", kind),
                "method"  : s.get("method", ""),
                "state"   : s.get("state", ""),
                "started" : s.get("started", ""),
                "finished": s.get("finished", ""),
                "events"  : s.get("events", 0),
                "picks"   : (s.get("picks") or {}).get("total", 0),
                "files"   : files,
            })
    result = list(configs.values())
    for c in result:
        c["jobs"].sort(key=lambda j: (_step_order.get(j["step"], 99),
                                      j.get("started", "")))
        c["n_jobs"] = len(c["jobs"])
    return jsonify({"offline": True, "configs": result,
                    "total": len(result), "sync_dir": str(sync_dir)})


@app.route("/api/local/result/<job_id>/<path:filename>", methods=["GET"])
def local_result_file(job_id, filename):
    """Serve a file from the local sync dir for offline result viewing."""
    from seiswork.client import SEISWORK_HOME
    sync_dir = SEISWORK_HOME / "remote"
    for kind in ("pipeline", "picking"):
        job_dir = (sync_dir / kind / job_id).resolve()
        base    = (sync_dir / kind).resolve()
        if not str(job_dir).startswith(str(base)):
            continue
        target = (job_dir / filename).resolve()
        if not str(target).startswith(str(job_dir)):
            continue
        if target.exists() and target.is_file():
            return send_file(str(target))
    return jsonify({"error": "file not found"}), 404


# ── Local-only sync pull: streams NDJSON progress back to the browser ──────────
@app.route("/api/local/sync/pull", methods=["POST"])
def local_sync_pull():
    """Pull job bundles from the connected remote server, streaming progress.

    Only works when _PROXY_CFG["url"] is set (remote server connected).
    Body: { cfg_id: str (required), sync_token: str (this project's token on
            the remote, falls back to the connection's token), force: bool }
    Response: newline-delimited JSON events:
      {"type":"start",    "total": N}
      {"type":"progress", "i": i, "total": N, "pct": 0-100, "id":..., "action":...}
      {"type":"done",     "pulled": N, "skipped": N, "errors": N, "sync_dir": "..."}
      {"type":"error",    "error": "..."}   (on fatal failure)
    """
    from seiswork.client import SeisWorkClient
    import json as _json

    base  = _PROXY_CFG.get("url", "")
    token = _PROXY_CFG.get("token", "")
    if not base:
        return jsonify({"error": "no remote server connected — use Connect first"}), 503
    data       = request.get_json(force=True) or {}
    cfg_id     = (data.get("cfg_id") or "").strip()
    sync_token = (data.get("sync_token") or "").strip() or token
    force      = bool(data.get("force", False))
    if not cfg_id:
        return jsonify({"error": "cfg_id required"}), 400

    def _generate():
        try:
            cli      = SeisWorkClient(base, token)
            manifest = cli.sync_manifest(cfg_id, sync_token=sync_token)
        except Exception as e:
            yield _json.dumps({"type": "error", "error": f"manifest fetch failed: {e}"}) + "\n"
            return

        jobs = manifest.get("jobs", [])
        total = len(jobs)
        yield _json.dumps({"type": "start", "total": total, "cfg_id": cfg_id}) + "\n"

        results = []
        for i, job in enumerate(jobs, 1):
            jid        = job["id"]
            kind       = job["kind"]
            server_sum = job.get("checksum", "")
            local_dir  = cli.sync_dir / kind / jid
            local_chk  = local_dir / ".sync_checksum"

            if not force and local_chk.exists():
                try:
                    if local_chk.read_text().strip() == server_sum:
                        res = {"id": jid, "kind": kind, "action": "skip"}
                        results.append(res)
                        pct = round(i * 100 / total) if total else 100
                        yield _json.dumps({"type": "progress", "i": i, "total": total,
                                           "pct": pct, **res}) + "\n"
                        continue
                except Exception:
                    pass

            try:
                path = cli.pull_bundle(jid, cfg_id, kind=kind, sync_token=sync_token)
                if server_sum:
                    (path / ".sync_checksum").write_text(server_sum)
                res = {"id": jid, "kind": kind, "action": "pulled", "path": str(path)}
            except Exception as exc:
                res = {"id": jid, "kind": kind, "action": "error", "error": str(exc)}
            results.append(res)
            pct = round(i * 100 / total) if total else 100
            yield _json.dumps({"type": "progress", "i": i, "total": total,
                               "pct": pct, **res}) + "\n"

        pulled  = sum(1 for r in results if r["action"] == "pulled")
        skipped = sum(1 for r in results if r["action"] == "skip")
        errors  = sum(1 for r in results if r["action"] == "error")
        yield _json.dumps({
            "type"    : "done",
            "ok"      : errors == 0 or bool(pulled),
            "cfg_id"  : cfg_id,
            "total"   : total,
            "pulled"  : pulled,
            "skipped" : skipped,
            "errors"  : errors,
            "sync_dir": str(cli.sync_dir),
        }) + "\n"

    return Response(stream_with_context(_generate()),
                    mimetype="text/plain; charset=utf-8")


# ── Minimal Plotly 3D diagnostic page ──────────────────────────────────────────

@app.route("/test3d")
def test3d():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{margin:0;background:#0d1320}#plot{width:100vw;height:100vh}</style>
</head><body>
<div id="plot"></div>
<script src="/static/js/vendor/plotly-2.35.2.min.js"></script>
<script>
var gl = (function(){try{var c=document.createElement('canvas');return !!(c.getContext('webgl')||c.getContext('experimental-webgl'));}catch(e){return false;}})();
document.title = 'WebGL: ' + gl;
var n=200,lats=[],lons=[],deps=[],mags=[];
for(var i=0;i<n;i++){lats.push(0.5+Math.random()*2);lons.push(126.5+Math.random()*2);deps.push(Math.random()*80);mags.push(1+Math.random()*4);}
Plotly.newPlot('plot',[{type:'scatter3d',mode:'markers',x:lons,y:lats,z:deps.map(function(d){return -d;}),
  marker:{size:4,color:deps,colorscale:'Viridis',opacity:0.8},hoverinfo:'x+y+z'}],
  {paper_bgcolor:'#0d1320',font:{color:'#e2e8f0'},
   scene:{dragmode:'turntable',xaxis:{title:'Lon'},yaxis:{title:'Lat'},zaxis:{title:'Depth'},bgcolor:'#0d1320'},
   margin:{t:10,r:10,b:10,l:10},annotations:[{text:'WebGL: '+gl+' | Drag to rotate',showarrow:false,xref:'paper',yref:'paper',x:0.5,y:1,font:{color:'#60a5fa',size:14}}]},
  {responsive:true,displayModeBar:true});
</script></body></html>""", 200, {'Content-Type': 'text/html'}


# ── Map tile proxy (browser -> Flask -> CartoDB/OSM, solves network restriction) ─

import functools
_tile_cache = {}  # simple in-memory LRU-like cache
_TILE_CACHE_MAX = 512

@app.route("/tiles/<int:z>/<int:x>/<int:y>.png")
def tile_proxy(z, x, y):
    key = (z, x, y)
    if key in _tile_cache:
        return Response(_tile_cache[key], content_type="image/png",
                        headers={"Cache-Control": "public,max-age=86400"})
    url = f"https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
    try:
        r = requests.get(url, timeout=6,
                         headers={"User-Agent": "SeisWork/1.2 tile-proxy"})
        if r.status_code == 200:
            if len(_tile_cache) >= _TILE_CACHE_MAX:
                # evict oldest entry
                _tile_cache.pop(next(iter(_tile_cache)))
            _tile_cache[key] = r.content
            return Response(r.content, content_type="image/png",
                            headers={"Cache-Control": "public,max-age=86400"})
    except Exception:
        pass
    return Response(b"", status=204)


# ── Presentation Builder ────────────────────────────────────────────────────────

@app.route("/present/<cfg_id>")
def present_view(cfg_id):
    return render_template("present.html", cfg_id=cfg_id, asset_version=_asset_version())


@app.route("/api/present/<cfg_id>", methods=["GET"])
def get_presentation(cfg_id):
    from seiswork.web._presentation import load_presentation
    cfg_dir = CONFIGS_DIR / cfg_id
    if not cfg_dir.exists():
        return jsonify({"error": "config not found"}), 404
    return jsonify(load_presentation(cfg_dir))


@app.route("/api/present/<cfg_id>", methods=["POST"])
def save_presentation_route(cfg_id):
    from seiswork.web._presentation import save_presentation
    cfg_dir = CONFIGS_DIR / cfg_id
    if not cfg_dir.exists():
        return jsonify({"error": "config not found"}), 404
    data = request.get_json(force=True) or {}
    save_presentation(cfg_dir, data)
    return jsonify({"ok": True})


# ── Entry point ────────────────────────────────────────────────────────────────

def _port_is_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """True if something is already listening on host:port (probe before spawn)."""
    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except OSError:
        return False


def _spawn_online_viewer(main_port: int):
    """Launch the read-only Online Viewer mirror on VIEWER_PORT as a child
    process. Idempotent: if the port is already served (e.g. an earlier instance
    survived a hot-restart re-exec), reuse it instead of spawning a duplicate.
    The child is terminated when this server shuts down (but NOT on a hot-restart
    re-exec, which keeps the same PID and never runs atexit, so the mirror keeps
    running and is reused by the fresh image)."""
    import subprocess
    if VIEWER_PORT == main_port:
        return
    if _port_is_open("127.0.0.1", VIEWER_PORT):
        print(f"[viewer] mirror already up on :{VIEWER_PORT} — reusing.", flush=True)
        return
    env = dict(os.environ)
    env["SEISWORK_VIEWER_MODE"]     = "1"
    env["SEISWORK_VIEWER_UPSTREAM"] = f"http://127.0.0.1:{main_port}"
    env["SEISWORK_VIEWER_PORT"]     = str(VIEWER_PORT)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "seiswork.web.app",
             "--host", "0.0.0.0", "--port", str(VIEWER_PORT)],
            env=env)
    except Exception as e:
        print(f"[viewer] failed to spawn mirror: {e}", flush=True)
        return
    print(f"[viewer] online mirror (read-only) on :{VIEWER_PORT} "
          f"→ upstream :{main_port}  (pid {proc.pid})", flush=True)
    import atexit
    def _kill_viewer():
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    atexit.register(_kill_viewer)


def run(host: str = "0.0.0.0", port: int = 5000, debug: bool = False):
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    PICK_DIR.mkdir(parents=True, exist_ok=True)
    PIPE_DIR.mkdir(parents=True, exist_ok=True)
    # Auto-launch the read-only Online Viewer mirror alongside the main GUI
    # (skipped when we ARE the viewer, to avoid spawning a viewer-of-a-viewer).
    if not VIEWER_MODE:
        _spawn_online_viewer(port)
    # Watch .py sources and hot-restart the server when idle (frontend assets
    # hot-reload in the browser via /api/livereload without any restart).
    if AUTO_RESTART:
        threading.Thread(target=_watch_py_and_restart, daemon=True).start()
        print("[live-reload] active — GUI assets stream live; "
              "Python edits restart the server when idle.", flush=True)
    # Pre-warm pandas/numpy in background so the first heavy endpoint
    # (e.g. View Result) is not slowed by cold-import (~1 second).
    def _warm_heavy_imports():
        try:
            import pandas, numpy  # noqa: F401
            print("[warmup] pandas/numpy ready.", flush=True)
        except Exception:
            pass
    threading.Thread(target=_warm_heavy_imports, daemon=True).start()
    app.run(host=host, port=port, debug=debug, use_reloader=False, threaded=True)


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser(prog="seiswork.web.app")
    _p.add_argument("--host", default="0.0.0.0")
    _p.add_argument("--port", type=int, default=5000)
    _p.add_argument("--debug", action="store_true")
    _a = _p.parse_args()
    run(host=_a.host, port=_a.port, debug=_a.debug)
