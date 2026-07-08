#!/usr/bin/env bash
# ==============================================================================
# SeisWork — update.sh   (by HakimBMKG)
#
# SINGLE script for:
#   1) Updating the code from GitHub (git pull, safe against local changes)
#   2) Re-building binaries / re-installing dependencies ONLY when those actually changed
#   3) Restarting the server (systemd 'seiswork' if present, else relying on auto-reload)
#   4) Managing the single VERSION in seiswork/__init__.py (+ syncing pyproject.toml)
#
# Usage:
#   bash update.sh                 Full update (pull + rebuild as needed + restart)
#   bash update.sh --check         Show what updates are available (without applying)
#   bash update.sh --version       Print the current version
#   bash update.sh --bump patch    Bump the version: patch|minor|major (suffix preserved)
#   bash update.sh --set 0.0.2(BETA)   Set the version manually
# ==============================================================================
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; cd "$DIR"
INIT="seiswork/__init__.py"
PYPROJECT="pyproject.toml"
CHANGELOG="$DIR/CHANGELOG.json"

if [ -t 1 ]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; B=$'\033[36m'; N=$'\033[0m'
else R=; G=; Y=; B=; N=; fi
ok(){   printf "%s✓%s %s\n" "$G" "$N" "$*"; }
inf(){  printf "%s•%s %s\n" "$B" "$N" "$*"; }
warn(){ printf "%s⚠%s %s\n" "$Y" "$N" "$*"; }
err(){  printf "%s✗%s %s\n" "$R" "$N" "$*" >&2; }

# portable in-place sed (GNU/Linux vs BSD/macOS)
sedi(){ if sed --version >/dev/null 2>&1; then sed -i "$@"; else local e="$1"; shift; sed -i '' "$e" "$@"; fi; }

# ── Version helpers ─────────────────────────────────────────────────────────
ver_get(){ grep -oE '__version__[[:space:]]*=[[:space:]]*"[^"]+"' "$INIT" | sed -E 's/.*"([^"]+)".*/\1/'; }
ver_core(){ printf '%s' "$1" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+'; }
ver_pep440(){  # display version → a valid PEP 440 form for pip/build
    local v="$1" core; core="$(ver_core "$v")"; [ -z "$core" ] && core="0.0.0"
    case "$v" in
        *BETA*|*beta*)   printf '%sb0'  "$core" ;;
        *ALPHA*|*alpha*) printf '%sa0'  "$core" ;;
        *RC*|*rc*)       printf '%src0' "$core" ;;
        *)               printf '%s'    "$core" ;;
    esac
}
ver_set(){  # write the new version to __init__.py + pyproject.toml
    local nv="$1" cur; cur="$(ver_get)"
    sedi "s|__version__ = \"${cur}\"|__version__ = \"${nv}\"|" "$INIT"
    sedi -E "s|^version = \"[^\"]*\"|version = \"$(ver_pep440 "$nv")\"|" "$PYPROJECT"
    ok "version: ${cur} → ${nv}   (pyproject: $(ver_pep440 "$nv"))"
    changelog_stub "$nv"
}

# CHANGELOG.json is the single file to hand-edit when releasing a version:
# ver_set() only stubs a dated, empty "changes" entry here — fill in the
# actual bullet points by editing CHANGELOG.json directly, no code changes
# needed elsewhere. Uses python3 (already a hard requirement of this project)
# for safe JSON read/modify/write instead of ad-hoc sed on JSON.
changelog_stub(){
    local nv="$1"
    python3 - "$CHANGELOG" "$nv" <<'PYEOF' 2>/dev/null || true
import json, sys, datetime, os
path, ver = sys.argv[1], sys.argv[2]
data = {"current": ver, "entries": []}
if os.path.exists(path):
    try:
        data = json.load(open(path))
    except Exception:
        pass
data.setdefault("entries", [])
data["current"] = ver
if not any(e.get("version") == ver for e in data["entries"]):
    data["entries"].insert(0, {
        "version": ver,
        "date": datetime.date.today().isoformat(),
        "changes": [],
    })
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"stubbed", file=sys.stderr)
PYEOF
    if [ -f "$CHANGELOG" ]; then
        inf "CHANGELOG.json: entry for ${nv} ready — edit its \"changes\" list by hand"
    fi
}

