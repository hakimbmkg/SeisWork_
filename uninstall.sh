#!/usr/bin/env bash
# ==============================================================================
#  SeisWork — Uninstaller  (counterpart to install.sh)
#  Author  : HakimBMKG
#
#  Removes everything install.sh creates OUTSIDE the repo, plus the conda
#  env / venv. The repo checkout itself and shared base installs (Miniforge,
#  Docker engine) are left untouched unless explicitly requested.
#
#  Removes by default:
#    - systemd user service (seiswork.service)
#    - launcher  ~/start_seiswork.sh  and  ~/bin/seiswork
#    - ~/bin symlinks to core/bin binaries (REAL, NLLoc, growclust, ...)
#    - PATH block install.sh added to ~/.bashrc (only the exact marker block)
#    - conda env 'seiswork'  /  venv ~/.venv/seiswork   (asks to confirm)
#
#  Opt-in (off by default):
#    --docker        remove Docker image seiswork/phasenet:tf2.12
#    --purge-build   remove core/bin/* (compiled) and core/src downloads
#                     (keeps core/src/velest, core/src/hypo71 — those ship in git)
#    --purge-repo    delete the whole repo checkout — requires typed "HAPUS"
#
#  Usage:
#    bash uninstall.sh                ← interactive, safe defaults
#    bash uninstall.sh -y             ← skip confirmations (still asks for --purge-repo)
#    bash uninstall.sh --dry-run      ← show what would be removed, do nothing
#    bash uninstall.sh --keep-env     ← leave conda env / venv alone
#    bash uninstall.sh --docker --purge-build
# ==============================================================================

set -euo pipefail

SEISWORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$SEISWORK_DIR/core"
SRC_DIR="$CORE_DIR/src"
BIN_DIR="$CORE_DIR/bin"
HOME_BIN="$HOME/bin"
ENV_NAME="seiswork"
VENV_DIR="$HOME/.venv/seiswork"
PN_IMAGE="seiswork/phasenet:tf2.12"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
CYN='\033[1;36m'; RST='\033[0m'
ok()   { echo -e "${GRN}  ✓  ${RST}$*"; }
warn() { echo -e "${YLW}  !  ${RST}$*"; }
err()  { echo -e "${RED}  ✗  ${RST}$*"; }
hdr()  { echo -e "\n${CYN}$(printf '─%.0s' {1..60})\n  $*\n$(printf '─%.0s' {1..60})${RST}"; }
inf()  { echo    "     $*"; }

YES=false
DRY=false
KEEP_ENV=false
DO_DOCKER=false
PURGE_BUILD=false
PURGE_REPO=false

for arg in "$@"; do
    case "$arg" in
        -y|--yes)       YES=true ;;
        --dry-run)      DRY=true ;;
        --keep-env)     KEEP_ENV=true ;;
        --docker)       DO_DOCKER=true ;;
        --purge-build)  PURGE_BUILD=true ;;
        --purge-repo)   PURGE_REPO=true ;;
        -h|--help)
            sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0 ;;
        *) err "Unknown option: $arg"; exit 1 ;;
    esac
done

run() {
    if $DRY; then echo "     [dry-run] $*"; else "$@"; fi
}

confirm() {
    $YES && return 0
    local prompt="$1"
    read -r -p "     $prompt [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]]
}

# Known SeisWork binaries published into ~/bin by install.sh (Step 3).
KNOWN_BINS=(REAL hypoDD ph2dt NLLoc Grid2Time Vel2Grid velest hypoinverse \
            growclust MatchLocate2 SelectFinal FDTCC PhsAssoc Time2EQ)

# ── 1. Systemd user service ────────────────────────────────────────────────
step_service() {
    hdr "Systemd user service"
    if ! command -v systemctl &>/dev/null; then
        inf "systemctl not found — skipping"
        return 0
    fi
    local svc_file="$HOME/.config/systemd/user/seiswork.service"
    if systemctl --user is-enabled seiswork &>/dev/null || systemctl --user is-active seiswork &>/dev/null; then
        run systemctl --user stop seiswork 2>/dev/null || true
        run systemctl --user disable seiswork 2>/dev/null || true
        ok "seiswork.service stopped + disabled"
    else
        inf "seiswork.service not installed"
    fi
    if [ -f "$svc_file" ]; then
        run rm -f "$svc_file"
        run systemctl --user daemon-reload 2>/dev/null || true
        ok "Unit file removed: $svc_file"
    fi
}

# ── 2. Docker image (opt-in) ───────────────────────────────────────────────
step_docker() {
    $DO_DOCKER || return 0
    hdr "Docker image"
    if ! command -v docker &>/dev/null; then
        inf "docker not found — skipping"
        return 0
    fi
    if docker image inspect "$PN_IMAGE" &>/dev/null; then
        run docker rmi "$PN_IMAGE" && ok "Image removed: $PN_IMAGE"
    else
        inf "Image $PN_IMAGE not found"
    fi
}

