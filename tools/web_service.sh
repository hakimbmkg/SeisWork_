#!/usr/bin/env bash
# ==============================================================================
# web_service.sh — repair the crash-looping systemd --user service 'seiswork'
#   "/home/<user>/bin/seiswork: line 3: exec: conda: not found"  (status=127).
#
# Cause: ExecStart uses the ~/bin/seiswork wrapper, which calls `conda`,
# but the systemd PATH has no conda. Fix: point ExecStart directly at the
# `seiswork` binary INSIDE the conda env (its shebang already targets the python
# env, no `conda` needed), fix Environment=PATH, then restart & verify.
#
# Usage (on the machine/account whose service is broken — NO sudo, this is a --user service):
#   bash web_service.sh
#
# Idempotent: safe to run repeatedly. Optional env: SEISWORK_PORT (default 5000).
# ==============================================================================
# ── --print mode: print a ready-to-paste block for any VM's terminal ────────
if [ "${1:-}" = "--print" ]; then
    cat <<'PASTE_BLOCK'
# ── Paste this whole block into the VM terminal (logged in as the SeisWork user) ──
bash <<'EOF'
set -eu
UNIT="$HOME/.config/systemd/user/seiswork.service"
PORT="${SEISWORK_PORT:-5000}"
WRAP="$HOME/bin/seiswork"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

[ -f "$UNIT" ] || { echo "FAILED: $UNIT not found. Run install.sh --service first."; exit 1; }

ENVNAME="$(grep -oE '\-n[[:space:]]+[A-Za-z0-9_.-]+' "$WRAP" 2>/dev/null | head -1 | awk '{print $2}')"
ENVNAME="${ENVNAME:-seiswork}"
APP="$(grep -oE '/[^\" ]+/seiswork\.py' "$WRAP" 2>/dev/null | head -1)"
[ -z "$APP" ] && APP="$HOME/seiswork/seiswork.py"

CBASE=""
command -v conda >/dev/null 2>&1 && CBASE="$(conda info --base 2>/dev/null | tr -d '[:space:]' || true)"
ENVPY=""
[ -n "$CBASE" ] && [ -x "$CBASE/envs/$ENVNAME/bin/python" ] && ENVPY="$CBASE/envs/$ENVNAME/bin/python"
[ -z "$ENVPY" ] && ENVPY="$(find "$HOME" /opt /data -maxdepth 7 \
    -path "*/envs/${ENVNAME}/bin/python" 2>/dev/null | head -1)"

[ -z "$ENVPY" ] && { echo "FAILED: python env '$ENVNAME' not found"; exit 1; }
[ -f "$APP"  ] || { echo "FAILED: $APP not found";  exit 1; }
echo "Python : $ENVPY"
echo "App    : $APP"

ENVBIN="$(dirname "$ENVPY")"
CONDA_ROOT="$(cd "$ENVBIN/../../.." && pwd)"
NEWPATH="$ENVBIN:$CONDA_ROOT/condabin:$CONDA_ROOT/bin:$HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

cp -a "$UNIT" "$UNIT.bak.$(date +%s)"
sed -i "s|^ExecStart=.*|ExecStart=$ENVPY $APP gui --host 0.0.0.0 --port $PORT --no-browser|" "$UNIT"
grep -q '^Environment=PATH=' "$UNIT" \
  && sed -i "s|^Environment=PATH=.*|Environment=PATH=$NEWPATH|" "$UNIT" \
  || sed -i "/^ExecStart=/i Environment=PATH=$NEWPATH" "$UNIT"

pkill -f 'seiswork' 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user reset-failed seiswork 2>/dev/null || true
systemctl --user restart seiswork
sleep 4

echo "state : $(systemctl --user is-active seiswork)"
curl -w "upload: HTTP %{http_code}\n" -sS -o /dev/null \
  -X POST "http://localhost:$PORT/api/online/inventory/upload"
journalctl --user -u seiswork -n 8 --no-pager
EOF
PASTE_BLOCK
    exit 0
fi

set -u
shopt -s nullglob

GRN='\033[0;32m'; YLW='\033[0;33m'; RED='\033[0;31m'; RST='\033[0m'
ok(){   echo -e "${GRN}✓${RST} $*"; }
warn(){ echo -e "${YLW}!${RST} $*"; }
err(){  echo -e "${RED}✗${RST} $*"; }

UNIT="$HOME/.config/systemd/user/seiswork.service"
PORT="${SEISWORK_PORT:-5000}"
WRAP="$HOME/bin/seiswork"

# ── 0. Prerequisites ─────────────────────────────────────────────────────────
command -v systemctl >/dev/null 2>&1 || { err "systemctl not found (not a systemd system)"; exit 1; }
if [ ! -f "$UNIT" ]; then
    err "Unit not found: $UNIT"
    warn "Create it first:  cd <seiswork-repo> && bash install.sh --service"
    exit 1
