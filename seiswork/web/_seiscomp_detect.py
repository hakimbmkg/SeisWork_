"""
SeisComP auto-detect — by HakimBMKG

Finds a local SeisComP install (root, SDS archive, seedlink port) and
checks whether its seedlink is actually reachable, without changing
anything on the system (read-only).
"""

import os
import re
import socket
from datetime import datetime
from pathlib import Path

import yaml

_DEFAULT_SEEDLINK_PORT = 18000
_CANDIDATE_ROOTS = [
    Path.home() / "seiscomp",
    Path("/opt/seiscomp"),
    Path("/usr/local/seiscomp"),
]


def _path_exists(p: Path) -> bool:
    """Like p.exists() but catches PermissionError.
    Python 3.10 in a systemd user service doesn't load supplementary groups,
    so stat() on a path owned by another user can raise PermissionError
    instead of returning False."""
    try:
        p.stat()
        return True
    except (OSError, PermissionError):
        return False


def _root_from_config(cfg_file: Path) -> Path | None:
    """seiscomp_bin is already used by locsat.py (locate.locsat.seiscomp_bin) —
    reuse the same key as the most accurate root hint when present."""
    if not cfg_file.exists():
        return None
    try:
        cfg = yaml.safe_load(cfg_file.read_text()) or {}
    except Exception:
        return None
    sc_bin = (((cfg.get("locate") or {}).get("locsat") or {}).get("seiscomp_bin") or "").strip()
    if not sc_bin:
        return None
    # .../seiscomp/bin/seiscomp -> root = .../seiscomp
    root = Path(sc_bin).resolve().parent.parent
    # If the path is configured explicitly, trust its root even when the binary
    # cannot be stat-ed (the service process may lack the required group).
    return root if (_path_exists(root / "bin" / "seiscomp") or _path_exists(root / "var" / "lib" / "archive")) else None


def _find_root(cfg_file: Path) -> Path | None:
    env_root = os.environ.get("SEISCOMP_ROOT", "").strip()
    if env_root and _path_exists(Path(env_root) / "bin" / "seiscomp"):
        return Path(env_root)

    root = _root_from_config(cfg_file)
    if root:
        return root

    for cand in _CANDIDATE_ROOTS:
        if _path_exists(cand / "bin" / "seiscomp"):
            return cand
    return None


def _read_seedlink_port(root: Path) -> int:
    for rel in ("etc/seedlink.cfg", "etc/defaults/seedlink.cfg"):
        f = root / rel
        if not f.exists():
            continue
        m = re.search(r"^\s*port\s*=\s*(\d+)", f.read_text(), re.MULTILINE)
        if m:
            return int(m.group(1))
    return _DEFAULT_SEEDLINK_PORT


def check_seedlink(host: str, port: int, timeout: float = 3.0) -> dict:
    """Coba HELLO ke port seedlink. Tidak mengubah apa pun di sisi server."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"HELLO\r\n")
            s.settimeout(timeout)
            banner = s.recv(256).decode(errors="replace").strip()
        return {"connected": True, "server_info": banner}
    except Exception as exc:
        return {"connected": False, "server_info": "", "error": str(exc)}


def detect_seiscomp(cfg_file: Path, host: str = "localhost") -> dict:
    """Deteksi instalasi SeisComP lokal. Read-only, aman dipanggil berulang."""
    root = _find_root(cfg_file)
    result = {
        "found"        : root is not None,
        "root"         : str(root) if root else None,
        "sds_dir"      : None,
        "seedlink_host": host,
        "seedlink_port": _DEFAULT_SEEDLINK_PORT,
        "status"       : "not_found",
        "server_info"  : "",
        "checked_at"   : datetime.now().isoformat(timespec="seconds"),
    }
    if root is None:
        return result

    sds_dir = root / "var" / "lib" / "archive"
    result["sds_dir"] = str(sds_dir) if _path_exists(sds_dir) else None
    result["seedlink_port"] = _read_seedlink_port(root)

    check = check_seedlink(host, result["seedlink_port"])
    if check["connected"]:
        result["status"] = "connected"
        result["server_info"] = check["server_info"]
    else:
        result["status"] = "installed_not_running"
    return result
