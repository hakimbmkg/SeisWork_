#!/usr/bin/env python3
"""
SeisWork Agent — by HakimBMKG
Runs on the SeisComP server (same or a different machine than SeisWork).
Receives commands from the SeisWork GUI (push inventory, binding, restart) and
forwards picks from the SeisComP message bus to SeisWork /api/online/trigger.

Usage:
    python agent.py --init          # generate a token, save it to ~/.seiswork-agent.conf
    python agent.py --start         # run the daemon (default port 7001)
    python agent.py --port 7001     # port custom
    python agent.py --status        # check the service status

Install (run from the SeisWork repo):
    bash seiswork-agent/install_agent.sh
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from datetime import datetime

# ── Dependency minimal (stdlib + Flask) ───────────────────────────────────────
try:
    from flask import Flask, request, jsonify
except ImportError:
    print("[ERROR] Flask not available. Install: pip install flask")
    sys.exit(1)

# ── Configuration ───────────────────────────────────────────────────────────────
CONF_FILE = Path.home() / ".seiswork-agent.conf"
DEFAULT_PORT = 7001
AGENT_VERSION = "0.0.1(BETA)"

app = Flask(__name__)

_config = {}
_bridge_thread = None
_bridge_running = False
_seiswork_url = None       # SeisWork URL for pushing picks
_seiswork_token = None     # Token SeisWork


# ── Helper ──────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    if CONF_FILE.exists():
        with open(CONF_FILE) as f:
            return json.load(f)
    return {}


def _save_config(cfg: dict):
    CONF_FILE.write_text(json.dumps(cfg, indent=2))
    CONF_FILE.chmod(0o600)


def _check_token(req) -> bool:
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]
    # Always read from the file so a new token (from re-running --init) takes effect
    # immediately, without restarting the agent.
    stored = _load_config().get("token_hash", "") or _config.get("token_hash", "")
    return hashlib.sha256(token.encode()).hexdigest() == stored


def _seiscomp_root() -> Path | None:
    env_root = os.environ.get("SEISCOMP_ROOT")
    if env_root and Path(env_root).exists():
        return Path(env_root)
    for candidate in [Path.home() / "seiscomp", Path("/opt/seiscomp"), Path("/usr/local/seiscomp")]:
        if (candidate / "bin" / "seiscomp").exists():
            return candidate
    return None


def _run_seiscomp(*args, timeout: int = 30) -> tuple[int, str, str]:
    root = _seiscomp_root()
    if not root:
        return -1, "", "SeisComP not found"
    bin_path = root / "bin" / "seiscomp"
    try:
        result = subprocess.run(
            [str(bin_path)] + list(args),
            capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "Timeout"
    except Exception as e:
        return -1, "", str(e)


# ── SeedLink INFO helper ────────────────────────────────────────────────────────
def _slinktool_streams(host: str, port: int, timeout: float = 15.0):
    """Query streams via the `slinktool -Q` binary (SeisComP ships it). This is
    far more robust than hand-parsing the raw SeedLink INFO packets (those wrap
    the XML inside MiniSEED log records — 8-byte SL header + 512-byte record with
    its own header — which the naive offset-8 parse corrupts → 'not well-formed'
    XML). Returns a stream list, or None when slinktool is unavailable/failed so
    the caller falls back to the raw-socket parse.

    slinktool -Q line format:  NET STA [LOC] CHA TYPE  YYYY/MM/DD HH:MM:SS  -  YYYY/MM/DD HH:MM:SS
    LOC may be blank; the data-type token (single letter, e.g. D) anchors the parse."""
    root = _seiscomp_root()
    cmds = []
    if root:
        cmds.append([str(root / "bin" / "seiscomp"), "exec", "slinktool", "-Q", f"{host}:{port}"])
    slt = shutil.which("slinktool")
    if slt:
        cmds.append([slt, "-Q", f"{host}:{port}"])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception:
            continue
        if r.returncode != 0 or not r.stdout.strip():
            continue
        out = []
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            # Anchor on the data-type token (a lone letter) at index 3 (no loc) or 4 (loc).
            type_idx = None
            for i in range(3, min(6, len(parts))):
                if len(parts[i]) == 1 and parts[i].isalpha():
                    type_idx = i
                    break
            if type_idx is None or type_idx < 3:
                continue
            net, sta = parts[0], parts[1]
            cha = parts[type_idx - 1]
            loc = parts[type_idx - 2] if (type_idx - 2) >= 2 else ""
            out.append({"net": net, "sta": sta, "loc": loc, "cha": cha,
                        "sampling_rate": None, "start": "", "end": ""})
        if out:
            return out
    return None


def _query_seedlink_streams(host: str, port: int, timeout: float = 8.0) -> list[dict]:
    """
    Query SeedLink server for available streams.
    Return: [{net, sta, loc, cha, sampling_rate, start, end}]

    Primary path: `slinktool -Q` (robust). Fallback: raw-socket INFO parse.
    """
    import socket, xml.etree.ElementTree as ET

    # Prefer slinktool — the raw INFO parse below mis-handles the MiniSEED framing
    # on some SeisComP seedlink builds ('not well-formed' XML at ~col 433).
    via_tool = _slinktool_streams(host, port, timeout=max(timeout, 15.0))
    if via_tool is not None:
        return via_tool

    result = []
    try:
        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, port))

        # HELLO
        s.send(b"HELLO\r\n")
        banner = s.recv(256)

        # INFO STREAMS — response berformat SeedLink info packet (512-byte records ending with END)
        s.send(b"INFO STREAMS\r\n")
        raw = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            raw += chunk
            # SeedLink info packet: scan for </seedlink> marking the end of the XML
            if b"</seedlink>" in raw:
                break
        s.close()

        # Extract XML from the raw bytes (8-byte SEED header per 512-byte record → skip non-XML bytes)
        # SeedLink info packet: 8-byte header + 512 byte data per record
        xml_parts = []
        i = 0
        while i < len(raw):
            if i + 8 < len(raw) and raw[i:i+2] in (b"SL", b"sl"):
                payload = raw[i+8:i+8+504]
                xml_parts.append(payload)
                i += 512
            else:
                # May be raw XML without a header (some implementations)
                xml_parts.append(raw[i:])
                break

        xml_str = b"".join(xml_parts).rstrip(b"\x00").decode("utf-8", errors="replace")
        # Cari tag <seedlink>
        start = xml_str.find("<seedlink")
        end   = xml_str.find("</seedlink>") + len("</seedlink>")
        if start >= 0 and end > start:
            xml_str = xml_str[start:end]
        else:
            # Try parsing directly
            pass

        root = ET.fromstring(xml_str)
        for sta_el in root.findall(".//station"):
            net = sta_el.get("network", "")
            sta = sta_el.get("name", "")
            for ch_el in sta_el.findall(".//stream"):
                loc = ch_el.get("location", "")
                cha = ch_el.get("seedname", "")
                sps = ch_el.get("samprate", "")
                t_start = ch_el.get("begin_time", "")
                t_end   = ch_el.get("end_time", "")
                if net and sta and cha:
                    result.append({
                        "net": net, "sta": sta, "loc": loc, "cha": cha,
                        "sampling_rate": float(sps) if sps else None,
                        "start": t_start, "end": t_end,
                    })

    except Exception as e:
        raise RuntimeError(f"SeedLink query error: {e}")

    return result


# ── Auth middleware ─────────────────────────────────────────────────────────────
def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_token(request):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ── Endpoint: Status ────────────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
@require_auth
def status():
    root = _seiscomp_root()
    rc, out, err = _run_seiscomp("status") if root else (-1, "", "not found")
    modules = {}
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3 and parts[1] == "is":
            modules[parts[0]] = "running" if parts[2] == "running" else "stopped"

    return jsonify({
        "ok": True,
        "agent_version": AGENT_VERSION,
        "seiscomp_root": str(root) if root else None,
        "seiscomp_found": root is not None,
        "modules": modules,
        "bridge_running": _bridge_running,
        "seiswork_url": _seiswork_url,
        "checked_at": datetime.utcnow().isoformat() + "Z",
    })


# ── Endpoint: Info Streams (auto-discovery from SeedLink) ──────────────────────
@app.route("/info/streams", methods=["GET"])
@require_auth
def info_streams():
    """
    Query the SeedLink server, return the list of available NET.STA.LOC.CHA.
    Used by the SeisWork wizard to auto-inject streams into SeisComP without a manual XML.
    """
    host = (request.args.get("host") or "localhost").strip()
    port = int(request.args.get("port") or 18000)
    try:
        streams = _query_seedlink_streams(host, port)
        # Summarize: group per station
        stations: dict[str, dict] = {}
        for s in streams:
            key = f"{s['net']}.{s['sta']}"
            if key not in stations:
                stations[key] = {"net": s["net"], "sta": s["sta"], "channels": []}
            stations[key]["channels"].append({
                "loc": s["loc"], "cha": s["cha"],
                "sampling_rate": s.get("sampling_rate"),
            })
        return jsonify({
            "ok": True,
            "host": host, "port": port,
            "n_streams": len(streams),
            "n_stations": len(stations),
            "stations": list(stations.values()),
            "streams": streams,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "host": host, "port": port}), 500


# ── Endpoint: Sync SeisComP (discover + inject) ─────────────────────────────────
@app.route("/sync", methods=["POST"])
@require_auth
def sync_seiscomp():
    """
    Workflow lengkap: query seedlink → generate inventory + binding → push ke SeisComP → restart.
    Body: {seedlink_host, seedlink_port, inventory_xml (optional, for metadata)}
    """
    data = request.get_json(force=True) or {}
    sl_host = (data.get("seedlink_host") or "localhost").strip()
    sl_port  = int(data.get("seedlink_port") or 18000)
    inv_xml  = data.get("inventory_xml", "")  # optional FDSNXML for metadata
    station_channels = data.get("station_channels", {})  # {sta: channel} from the meta config (fallback)

    root = _seiscomp_root()
    if not root:
        return jsonify({"error": "SeisComP not found"}), 500

    steps = []

    # 1. Discover streams from seedlink (optional — on failure, continue with the inventory)
    streams = []
    try:
        streams = _query_seedlink_streams(sl_host, sl_port)
        steps.append({"step": "discover", "ok": True, "n_streams": len(streams)})
    except Exception as e:
        steps.append({"step": "discover", "ok": False, "warning": str(e),
                       "note": "INFO STREAMS timeout/error — the binding will be built from the inventory XML"})

    # 2. Push the inventory XML when provided
    # Deteksi format: FDSN StationXML vs SeisComP XML
    if inv_xml:
        inv_dir = root / "etc" / "inventory"
        inv_dir.mkdir(parents=True, exist_ok=True)
        is_fdsn = "FDSNStationXML" in inv_xml[:200]

        if is_fdsn:
            # FDSN StationXML → convert to SeisComP format first with fdsnxml2inv
            import tempfile as _tmp
            with _tmp.NamedTemporaryFile(suffix=".xml", delete=False, mode="w", encoding="utf-8") as _f:
                _f.write(inv_xml); _fdsn_path = _f.name
            sc3_path = str(inv_dir / "seiswork_online.xml")
            rc_conv, out_conv, err_conv = _run_seiscomp(
                "exec", "fdsnxml2inv", _fdsn_path, sc3_path, "--log-level=3", timeout=30)
            Path(_fdsn_path).unlink(missing_ok=True)
            if rc_conv == 0 and Path(sc3_path).exists():
                steps.append({"step": "fdsnxml_convert", "ok": True})
            else:
                # If fdsnxml2inv fails, save it as-is and still try scinv
                Path(sc3_path).write_text(inv_xml, encoding="utf-8")
                steps.append({"step": "fdsnxml_convert", "ok": False,
                               "warning": (err_conv or out_conv)[:200]})
        else:
            (inv_dir / "seiswork_online.xml").write_text(inv_xml, encoding="utf-8")

        rc, out, err = _run_seiscomp("exec", "scinv", "sync", "--filebase", str(inv_dir), timeout=90)
        steps.append({"step": "inventory_sync", "ok": rc == 0, "rc": rc,
                       "stderr": err[:400] if err else ""})
    else:
        steps.append({"step": "inventory_sync", "ok": True, "skipped": True})

    # 3. Generate binding key files
    # Source: discovered streams (when successful) OR parsed inventory XML (FDSN or SC3)
    net_sta_seen = set()
    if streams:
        for s in streams:
            net_sta_seen.add((s["net"], s["sta"]))
    elif inv_xml:
        try:
            import xml.etree.ElementTree as _ET
            _root_xml = _ET.fromstring(inv_xml)
            # FDSN StationXML: <Network code="IA"><Station code="APSI">
            _fdsn_ns = "http://www.fdsn.org/xml/station/1"
            for _net_el in _root_xml.iter(f"{{{_fdsn_ns}}}Network"):
                net_code = _net_el.get("code", "")
                for _sta_el in _net_el.iter(f"{{{_fdsn_ns}}}Station"):
                    sta_code = _sta_el.get("code", "")
                    if sta_code:
                        net_sta_seen.add((net_code or "IA", sta_code))
            if not net_sta_seen:
                # SeisComP XML fallback
                for _sta_el in _root_xml.iter():
                    if _sta_el.tag.endswith("}station") or _sta_el.tag == "station":
                        net_code = _sta_el.get("publicID", "").split(".")[0] or "IA"
                        sta_code = _sta_el.get("code") or _sta_el.get("name") or ""
                        if sta_code:
                            net_sta_seen.add((net_code, sta_code))
        except Exception:
            pass

    # Channel per station: priority (1) a Z channel confirmed live from discovery,
    # (2) default_channel from the meta config (station_channels), (3) fallback "BHZ".
    # Hardcoding "BHZ" for every station is wrong — many IA sensors only have short-period (SHZ).
    # Band priority matches seiswork.utils.channels.BAND_PRIORITY (HH > BH > EH > SH) —
    # this file stays stdlib-only (installed standalone on the SeisComP host), so the
    # order is duplicated here rather than imported.
    live_channel = {}
    if streams:
        _z_priority = {"HHZ": 0, "BHZ": 1, "EHZ": 2, "SHZ": 3}
        for s in streams:
            if not s.get("cha", "").endswith("Z"):
                continue
            key = (s["net"], s["sta"])
            cur = live_channel.get(key)
            rank = _z_priority.get(s["cha"], 9)
            if cur is None or rank < _z_priority.get(cur, 9):
                live_channel[key] = s["cha"]

    key_dir = root / "etc" / "key"
    written_keys = []
    for net_code, sta_code in net_sta_seen:
        channel = (live_channel.get((net_code, sta_code))
                   or station_channels.get(sta_code)
                   or "BHZ")
        key_file = key_dir / f"station_{net_code}_{sta_code}"
        key_file.write_text(
            f"# Auto-generated by SeisWork Agent — {datetime.utcnow().isoformat()}\n"
            f"global:{channel}\nscautopick:default\nseedlink:bmkg\n"
        )
        written_keys.append(str(key_file))
    steps.append({"step": "bindings", "ok": True, "count": len(written_keys)})

    # 4. Update SeisComP config
    rc, out, err = _run_seiscomp("update-config", timeout=30)
    steps.append({"step": "update_config", "ok": rc == 0, "rc": rc})

    # 5. Restart seedlink + scautopick
    restart_mods = data.get("restart_modules", ["seedlink", "scautopick"])
    restart_results = {}
    for mod in restart_mods:
        rc2, _, _ = _run_seiscomp("restart", mod, timeout=20)
        restart_results[mod] = rc2 == 0
    steps.append({"step": "restart", "ok": all(restart_results.values()), "modules": restart_results})

    # Minimal success: the bindings have been written (local setup done).
    # inventory_sync & update_config butuh scmaster running — optional.
    bindings_ok = any(s.get("step") == "bindings" and s.get("ok") for s in steps)
    inv_ok = any(s.get("step") in ("inventory_sync", "fdsnxml_convert")
                  and s.get("ok") and not s.get("skipped") for s in steps)
    overall_ok = bindings_ok and len(net_sta_seen) > 0
    return jsonify({
        "ok"           : overall_ok,
        "steps"        : steps,
        "n_streams"    : len(streams),
        "n_stations"   : len(net_sta_seen),
        "inventory_ok" : inv_ok,
        "note"         : "" if inv_ok else "Bindings written. Start SeisComP then sync again to apply the inventory to the database.",
    })


# ── Endpoint: Push Inventory ────────────────────────────────────────────────────
@app.route("/push/inventory", methods=["POST"])
@require_auth
def push_inventory():
    root = _seiscomp_root()
    if not root:
        return jsonify({"error": "SeisComP not found"}), 500

    data = request.get_json(force=True) or {}
    filename = data.get("filename", "seiswork_inventory.xml")
    content = data.get("content", "")

    if not content:
        return jsonify({"error": "empty content"}), 400

    inv_dir = root / "etc" / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    inv_file = inv_dir / filename

    inv_file.write_text(content, encoding="utf-8")

    # Jalankan scinv sync
    rc, out, err = _run_seiscomp("exec", "scinv", "sync", "--filebase",
                                  str(inv_dir), timeout=60)
    return jsonify({
        "ok": rc == 0,
        "file": str(inv_file),
        "rc": rc,
        "stdout": out[:500],
        "stderr": err[:500],
    })


# ── Endpoint: Push Bindings ─────────────────────────────────────────────────────
@app.route("/push/bindings", methods=["POST"])
@require_auth
def push_bindings():
    root = _seiscomp_root()
    if not root:
        return jsonify({"error": "SeisComP not found"}), 500

    data = request.get_json(force=True) or {}
    stations = data.get("stations", [])  # [{net, sta, seedlink_profile, scautopick_profile}]

    key_dir = root / "etc" / "key"
    written = []
    for s in stations:
        net = s.get("net", "IA")
        sta = s.get("sta", "")
        channel = s.get("channel") or s.get("default_channel") or "BHZ"
        sl_profile = s.get("seedlink_profile", "bmkg")
        pick_profile = s.get("scautopick_profile", "default")
        if not sta:
            continue
        key_file = key_dir / f"station_{net}_{sta}"
        key_file.write_text(
            f"# Binding: {net}.{sta} — generated by SeisWork Agent\n"
            f"global:{channel}\n"
            f"scautopick:{pick_profile}\n"
            f"seedlink:{sl_profile}\n"
        )
        written.append(str(key_file))

    # update-config
    rc, out, err = _run_seiscomp("update-config", timeout=30)
    return jsonify({
        "ok": rc == 0,
        "written": written,
        "rc": rc,
        "stdout": out[:300],
    })


# ── Endpoint: Push Config (processing params) ───────────────────────────────────
@app.route("/push/config", methods=["POST"])
@require_auth
def push_config():
    root = _seiscomp_root()
    if not root:
        return jsonify({"error": "SeisComP not found"}), 500

    data = request.get_json(force=True) or {}
    # Misalnya: {"scautopick.staSta": 0.5, "scautopick.ltaLen": 10.0}
    module = data.get("module", "scautopick")
    params = data.get("params", {})

    cfg_file = root / "etc" / f"{module}.cfg"
    lines = []
    if cfg_file.exists():
        lines = cfg_file.read_text().splitlines()

    for key, val in params.items():
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(key + " ") or line.strip().startswith(key + "="):
                lines[i] = f"{key} = {val}"
                found = True
                break
        if not found:
            lines.append(f"{key} = {val}")

    cfg_file.write_text("\n".join(lines) + "\n")
    return jsonify({"ok": True, "module": module, "params": params})


# ── Endpoint: Restart Module ────────────────────────────────────────────────────
@app.route("/push/restart", methods=["POST"])
@require_auth
def push_restart():
    data = request.get_json(force=True) or {}
    modules_to_restart = data.get("modules", ["seedlink", "scautopick"])

    results = {}
    for mod in modules_to_restart:
        rc, out, err = _run_seiscomp("restart", mod, timeout=20)
        results[mod] = {"rc": rc, "ok": rc == 0, "out": out[:200]}

    return jsonify({"ok": all(v["rc"] == 0 for v in results.values()), "results": results})


# ── Endpoint: Start / Stop module ───────────────────────────────────────────────
@app.route("/push/start", methods=["POST"])
@require_auth
def push_start():
    data = request.get_json(force=True) or {}
    modules_to_start = data.get("modules", [])
    results = {}
    for mod in modules_to_start:
        rc, out, err = _run_seiscomp("start", mod, timeout=20)
        results[mod] = {"rc": rc, "ok": rc == 0}
    return jsonify({"ok": True, "results": results})


@app.route("/push/stop", methods=["POST"])
@require_auth
def push_stop():
    data = request.get_json(force=True) or {}
    modules_to_stop = data.get("modules", [])
    results = {}
    for mod in modules_to_stop:
        rc, out, err = _run_seiscomp("stop", mod, timeout=20)
        results[mod] = {"rc": rc, "ok": rc == 0}
    return jsonify({"ok": True, "results": results})


# ── Endpoint: Dispatch SCML (remote inject into scevent) ──────────────────────
# A SeisWork on ANOTHER host / public IP can push its OWN located events (origins
# + arrivals + picks, or picks-only) into THIS SeisComP's messaging via
# scdispatch — so events land in scevent under SeisWork's own agencyID (set in
# the SCML creationInfo). This is the remote counterpart of app.py's local
# api_inject_seiscomp: the SC binary runs HERE, next to SeisComP, so seiswork
# never needs SeisComP installed on its side — only reachability to this port +
# the token.
@app.route("/dispatch", methods=["POST"])
@require_auth
def dispatch_scml():
    root = _seiscomp_root()
    if not root:
        return jsonify({"ok": False, "error": "SeisComP not found on this host"}), 500
    data = request.get_json(force=True) or {}
    scml = data.get("scml", "")
    if not (scml or "").strip():
        return jsonify({"ok": False, "error": "scml (EventParameters XML) required"}), 400
    # add (new/merge) | update (overwrite) | remove
    operation = (data.get("operation") or "add").strip()
    if operation not in ("add", "update", "remove"):
        return jsonify({"ok": False, "error": f"bad operation '{operation}'"}), 400
    # messaging target: host/queue (SeisComP 8 default queue is 'production').
    messaging = (data.get("messaging") or "localhost/production").strip()

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(scml)
        path = tf.name
    try:
        rc, out, err = _run_seiscomp(
            "exec", "scdispatch", "-O", operation, "-i", path,
            "-H", messaging, timeout=90)
        # scdispatch reports per-object errors in stderr; surface the tail.
        n_err = (out + err).count("errors occured") and \
                (out + err).split("errors occured")[0].strip().split()[-1]
        return jsonify({
            "ok": rc == 0,
            "returncode": rc,
            "operation": operation,
            "messaging": messaging,
            "stdout": out[-1500:],
            "stderr": err[-1500:],
        }), (200 if rc == 0 else 500)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass


# ── Endpoint: Register SeisWork URL (for the pick-push bridge) ─────────────────
@app.route("/register", methods=["POST"])
@require_auth
def register():
    global _seiswork_url, _seiswork_token
    data = request.get_json(force=True) or {}
    url = (data.get("seiswork_url") or "").strip().rstrip("/")
    token = (data.get("seiswork_token") or "").strip()

    if not url:
        return jsonify({"error": "seiswork_url wajib diisi"}), 400

    _seiswork_url = url
    _seiswork_token = token

    cfg = _load_config()
    cfg["seiswork_url"] = url
    cfg["seiswork_token"] = token
    _save_config(cfg)

    _start_bridge()
    return jsonify({"ok": True, "seiswork_url": url, "bridge": "starting"})


# ── Bridge Thread: Subscribe SeisComP message bus → POST picks ke SeisWork ─────
def _bridge_loop():
    global _bridge_running
    _bridge_running = True

    try:
        import requests as req_lib
    except ImportError:
        print("[BRIDGE] requests not available — the bridge cannot run")
        _bridge_running = False
        return

    # Try importing seiscomp.client (only available in the SeisComP env, and it can fail
    # with something other than ImportError — AttributeError/SystemError from a NumPy ABI
    # mismatch between the system python that compiled _core and this agent's conda env).
    try:
        import seiscomp.client
        import seiscomp.datamodel
        _bridge_seiscomp(req_lib)
    except Exception as e:
        # Fallback: poll the MySQL database when seiscomp.client is unavailable/fails to load
        print(f"[BRIDGE] seiscomp.client failed to load ({e}) — using DB polling mode")
        _bridge_db_poll(req_lib)


def _bridge_seiscomp(req_lib):
    """Bridge via the SeisComP message bus (available only inside the SeisComP env)."""
    global _bridge_running
    import seiscomp.client as sc_client
    import seiscomp.datamodel as sc_dm

    class BridgeApp(sc_client.Application):
        def __init__(self):
            super().__init__(1, ["seiswork-bridge"])
            self.setMessagingEnabled(True)
            self.addMessagingSubscription("PICK")

        def handleMessage(self, msg):
            if not _seiswork_url or not _bridge_running:
                return
            for obj in msg:
                if isinstance(obj, sc_dm.Pick):
                    payload = {
                        "net": obj.waveformID().networkCode(),
                        "sta": obj.waveformID().stationCode(),
                        "loc": obj.waveformID().locationCode(),
                        "cha": obj.waveformID().channelCode(),
                        "time": str(obj.time().value()),
                        "phase": obj.phaseHint().code() if obj.phaseHint() else "P",
                        "source": "scautopick",
                    }
                    try:
                        req_lib.post(
                            f"{_seiswork_url}/api/online/trigger",
                            json=payload,
                            headers={"Authorization": f"Bearer {_seiswork_token}"},
                            timeout=3,
                        )
                    except Exception:
                        pass

    bridge_app = BridgeApp()
    bridge_app.exec()
    _bridge_running = False


def _bridge_db_poll(req_lib):
    """Fallback: poll the Pick table in the SeisComP database every 2 seconds."""
    global _bridge_running

    try:
        import pymysql
    except ImportError:
        print("[BRIDGE] pymysql not available — the DB-polling bridge cannot run")
        _bridge_running = False
        return

    db_cfg = _load_config().get("db", {})
    host = db_cfg.get("host", "localhost")
    port = db_cfg.get("port", 3306)
    user = db_cfg.get("user", "sysop")
    password = db_cfg.get("password", "sysop")
    dbname = db_cfg.get("name", "seiscomp")

    # _last_modified uses MySQL's current_timestamp() — that is the server's LOCAL time
    # (WIB), not UTC. time_value (the pick's own time) stays UTC, untouched.
    last_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    while _bridge_running and _seiswork_url:
        try:
            conn = pymysql.connect(host=host, port=port, user=user,
                                   password=password, db=dbname, connect_timeout=3)
            with conn.cursor() as cur:
                # Skema SeisComP: kolom prefix waveformID_*, timestamp insert ada di
                # _last_modified (there is no "created" column in the Pick table).
                cur.execute("""
                    SELECT waveformID_networkCode, waveformID_stationCode,
                           waveformID_locationCode, waveformID_channelCode,
                           time_value, phaseHint_code
                    FROM Pick
                    WHERE _last_modified > %s
                    ORDER BY _last_modified DESC LIMIT 20
                """, (last_time,))
                rows = cur.fetchall()
            conn.close()

            for row in rows:
                net, sta, loc, cha, ptime, phase = row
                payload = {
                    "net": net, "sta": sta, "loc": loc, "cha": cha,
                    "time": str(ptime), "phase": phase or "P",
                    "source": "db_poll",
                }
                try:
                    req_lib.post(
                        f"{_seiswork_url}/api/online/trigger",
                        json=payload,
                        headers={"Authorization": f"Bearer {_seiswork_token}"},
                        timeout=3,
                    )
                except Exception:
                    pass

            if rows:
                last_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            print(f"[BRIDGE-DB] error: {e}")

        time.sleep(2)

    _bridge_running = False


def _start_bridge():
    global _bridge_thread, _bridge_running
    if _bridge_thread and _bridge_thread.is_alive():
        return
    _bridge_running = True
    _bridge_thread = threading.Thread(target=_bridge_loop, daemon=True)
    _bridge_thread.start()
    print("[BRIDGE] started")


# ── CLI ────────────────────────────────────────────────────────────────────────
def cmd_init():
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cfg = {"token_hash": token_hash, "port": DEFAULT_PORT}
    _save_config(cfg)
    print("=" * 60)
    print("SeisWork Agent — Token Generated")
    print("=" * 60)
    print(f"Token (copy it into the SeisWork GUI, it cannot be shown again):")
    print(f"\n  {token}\n")
    print(f"Config saved to: {CONF_FILE}")
    print("=" * 60)


def cmd_start(port: int):
    global _config, _seiswork_url, _seiswork_token
    _config = _load_config()
    if not _config.get("token_hash"):
        print("[ERROR] Token not generated yet. Run: python agent.py --init")
        sys.exit(1)

    _seiswork_url = _config.get("seiswork_url")
    _seiswork_token = _config.get("seiswork_token")

    if _seiswork_url:
        _start_bridge()

    port = _config.get("port", port)
    print(f"[SeisWork Agent v{AGENT_VERSION}] listening on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


def cmd_status():
    cfg = _load_config()
    port = cfg.get("port", DEFAULT_PORT)
    try:
        import urllib.request
        import urllib.error
        # Cannot test the token without knowing its plaintext value; just check the port is open
        req = urllib.request.Request(f"http://localhost:{port}/status")
        req.add_header("Authorization", "Bearer __status_check__")
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:
        if "401" in str(e):
            print(f"[OK] Agent running on port {port} (auth enabled)")
        elif "Connection refused" in str(e):
            print(f"[STOPPED] Agent not running on port {port}")
        else:
            print(f"[?] {e}")


def main():
    parser = argparse.ArgumentParser(description="SeisWork Agent — by HakimBMKG")
    parser.add_argument("--init", action="store_true", help="Generate a new token")
    parser.add_argument("--start", action="store_true", help="Run the agent")
    parser.add_argument("--status", action="store_true", help="Check the agent status")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.init:
        cmd_init()
    elif args.start:
        cmd_start(args.port)
    elif args.status:
        cmd_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