fi

# ── 1. Find the seiswork binary INSIDE the conda env ────────────────────────
# $HOME/.conda is conda's OTHER default envs_dirs entry (used when the base
# install is shared/system-wide and this user has no miniconda3/miniforge3/
# anaconda3 of their own) — easy to miss since it holds only `envs/`, no `bin/`.
CANDS=()
for root in "$HOME/miniconda3" "$HOME/miniforge3" "$HOME/mambaforge" \
            "$HOME/anaconda3" "$HOME/.conda" /opt/miniconda3 /opt/miniforge3 /opt/conda; do
    for b in "$root"/envs/*/bin/seiswork "$root"/bin/seiswork; do
        [ -x "$b" ] && CANDS+=("$b")
    done
done
# Multiple conda envs can each have their own bin/seiswork (e.g. a stray
# console-script left over in an unrelated env like "noisepy" from an old
# install). Alphabetical glob order would then silently pick the wrong one
# ("noisepy" < "seiswork"), so prefer any candidate living in an env
# literally named "seiswork" — the canonical name install.sh creates.
if [ "${#CANDS[@]}" -gt 1 ]; then
    PRIORITY=()
    for c in "${CANDS[@]}"; do
        [[ "$c" == */envs/seiswork/bin/seiswork ]] && PRIORITY+=("$c")
    done
    [ "${#PRIORITY[@]}" -gt 0 ] && CANDS=("${PRIORITY[@]}")
fi

# Fallback 1: parse the ~/bin/seiswork wrapper for the env name + conda base location.
ENVNAME=""; CBASE=""
if [ -f "$WRAP" ]; then
    ENVNAME="$(grep -oE 'activate[[:space:]]+[A-Za-z0-9_.-]+|-n[[:space:]]+[A-Za-z0-9_.-]+' "$WRAP" 2>/dev/null | head -1 | awk '{print $2}')"
    CBASE="$(grep -oE '[^ "'"'"']*/etc/profile\.d/conda\.sh' "$WRAP" 2>/dev/null | head -1 | sed 's#/etc/profile\.d/conda\.sh##')"
    [ -z "$CBASE" ] && CBASE="$(grep -oE '[^ "'"'"']*/(bin|condabin)/conda' "$WRAP" 2>/dev/null | head -1 | sed -E 's#/(bin|condabin)/conda##')"
fi
# Fallback to conda info --base when CBASE is still empty (wrapper lacks a full path)
if [ -z "$CBASE" ] && command -v conda >/dev/null 2>&1; then
    CBASE="$(conda info --base 2>/dev/null | tr -d '[:space:]' || true)"
fi
if [ "${#CANDS[@]}" -eq 0 ] && [ -n "$CBASE" ] && [ -n "$ENVNAME" ] && [ -x "$CBASE/envs/$ENVNAME/bin/seiswork" ]; then
    CANDS+=("$CBASE/envs/$ENVNAME/bin/seiswork")
fi
# Fallback 2: ask conda directly when it is on PATH.
if [ "${#CANDS[@]}" -eq 0 ] && command -v conda >/dev/null 2>&1; then
    c="$(conda run -n "${ENVNAME:-seiswork}" which seiswork 2>/dev/null || true)"
    [ -n "$c" ] && [ -x "$c" ] && CANDS+=("$c")
fi
# Fallback 3: search all of HOME/opt/data (except the wrapper itself).
if [ "${#CANDS[@]}" -eq 0 ]; then
    while IFS= read -r f; do
        [ "$f" = "$WRAP" ] && continue
        [ -x "$f" ] && CANDS+=("$f")
    done < <( { find "$HOME" /opt /data -maxdepth 7 -type f -path '*/envs/*/bin/seiswork' 2>/dev/null
                find "$HOME" /opt /data -maxdepth 7 -type f -path '*/bin/seiswork'        2>/dev/null; } )
fi

# Fallback 4: no console-script bin/seiswork (the launcher uses
# `python <repo>/seiswork.py`). Use the python env + that script directly.
#   EXEC_PREFIX = "<env-python> <repo>/seiswork.py"  (or "<bin/seiswork>")
EXEC_PREFIX=""; ENVBIN=""
if [ "${#CANDS[@]}" -eq 0 ]; then
    APP="$(grep -oE '/[^" ]+/seiswork\.py' "$WRAP" 2>/dev/null | head -1)"
    [ -z "$APP" ] && for a in "$HOME/seiswork/seiswork.py" "$HOME/apps/seiswork/seiswork.py"; do [ -f "$a" ] && APP="$a" && break; done
    ENVPY=""
    if [ -n "$CBASE" ] && [ -x "$CBASE/envs/${ENVNAME:-seiswork}/bin/python" ]; then
        ENVPY="$CBASE/envs/${ENVNAME:-seiswork}/bin/python"
    fi
    [ -z "$ENVPY" ] && ENVPY="$(find "$HOME" /opt /data -maxdepth 7 -type f -path "*/envs/${ENVNAME:-seiswork}/bin/python" 2>/dev/null | head -1)"
    if [ -n "$ENVPY" ] && [ -n "$APP" ] && [ -f "$APP" ]; then
        EXEC_PREFIX="$ENVPY $APP"
        ENVBIN="$(dirname "$ENVPY")"
        ok "Python env  : $ENVPY"
        ok "Launcher    : $APP"
    fi