# Pretty-print the N most recent CHANGELOG.json entries (dev-facing, terminal only).
changelog_show(){
    local n="${1:-5}"
    [ -f "$CHANGELOG" ] || { warn "CHANGELOG.json not found at $CHANGELOG"; return 1; }
    python3 - "$CHANGELOG" "$n" <<'PYEOF'
import json, sys
path, n = sys.argv[1], int(sys.argv[2])
data = json.load(open(path))
for e in data.get("entries", [])[:n]:
    print(f"\n{e.get('version','?')}  ({e.get('date','?')})")
    changes = e.get("changes") or []
    if not changes:
        print("  (no changes recorded yet — edit CHANGELOG.json)")
    for c in changes:
        print(f"  - {c}")
print()
PYEOF
}
ver_bump(){  # bump major|minor|patch, preserving the suffix (e.g. '(BETA)')
    local part="$1" cur core suf MA MI PA
    cur="$(ver_get)"; core="$(ver_core "$cur")"; suf="${cur#"$core"}"
    IFS=. read -r MA MI PA <<EOF
${core}
EOF
    case "$part" in
        major) MA=$((MA+1)); MI=0; PA=0 ;;
        minor) MI=$((MI+1)); PA=0 ;;
        patch) PA=$((PA+1)) ;;
        *) err "bump requires: major | minor | patch"; exit 1 ;;
    esac
    ver_set "${MA}.${MI}.${PA}${suf}"
}

restart_server(){
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    if systemctl --user list-unit-files 2>/dev/null | grep -q '^seiswork\.service'; then
        if systemctl --user restart seiswork 2>/dev/null; then
            ok "service 'seiswork' restarted"
            return
        fi
        # Restart failed — check for a conda/PATH crash-loop (status=127)
        warn "systemctl restart failed — checking for a crash-loop..."
        if journalctl --user -u seiswork -n 15 --no-pager 2>/dev/null \
                | grep -qiE 'conda.*not found|exec.*not found|status=127|not found.*seiswork'; then
            warn "Detected a 'conda not found' crash-loop — invoking fix_service.sh ..."
            local FIX="$DIR/tools/fix_service.sh"
            if [ -f "$FIX" ]; then
                bash "$FIX" && return
            else
                warn "fix_service.sh not found at $FIX"
                warn "For another VM: bash tools/fix_service.sh --print  (copy-paste onto the VM)"
            fi
        else
            warn "Service failed to restart — check: journalctl --user -u seiswork -n 20"
        fi
        return
    fi
    if pgrep -f "seiswork\.web\.app|seiswork/seiswork\.py" >/dev/null 2>&1; then
        # Server alive with AUTO_RESTART → self-reloads when .py changes; send HUP to be sure
        kill -HUP "$(pgrep -f 'seiswork\.web\.app|seiswork/seiswork\.py' | head -1)" 2>/dev/null \
            && ok "server restarted (HUP)" \
            || inf "Server running — auto-reload will pick up the changes on its own."
    else
        inf "Server not running. Run:  seiswork   (or bash ~/start_seiswork.sh)"
    fi
}

# ── Verify & repair EVERY sector (important for realtime/production) ─────────
# List of engine binaries that MUST exist (core/bin or PATH).
EXPECTED_BINS="REAL NLLoc Grid2Time Vel2Grid Time2EQ PhsAssoc hypoDD ph2dt growclust MatchLocate2 SelectFinal FDTCC velest hypoinverse glass-app"

bin_ok(){ [ -x "core/bin/$1" ] || command -v "$1" >/dev/null 2>&1; }

