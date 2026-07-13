#!/usr/bin/env python3
"""
SeisWork remote client — by HakimBMKG

Thin REST client to drive a remote SeisWork server: submit pipeline jobs,
poll status/logs, and auto-sync finished results to a local mirror.
Only used when a server URL is configured — SeisWork still runs standalone.

IDs are server-assigned; a local registry (~/.seiswork/registry.json) maps
each job ID to its server and local mirror path.
Auth: optional bearer token; none sent when the server runs open.
"""

import json
import os
import time
from pathlib import Path

import requests


SEISWORK_HOME = Path(os.environ.get("SEISWORK_HOME", Path.home() / ".seiswork"))
REGISTRY_FILE = SEISWORK_HOME / "registry.json"

# Artifacts worth mirroring when a job finishes (catalog CSVs, configs, logs).
_SYNC_SUFFIXES = (".csv", ".yaml", ".log", ".json", ".txt")


class SeisWorkClient:
    """REST client for a remote SeisWork server."""

    def __init__(self, server_url: str, token: str = "",
                 sync_dir: str = "", timeout: float = 30.0):
        if not server_url:
            raise ValueError("server_url is required")
        self.base    = server_url.rstrip("/")
        self.token   = (token or "").strip()
        self.timeout = timeout
        self.sync_dir = Path(sync_dir) if sync_dir else (SEISWORK_HOME / "remote")
        SEISWORK_HOME.mkdir(parents=True, exist_ok=True)

    # ── low-level HTTP ────────────────────────────────────────────────────────
    def _headers(self, token_override: str = "") -> dict:
        h = {"Accept": "application/json"}
        tok = (token_override or self.token)
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def _url(self, path: str) -> str:
        return f"{self.base}/{path.lstrip('/')}"

    def _get(self, path: str, token_override: str = "", **kw):
        r = requests.get(self._url(path), headers=self._headers(token_override),
                         timeout=self.timeout, **kw)
        return r

    def _post(self, path: str, payload: dict):
        r = requests.post(self._url(path), headers=self._headers(),
                          json=payload, timeout=self.timeout)
        return r

    @staticmethod
    def _json(r):
        r.raise_for_status()
        return r.json()

    # ── discovery / identity ──────────────────────────────────────────────────
    def health(self) -> dict:
        """Server identity + whether a token is required. Public (no auth)."""
        return self._json(self._get("/api/health"))

    def ping(self) -> bool:
        try:
            return bool(self.health().get("ok"))
        except Exception:
            return False

    def server_info(self) -> dict:
        """Connection info: server_id, urls, auth_required, token (localhost /
        authenticated callers only)."""
        return self._json(self._get("/api/server-info"))

    def set_token(self, token: str = "", generate: bool = False) -> dict:
        """Set/generate the server's bearer token at runtime (operator only).
        On success, adopt the new token for this client too."""
        res = self._json(self._post("/api/server-info/token",
                                    {"token": token, "generate": generate}))
        self.token = res.get("token", "") or ""
        return res

    # ── per-project federation tokens (scope a subscriber to one cfg_id) ───────
    def get_sync_token(self, cfg_id: str) -> str:
        """Fetch one project's sync token — only works from the server machine,
        or if this client's token already matches it."""
        res = self._json(self._get(f"/api/configs/{cfg_id}/sync-token"))
        return res.get("sync_token", "")

    def regen_sync_token(self, cfg_id: str) -> str:
        """Regenerate one project's sync token, revoking any subscriber using
        the old one. Same access rule as get_sync_token."""
        res = self._json(self._post(f"/api/configs/{cfg_id}/sync-token", {}))
        return res.get("sync_token", "")

    # ── configs ───────────────────────────────────────────────────────────────
    def list_configs(self) -> list:
        return self._json(self._get("/api/configs"))

    # ── picking (separate endpoint from the pipeline steps) ────────────────────
    def run_pick(self, cfg_id: str, method: str = "phasenet",
                 params: dict | None = None) -> dict:
        res = self._json(self._post("/api/picking/run", {
            "cfg_id": cfg_id, "method": method, "params": params or {},
        }))
        jid = res.get("id")
        if jid:
            self._register_job(jid, "pick", method, cfg_id)
        return res

    # ── jobs ──────────────────────────────────────────────────────────────────
    def run(self, cfg_id: str, step: str, method: str,
            params: dict | None = None, input_file: str = "") -> dict:
        """Submit a pipeline job; returns {id, state, step, method}. The job_id
        is server-assigned and recorded in the local registry."""
        res = self._json(self._post("/api/pipeline/run", {
            "cfg_id": cfg_id, "step": step, "method": method,
            "params": params or {}, "input_file": input_file,
        }))
        jid = res.get("id")
        if jid:
            self._register_job(jid, step, method, cfg_id)
        return res

    def jobs(self, cfg_id: str = "") -> list:
        path = "/api/pipeline/jobs"
        if cfg_id:
            path += f"?cfg_id={cfg_id}"
        return self._json(self._get(path))

    def job_status(self, job_id: str) -> dict:
        for j in self.jobs():
            if j.get("id") == job_id:
                return j
        return {}

    def job_log(self, job_id: str, offset: int = 0) -> dict:
        return self._json(self._get(
            f"/api/pipeline/jobs/{job_id}/log", params={"offset": offset}))

    def job_files(self, job_id: str) -> list:
        return self._json(self._get(f"/api/pipeline/jobs/{job_id}/files"))

    def stop_job(self, job_id: str) -> dict:
        r = requests.delete(self._url(f"/api/pipeline/jobs/{job_id}"),
                            headers=self._headers(), timeout=self.timeout)
        return self._json(r)

    # ── artifact download / auto-sync ─────────────────────────────────────────
    def download_file(self, job_id: str, rel_name: str, dest: Path) -> Path:
        r = self._get(f"/api/pipeline/jobs/{job_id}/download",
                      params={"name": rel_name}, stream=True)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
        return dest

    def sync_job(self, job_id: str) -> list:
        """Mirror a finished job's artifacts into ``sync_dir/<job_id>/``.
        Returns the list of local file paths written."""
        local_dir = self.sync_dir / job_id
        written = []
        for f in self.job_files(job_id):
            name = f.get("name", "")
            if not name or os.path.splitext(name)[1] not in _SYNC_SUFFIXES:
                continue
            # Reconstruct the path relative to the job dir from the server path.
            rel = f.get("rel") or name
            try:
                dest = local_dir / rel
                self.download_file(job_id, rel, dest)
                written.append(str(dest))
            except Exception:
                continue
        self._register_job(job_id, mirror=str(local_dir))
        return written

    def wait(self, job_id: str, poll: float = 3.0, timeout: float = 0.0,
             on_log=None, auto_sync: bool = True) -> dict:
        """Block until the job reaches a terminal state. Streams new log lines to
        ``on_log(text)`` if given, and auto-syncs artifacts on completion."""
        t0 = time.time()
        offset = 0
        state = "running"
        terminal = {"done", "error", "stopped"}
        while True:
            try:
                lg = self.job_log(job_id, offset)
                for line in lg.get("lines", []):
                    if on_log:
                        on_log(line + "\n")
                offset = lg.get("offset", offset)
                state  = lg.get("state", state)
            except Exception:
                pass
            if state in terminal:
                st = self.job_status(job_id) or {"state": state}
                st["state"] = state
                if auto_sync and state == "done":
                    try:
                        st["_synced"] = self.sync_job(job_id)
                    except Exception:
                        pass
                return st
            if timeout and (time.time() - t0) > timeout:
                return {"state": "timeout"}
            time.sleep(poll)

    # ── local registry (ID synchronisation) ───────────────────────────────────
    def _load_registry(self) -> dict:
        if REGISTRY_FILE.exists():
            try:
                return json.loads(REGISTRY_FILE.read_text())
            except Exception:
                return {}
        return {}

    def _save_registry(self, reg: dict):
        SEISWORK_HOME.mkdir(parents=True, exist_ok=True)
        REGISTRY_FILE.write_text(json.dumps(reg, indent=2))

    def _server_id(self) -> str:
        try:
            return self.health().get("server_id", self.base)
        except Exception:
            return self.base

    def _register_job(self, job_id: str, step: str = "", method: str = "",
                      cfg_id: str = "", mirror: str = ""):
        reg = self._load_registry()
        jobs = reg.setdefault("jobs", {})
        rec = jobs.get(job_id, {})
        rec.update({k: v for k, v in {
            "server": self.base, "server_id": self._server_id(),
            "step": step, "method": method, "cfg_id": cfg_id, "mirror": mirror,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }.items() if v})
        jobs[job_id] = rec
        self._save_registry(reg)

    def registered_jobs(self) -> dict:
        return self._load_registry().get("jobs", {})

    # ── Bulk sync (manifest-based delta pull) ─────────────────────────────────
    # Scoped to one project: the sync token only works for its own cfg_id.
    def sync_manifest(self, cfg_id: str, sync_token: str = "") -> dict:
        """Fetch the server's sync manifest for one project: jobs + checksums + sizes."""
        if not cfg_id:
            raise ValueError("cfg_id is required")
        return self._json(self._get("/api/sync/manifest",
                                    token_override=sync_token,
                                    params={"cfg_id": cfg_id}))

    def pull_bundle(self, job_id: str, cfg_id: str, kind: str = "pipeline",
                    sync_token: str = "", dest_dir: "Path | None" = None) -> "Path":
        """Download and extract the job's ZIP bundle to dest_dir
        (default: sync_dir/<kind>/<job_id>/). Returns the local dir."""
        import tempfile
        import zipfile as _zf
        if not cfg_id:
            raise ValueError("cfg_id is required")
        if dest_dir is None:
            dest_dir = self.sync_dir / kind / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        r = self._get(f"/api/sync/jobs/{job_id}/bundle",
                      token_override=sync_token,
                      params={"kind": kind, "cfg_id": cfg_id}, stream=True)
        r.raise_for_status()
        # Stream to a temp file so large ZIPs never sit fully in memory.
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    tmp.write(chunk)
            tmp_path = tmp.name
        try:
            with _zf.ZipFile(tmp_path) as zf:
                zf.extractall(str(dest_dir))
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        self._register_job(job_id, step=kind, mirror=str(dest_dir))
        return dest_dir

    def sync_pull(self, cfg_id: str, job_ids: "list | None" = None, dry_run: bool = False,
                  force: bool = False,
                  kinds: "list | None" = None, sync_token: str = "") -> list:
        """Pull new/updated jobs for one project. Compares server checksums
        against local .sync_checksum markers and downloads only what changed.
        Returns dicts: {id, kind, action (pulled|skip|error), path|reason|error}."""
        manifest = self.sync_manifest(cfg_id, sync_token=sync_token)
        results = []
        for job in manifest.get("jobs", []):
            jid        = job["id"]
            kind       = job["kind"]
            server_sum = job.get("checksum", "")
            if job_ids and jid not in job_ids:
                continue
            if kinds and kind not in kinds:
                continue
            local_dir   = self.sync_dir / kind / jid
            local_check = local_dir / ".sync_checksum"
            if not force and local_check.exists():
                try:
                    if local_check.read_text().strip() == server_sum:
                        results.append({"id": jid, "kind": kind,
                                        "action": "skip", "reason": "up-to-date"})
                        continue
                except Exception:
                    pass
            if dry_run:
                results.append({
                    "id": jid, "kind": kind, "action": "pull",
                    "state"     : job.get("state", ""),
                    "n_files"   : job.get("n_files", 0),
                    "size_bytes": job.get("size_bytes", 0),
                })
                continue
            try:
                path = self.pull_bundle(jid, cfg_id, kind=kind, sync_token=sync_token)
                if server_sum:
                    (path / ".sync_checksum").write_text(server_sum)
                results.append({"id": jid, "kind": kind,
                                "action": "pulled", "path": str(path)})
            except Exception as exc:
                results.append({"id": jid, "kind": kind,
                                "action": "error", "error": str(exc)})
        return results