fi

if [ -z "$EXEC_PREFIX" ] && [ "${#CANDS[@]}" -gt 0 ]; then
    EXEC_PREFIX="${CANDS[0]}"
    ENVBIN="$(dirname "${CANDS[0]}")"
    ok "Binari env  : ${CANDS[0]}"
fi

if [ -z "$EXEC_PREFIX" ]; then
    err "SeisWork not found inside the conda env (neither console-script nor python+seiswork.py)."
    warn "Send this output for further diagnosis:"
    echo "  conda info --envs 2>/dev/null ; head -6 $WRAP ; ls -d $HOME/*conda* /opt/*conda* 2>/dev/null"
    exit 1
fi

# ── 2. Build PATH: env bin + conda (in case of subprocesses) + repo core/bin + system ─
CONDA_ROOT="$(cd "$ENVBIN/../../.." 2>/dev/null && pwd || true)"
PATH_EXTRA="$ENVBIN"
[ -n "$CONDA_ROOT" ] && [ -d "$CONDA_ROOT/condabin" ] && PATH_EXTRA="$PATH_EXTRA:$CONDA_ROOT/condabin"
[ -n "$CONDA_ROOT" ] && [ -d "$CONDA_ROOT/bin" ]      && PATH_EXTRA="$PATH_EXTRA:$CONDA_ROOT/bin"

WD="$(sed -n 's/^WorkingDirectory=//p' "$UNIT" | head -1)"
[ -z "$WD" ] && for d in "$HOME/seiswork" "$HOME/apps/seiswork"; do [ -d "$d" ] && WD="$d" && break; done
[ -n "$WD" ] && [ -d "$WD/core/bin" ] && PATH_EXTRA="$PATH_EXTRA:$WD/core/bin"

NEWPATH="$PATH_EXTRA:$HOME/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# ── 3. Back up + rewrite ExecStart & Environment=PATH ───────────────────────
cp -a "$UNIT" "$UNIT.bak.$(date +%s)" && ok "Unit backup : ${UNIT}.bak.*"

NEW_EXEC="ExecStart=$EXEC_PREFIX gui --host 0.0.0.0 --port $PORT --no-browser"
if grep -q '^ExecStart=' "$UNIT"; then
    sed -i "s|^ExecStart=.*|$NEW_EXEC|" "$UNIT"
else
    sed -i "/^\[Service\]/a $NEW_EXEC" "$UNIT"
fi
if grep -q '^Environment=PATH=' "$UNIT"; then
    sed -i "s|^Environment=PATH=.*|Environment=PATH=$NEWPATH|" "$UNIT"
else
    sed -i "/^ExecStart=/i Environment=PATH=$NEWPATH" "$UNIT"
fi
ok "ExecStart   → $EXEC_PREFIX gui --host 0.0.0.0 --port $PORT --no-browser"
ok "PATH        → $NEWPATH"

# ── 4. Kill any old (manually started) seiswork process still holding :PORT, restart ─
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if pkill -f 'seiswork\.web\.app' 2>/dev/null; then warn "old (manually started) seiswork process killed"; fi
systemctl --user daemon-reload
systemctl --user reset-failed seiswork 2>/dev/null || true
systemctl --user enable seiswork >/dev/null 2>&1 || true
systemctl --user restart seiswork

# ── 5. Verify ────────────────────────────────────────────────────────────────
sleep 3
state="$(systemctl --user is-active seiswork 2>/dev/null || echo unknown)"
if [ "$state" = "active" ]; then
    ok "service active"
else
    err "service not active yet ($state). Recent log:"
    journalctl --user -u seiswork -n 15 --no-pager
    exit 1
fi

code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 8 \
        -X POST "http://localhost:$PORT/api/online/inventory/upload" 2>/dev/null || echo 000)"
case "$code" in
    400) ok "upload endpoint OK (HTTP 400 = route active, just send the file from the GUI)";;
    404) warn "still 404 — the code on disk may be stale. Update:  cd \"$WD\" && bash update.sh";;
    000) warn "server not responding on :$PORT yet — check: journalctl --user -u seiswork -n 30";;
    *)   ok "upload endpoint responded HTTP $code (not 404 → route active)";;
esac

echo
ok "Done. GUI: http://localhost:$PORT  (or http://\$(hostname -I):$PORT)"
echo "  status : systemctl --user status seiswork"
echo "  log    : journalctl --user -u seiswork -f"