# Locate the SeisWork python env (best-effort) to test imports.
sw_python(){
    local p
    for p in "$HOME/miniforge3/envs/seiswork/bin/python" \
             "$HOME/miniconda3/envs/seiswork/bin/python" \
             "/opt/miniconda3/envs/seiswork/bin/python" \
             "$DIR/.venv/bin/python" "$HOME/.seiswork-venv/bin/python"; do
        [ -x "$p" ] && { echo "$p"; return; }
    done
    command -v python3 || command -v python
}

# Check every sector, set repair flags (NEED_*). Return a summary to the screen.
verify_sectors(){
    inf "── Checking every SeisWork sector ──"
    # 1) BINARY engine
    local miss="" b
    for b in $EXPECTED_BINS; do bin_ok "$b" || miss="$miss$b "; done
    if [ -n "$miss" ]; then
        warn "  BINARY    : missing/non-executable → ${miss}→ will be rebuilt"
        NEED_COMPILE=1; NEED_DOWNLOAD=1; SEC_BIN="repair"
    else
        ok   "  BINARY    : $(echo $EXPECTED_BINS | wc -w) engines OK"; SEC_BIN="ok"
    fi
    # 2) Bundled GRID & CONFIG
    if [ -d config/nlloc_grids/global ] && [ -f config/inventory.xml ]; then
        ok   "  GRID/CFG  : global IASP91 grid + inventory OK"; SEC_CFG="ok"
    else
        warn "  GRID/CFG  : config/nlloc_grids/global or inventory.xml missing (check 'git pull')"; SEC_CFG="warn"
    fi
    # 3) Core PYTHON + SeisWork modules
    local PY; PY="$(sw_python)"
    if [ -n "$PY" ] && "$PY" -c "import obspy,numpy,pandas,flask,seiswork" >/dev/null 2>&1; then
        ok   "  PYTHON    : core imports (obspy/flask/seiswork) OK"; SEC_PY="ok"
    else
        warn "  PYTHON    : core imports FAILED → dependencies will be reinstalled"; NEED_PIP=1; SEC_PY="repair"
    fi
    # 4) realtime engine modules (picker/associator/locator/grid-gen)
    if [ -n "$PY" ] && "$PY" -c "import seiswork.web._realtime_pipeline, seiswork.modules.associator.real, seiswork.modules.locator.nlloc, seiswork.modules.locator.nlloc_grids" >/dev/null 2>&1; then
        ok   "  REALTIME  : picker/REAL/NLLoc/grid-gen modules OK"; SEC_RT="ok"
    else
        warn "  REALTIME  : realtime modules failed to import (check the error above / step 5)"; SEC_RT="warn"
    fi
    # 4b) PyOcto — OPTIONAL associator (pip, --no-deps, see install.sh step5_python).
    # Not part of environment.yml/requirements.txt, so a plain `git diff` on those
    # files won't catch it — only checked here + the install.sh-changed heuristic
    # below (NEED_PIP) picks it up on update.
    if [ -n "$PY" ] && "$PY" -c "import pyocto" >/dev/null 2>&1; then
        ok   "  ASSOC     : PyOcto associator OK"; SEC_ASSOC="ok"
    else
        warn "  ASSOC     : PyOcto not installed (optional) → will be installed (step 5)"
        NEED_PIP=1; SEC_ASSOC="repair"
    fi
    # 4c) FocoNet — OPTIONAL focal mechanism (pure Python source tree, no pip
    # package, no compile — cloning core/src/FocoNet/ IS the install; see
    # install.sh step2_download/step3_compile comments). Needs pyrocko too
    # (--no-deps, same numpy<2 regression as SKHASH/PyOcto).
    if [ -f "core/src/FocoNet/FocoNet_O/model.py" ]; then
        SEC_FOCONET="ok-src"
    else
        warn "  MECHANISM : FocoNet source not found → will be cloned (step 2)"
        NEED_DOWNLOAD=1; SEC_FOCONET="repair"
    fi
    if [ -n "$PY" ] && "$PY" -c "import pyrocko" >/dev/null 2>&1; then
        [ "$SEC_FOCONET" = "ok-src" ] && { ok "  MECHANISM : FocoNet source + pyrocko OK"; SEC_FOCONET="ok"; } \
            || { NEED_PIP=1; SEC_FOCONET="repair"; }
    else
        warn "  MECHANISM : pyrocko not installed (optional) → will be installed (step 5)"
        NEED_PIP=1; SEC_FOCONET="repair"
    fi
    # 5) REVERSE PROXY (nginx/apache) in front of Flask → upload body size limit.
    #    Common cause of "Upload failed: Invalid response" during a StationXML upload:
    #    nginx default client_max_body_size = 1 MB → the proxy rejects large files
    #    with an HTML 413 before it reaches Flask.
    verify_proxy
}