# ── Persistent client↔server connection (so the client "remembers" the server) ──
CONNECTION_FILE = SEISWORK_HOME / "remote.json"


def save_connection(server_url: str, token: str = "", sync_dir: str = "") -> dict:
    """Save the server connection so later calls need no --server/--token."""
    SEISWORK_HOME.mkdir(parents=True, exist_ok=True)
    conn = {"server_url": server_url.rstrip("/"), "token": token or "",
            "sync_dir": sync_dir or "", "saved": time.strftime("%Y-%m-%dT%H:%M:%S")}
    CONNECTION_FILE.write_text(json.dumps(conn, indent=2))
    return conn


def load_connection() -> dict:
    try:
        if CONNECTION_FILE.exists():
            return json.loads(CONNECTION_FILE.read_text())
    except Exception:
        pass
    return {}


def clear_connection() -> None:
    try:
        if CONNECTION_FILE.exists():
            CONNECTION_FILE.unlink()
    except Exception:
        pass


def client_from_config(cfg: dict, server_url: str = "", token: str = "",
                       base_dir: str = "") -> "SeisWorkClient":
    """Build a client. Precedence: explicit args > env > saved connection
    (~/.seiswork/remote.json) > config ``federation``."""
    fed  = (cfg or {}).get("federation", {}) or {}
    conn = load_connection()
    url = (server_url or os.environ.get("SEISWORK_SERVER")
           or conn.get("server_url") or fed.get("server_url", "")).strip()
    tok = (token or os.environ.get("SEISWORK_TOKEN")
           or conn.get("token") or fed.get("token", "")).strip()
    sync = conn.get("sync_dir") or fed.get("sync_dir", "work/remote")
    if base_dir and sync and not os.path.isabs(sync):
        sync = os.path.join(base_dir, sync)
    return SeisWorkClient(url, tok, sync_dir=sync)