# ── 3. Launcher + PATH block ───────────────────────────────────────────────
step_launcher() {
    hdr "Launcher & PATH"
    if [ -f "$HOME/start_seiswork.sh" ]; then
        run rm -f "$HOME/start_seiswork.sh"
        ok "Removed: ~/start_seiswork.sh"
    fi
    if [ -f "$HOME_BIN/seiswork" ]; then
        run rm -f "$HOME_BIN/seiswork"
        ok "Removed: ~/bin/seiswork"
    fi
    # Only strip the exact marker block install.sh appends — never touch
    # other PATH lines a user may already have for ~/bin.
    if grep -q '^# SeisWork binaries$' "$HOME/.bashrc" 2>/dev/null; then
        if $DRY; then
            echo "     [dry-run] would remove '# SeisWork binaries' block from ~/.bashrc"
        else
            sed -i.bak '/^# SeisWork binaries$/,/^export PATH="\$HOME\/bin:\$PATH"$/d' "$HOME/.bashrc"
            rm -f "$HOME/.bashrc.bak"
        fi
        ok "SeisWork PATH block removed from ~/.bashrc"
    else
        inf "No SeisWork PATH block found in ~/.bashrc"
    fi
}

# ── 4. ~/bin binaries/symlinks ─────────────────────────────────────────────
step_binaries() {
    hdr "Binaries in ~/bin"
    local removed=0
    for name in "${KNOWN_BINS[@]}"; do
        local p="$HOME_BIN/$name"
        [ -e "$p" ] || [ -L "$p" ] || continue
        if [ -L "$p" ]; then
            run rm -f "$p"
            ok "Symlink removed: ~/bin/$name"
        else
            run rm -f "$p"
            warn "File (not a symlink) removed: ~/bin/$name"
        fi
        removed=$((removed + 1))
    done
    if [ "$removed" -eq 0 ]; then inf "No SeisWork binaries found in ~/bin"; fi
}

# ── 5. Conda env / venv ────────────────────────────────────────────────────
step_env() {
    if $KEEP_ENV; then inf "Skipping conda env / venv (--keep-env)"; return 0; fi
    hdr "Conda env / venv"

    if [ -d "$VENV_DIR" ]; then
        if confirm "Remove venv $VENV_DIR?"; then
            run rm -rf "$VENV_DIR"
            ok "venv removed: $VENV_DIR"
        else
            inf "venv left in place"
        fi
    fi

    local conda_bin=""
    for c in "$(command -v conda 2>/dev/null)" \
             "$HOME/miniforge3/bin/conda" "$HOME/miniconda3/bin/conda" \
             "$HOME/anaconda3/bin/conda" "/opt/miniconda3/bin/conda" \
             "/opt/miniforge3/bin/conda" "/opt/anaconda3/bin/conda"; do
        if [ -n "$c" ] && [ -x "$c" ]; then conda_bin="$c"; break; fi
    done

    if [ -n "$conda_bin" ] && "$conda_bin" env list 2>/dev/null | grep -q "^$ENV_NAME\b"; then
        if confirm "Remove conda env '$ENV_NAME'?"; then
            run "$conda_bin" env remove -n "$ENV_NAME" -y
            ok "conda env '$ENV_NAME' removed"
        else
            inf "conda env '$ENV_NAME' left in place"
        fi
    elif [ ! -d "$VENV_DIR" ]; then
        inf "No conda env / venv 'seiswork' found"
    fi
}

# ── 6. Build artifacts (opt-in) ────────────────────────────────────────────
step_build() {
    $PURGE_BUILD || return 0
    hdr "Build artifacts (core/bin, core/src downloads)"
    if [ -d "$BIN_DIR" ]; then
        run rm -rf "${BIN_DIR:?}"/*
        ok "Cleaned: $BIN_DIR/*"
    fi
    if [ -d "$SRC_DIR" ]; then
        find "$SRC_DIR" -mindepth 1 -maxdepth 1 \
            ! -name velest ! -name hypo71 -exec rm -rf {} + 2>/dev/null || true
        ok "Cleaned: $SRC_DIR/* (except velest, hypo71 — their sources ship in git)"
    fi
}

# ── 7. Whole repo checkout (opt-in, double confirm) ───────────────────────
step_repo() {
    $PURGE_REPO || return 0
    hdr "Delete the entire repo directory"
    warn "This will PERMANENTLY delete: $SEISWORK_DIR"
    read -r -p "     Type HAPUS to confirm: " typed
    if [ "$typed" = "HAPUS" ]; then
        run rm -rf "${SEISWORK_DIR:?}"
        ok "Repo removed: $SEISWORK_DIR"
    else
        warn "Confirmation did not match — repo NOT removed"
    fi
}

main() {
    hdr "SeisWork — Uninstaller"
    $DRY && warn "DRY RUN — no changes will be made"

    step_service
    step_docker
    step_launcher
    step_binaries
    step_env
    step_build
    step_repo

    echo
    ok "Done."
    if ! $PURGE_REPO; then
        inf "Repo directory left in place: $SEISWORK_DIR"
        inf "To also remove the repo: bash uninstall.sh --purge-repo"
    fi
}

main