# Check the reverse proxy + upload body limit. Warn-only (does not change the config).
verify_proxy(){
    local port="${SEISWORK_PORT:-5000}"
    # Find the nginx config that proxies to the SeisWork port.
    local ngx_files
    ngx_files="$(grep -rlsE "proxy_pass[[:space:]].*:${port}\b" /etc/nginx 2>/dev/null)"
    if [ -n "$ngx_files" ]; then
        # Take the largest configured client_max_body_size value (e.g. '256m','1024k').
        local cmb
        cmb="$(grep -rhoiE "client_max_body_size[[:space:]]+[0-9]+[kmg]?" /etc/nginx 2>/dev/null \
               | grep -oiE "[0-9]+[kmg]?" | tail -1)"
        if [ -z "$cmb" ]; then
            warn "  PROXY     : nginx proxies :${port} but client_max_body_size is NOT set"
            warn "              → default 1 MB; large StationXML uploads will 413. Add to server/location:"
            warn "                  client_max_body_size 256m;  proxy_read_timeout 120s;"
            SEC_PROXY="warn"
        else
            # Normalize to an approximate MB value for comparison.
            local num unit mb; num="${cmb%[kKmMgG]}"; unit="${cmb#$num}"
            case "$unit" in [gG]) mb=$((num*1024));; [kK]) mb=$((num/1024));; *) mb="$num";; esac
            if [ "${mb:-0}" -lt 64 ]; then
                warn "  PROXY     : nginx client_max_body_size=${cmb} (~${mb}MB) — likely too small for StationXML"
                warn "              → raise it to 256m if inventory uploads fail (413/Invalid response)"
                SEC_PROXY="warn"
            else
                ok   "  PROXY     : nginx :${port} client_max_body_size=${cmb} OK"; SEC_PROXY="ok"
            fi
        fi
    elif grep -rqsE "ProxyPass[[:space:]].*:${port}\b" /etc/apache2 /etc/httpd 2>/dev/null; then
        warn "  PROXY     : apache proxies :${port} — make sure LimitRequestBody is large enough (or 0=unlimited)"
        SEC_PROXY="warn"
    else
        ok   "  PROXY     : no reverse proxy detected (direct access to Flask)"; SEC_PROXY="ok"
    fi
}

# Check the server is alive after a restart (realtime).
health_check(){
    local url="http://localhost:${SEISWORK_PORT:-5000}/"
    command -v curl >/dev/null 2>&1 || { inf "  (curl not found — skipping the HTTP check)"; return; }
    local code; code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$url" 2>/dev/null || echo 000)"
    if [ "$code" = "200" ]; then ok "  SERVER    : $url → 200 OK"
    else warn "  SERVER    : $url → ${code} (server may still be starting / a different port)"; fi
}

usage(){ cat <<EOF
SeisWork updater — current version: $(ver_get)
  bash update.sh                 Update + CHECK EVERY SECTOR (binary/grid/python/realtime)
                                 then repair anything broken/missing + restart (recommended for realtime)
  bash update.sh --quick         Light update: only act on the changed files
  bash update.sh --check         Check for updates (without applying)
  bash update.sh --version       Show the version
  bash update.sh --bump patch    Bump the version (patch|minor|major)
  bash update.sh --set X.Y.Z     Set the version manually (e.g. 0.0.2(BETA))
  bash update.sh --changelog [N] Show the N most recent CHANGELOG.json entries (default 5)

  Recording a release: --bump/--set stub a dated entry in CHANGELOG.json with
  an empty "changes" list — fill in the bullet points by editing that file
  directly, no code changes needed elsewhere.

  Service repair (direct, no pull):
  bash tools/fix_service.sh          Run on a machine whose service is crash-looping
  bash tools/fix_service.sh --print  Print a ready-to-paste block for another VM (no SSH)
EOF
}

# ── Version / help arguments ────────────────────────────────────────────────
case "${1:-}" in
    -h|--help)  usage; exit 0 ;;
    --version)  ver_get; exit 0 ;;
    --bump)     ver_bump "${2:-patch}"
                inf "Then commit: git add -A && git commit -m \"chore: bump version $(ver_get)\""; exit 0 ;;
    --set)      [ -n "${2:-}" ] || { err "--set requires a version argument"; exit 1; }
                ver_set "$2"
                inf "Then commit: git add -A && git commit -m \"chore: set version $2\""; exit 0 ;;
    --changelog) changelog_show "${2:-5}"; exit 0 ;;
esac
CHECK=false; [ "${1:-}" = "--check" ] && CHECK=true
QUICK=false; [ "${1:-}" = "--quick" ] && QUICK=true   # skip the check-every-sector pass
# Action flags (set by the git diff & verify_sectors)
NEED_PIP=0; NEED_COMPILE=0; NEED_RESTART=0; NEED_DOWNLOAD=0
SEC_BIN="-"; SEC_CFG="-"; SEC_PY="-"; SEC_RT="-"; SEC_ASSOC="-"; SEC_FOCONET="-"; SEC_PROXY="-"

# ── Update from GitHub ─────────────────────────────────────────────────────────
git rev-parse --git-dir >/dev/null 2>&1 || { err "Not a git repo — update manually."; exit 1; }
BR="$(git rev-parse --abbrev-ref HEAD)"
OLD_VER="$(ver_get)"; OLD_HEAD="$(git rev-parse --short HEAD)"
inf "SeisWork ${OLD_VER}  (${OLD_HEAD}, branch ${BR})"

# Run install.sh --step 2/3/5 for whichever NEED_* flags are set. Shared by
# both the "0 new commits" path and the normal pull path below, so a repair
# behaves identically regardless of which one triggered it.
do_repairs(){
    if [ "$NEED_DOWNLOAD" = 1 ]; then
        inf "A binary is missing → ensuring the external engine source (install.sh --step 2)"
        bash install.sh --step 2 || warn "step 2 (download) had an issue — check the output above"
    fi
    if [ "$NEED_COMPILE" = 1 ]; then
        inf "Re-compiling engine binaries → core/bin (install.sh --step 3)"
        bash install.sh --step 3 || warn "step 3 (compile) had an issue — check the output above"
    fi
    if [ "$NEED_PIP" = 1 ]; then
        inf "Reinstalling Python dependencies (install.sh --step 5)"
        bash install.sh --step 5 || warn "step 5 (python deps) had an issue — check the output above"
    fi
}
print_sector_summary(){
    echo
    inf "── Sector summary ──"
    $QUICK && inf "  (--quick mode: sector checks skipped)" || {
        printf "  BINARY=%s  GRID/CFG=%s  PYTHON=%s  REALTIME=%s  ASSOC=%s  MECHANISM=%s  PROXY=%s\n" \
            "$SEC_BIN" "$SEC_CFG" "$SEC_PY" "$SEC_RT" "$SEC_ASSOC" "$SEC_FOCONET" "$SEC_PROXY"
    }
}

inf "Fetching remote info …"
git fetch origin --quiet "$BR" 2>/dev/null || git fetch origin --quiet || { err "git fetch failed — check the connection/remote."; exit 1; }
BEHIND="$(git rev-list --count "HEAD..origin/${BR}" 2>/dev/null || echo 0)"
if [ "${BEHIND:-0}" -eq 0 ]; then
    ok "Already up to date (${OLD_VER}, ${OLD_HEAD}) — no new commits."
    if $CHECK; then
        exit 0
    fi
    if $QUICK; then
        inf "(--quick) nothing to pull — skipping the sector check too."
        exit 0
    fi
    # IMPORTANT: "0 new commits" only means the CODE is current — it says
    # nothing about whether install.sh has actually been run for it. A fresh
    # `git clone` (or a checkout that was manually `git pull`-ed outside this
    # script) lands here on its very first `update.sh` run despite never
    # having built glass-app, cloned FocoNet, or pip-installed PyOcto/pyrocko
    # — exiting early used to silently skip repairing exactly that gap.
    # Always verify sectors here (same as the normal path below) so a stale
    # environment still gets fixed even with nothing new to pull.
    verify_sectors
    if [ "$NEED_DOWNLOAD" = 1 ] || [ "$NEED_COMPILE" = 1 ] || [ "$NEED_PIP" = 1 ]; then
        echo
        inf "── Repairing missing/broken components ──"
        do_repairs
        echo
        inf "── Restart & health check ──"
        restart_server
        sleep 2
        health_check
    else
        ok "All sectors OK — nothing to repair."
    fi
    print_sector_summary
    exit 0
fi
inf "${BEHIND} new commit(s) on origin/${BR}:"
git log --oneline "HEAD..origin/${BR}" | sed 's/^/    /' | head -30
CHANGED="$(git diff --name-only "HEAD..origin/${BR}")"

if $CHECK; then
    inf "(--check) not applying. Run 'bash update.sh' to update."
    exit 0
fi

# Stash uncommitted local changes (config edits, etc.) before pulling
STASHED=0
if ! git diff --quiet || ! git diff --cached --quiet; then
    warn "There are uncommitted local changes → stashing them temporarily (auto-restored after the pull)."
    if git stash push -u -m "update.sh autostash" >/dev/null 2>&1; then STASHED=1; fi
fi

if ! git pull --ff-only origin "$BR"; then
    err "git pull failed (likely a conflict). Resolve manually then retry."
    [ "$STASHED" = 1 ] && git stash pop >/dev/null 2>&1 || true
    exit 1
fi
NEW_VER="$(ver_get)"; NEW_HEAD="$(git rev-parse --short HEAD)"

# 1) Actions driven by which files changed (git diff)
echo "$CHANGED" | grep -qE '(^|/)(requirements\.txt|environment\.yml|pyproject\.toml)$' && NEED_PIP=1
echo "$CHANGED" | grep -qE '^core/src/|^install\.sh$'                                   && NEED_COMPILE=1
# install.sh itself can add/change pip installs OUTSIDE environment.yml/
# requirements.txt (e.g. PyOcto's --no-deps install in step5_python, added
# without touching environment.yml) — a file-level diff can't tell WHICH
# step changed, so treat any install.sh change as needing both a recompile
# AND a pip-deps pass, rather than only the (safe but incomplete) former.
echo "$CHANGED" | grep -qE '^install\.sh$'                                              && NEED_PIP=1
echo "$CHANGED" | grep -qE '\.(py|js|html|css|yaml)$'                                   && NEED_RESTART=1

# 2) CHECK EVERY SECTOR (default; skipped with --quick) → repair whatever is broken/missing
if ! $QUICK; then
    verify_sectors
fi

# 3) Repairs: download external sources → compile → python deps (per the flags)
do_repairs

# 4) Restore the local changes that were stashed earlier
if [ "$STASHED" = 1 ]; then
    git stash pop >/dev/null 2>&1 || warn "Conflict while restoring local changes (git stash) — resolve manually: git stash list"
fi

# 5) Restart the server + health check (important for realtime). Always restart since there are new commits.
echo
inf "── Restart & health check ──"
restart_server
sleep 2
health_check

# 6) Summary of every sector
print_sector_summary
ok "Update complete:  ${OLD_VER} (${OLD_HEAD})  →  ${NEW_VER} (${NEW_HEAD})"
