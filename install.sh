#!/usr/bin/env bash
# ==============================================================================
#  SeisWork — Smart cross-platform installer  (Linux • macOS • WSL)
#  Author  : HakimBMKG
#
#  Auto-detects OS, architecture and package manager (apt/dnf/yum/zypper/pacman
#  or Homebrew). When the system lacks compilers, Python or conda it screens the
#  environment and installs the right pieces automatically:
#    - missing build tools  → installed via the native package manager
#    - no conda             → Miniforge bootstrapped (no root needed)
#    - no python (venv mode)→ python3 installed via package manager
#
#  Steps (in order):
#    Step 1  Screen + provision OS deps  (gcc, gfortran, cmake, git, wget, python/conda)
#    Step 2  Download source code        (REAL, HypoDD, NonLinLoc, GrowClust, MatchLocate2,
#                                          FDTCC, neic-glass3, FocoNet)
#    Step 3  Compile binaries            → core/bin/ + ~/bin/
#            (incl. VELEST & Hypoinverse — source ships in core/src/; incl. glass-app
#             from neic-glass3, BUILD_GLASS-BROKER-APP=OFF, no Kafka/ActiveMQ needed;
#             FocoNet is pure Python, no compile — cloning IS the install)
#    Step 3b Docker fallback (auto)      any binary step 3 couldn't compile
#            natively runs via the seiswork-corebin Docker image instead —
#            native stays primary (no per-call container overhead), Docker
#            only fills the gaps. Skipped silently when Docker isn't installed.
#    Step 4  Create Python env           conda 'seiswork' OR venv ~/.venv/seiswork
#            (env update, not just skip, when the env already exists — additive only)
#    Step 5  Install Python packages     (PyTorch auto-CUDA/CPU, seisbench, GaMMA, PyOcto,
#                                          pyrocko (FocoNet), slab deps, pywebview)
#    Step 6  Verify + launcher           ~/start_seiswork.sh + PATH setup
#    Step 7  Docker + PhaseNet-native GPU image (optional fast picker; OS-aware)
#    Step 8  Systemd user service        auto-start SeisWork GUI at boot (Linux only)
#
#  VELEST & Hypoinverse: source ships in the repo (core/src/velest,
#  core/src/hypo71/source) and Step 3 compiles them automatically (gfortran).
#
#  Usage:
#    bash install.sh               ← full install (recommended; auto-provisions deps)
#    bash install.sh --venv        ← force venv instead of conda
#    bash install.sh --no-auto     ← only report missing deps, don't install them
#    bash install.sh --check       ← status overview only
#    bash install.sh --step 3      ← rerun only one step
#    bash install.sh --step 4,5    ← rerun env + Python
#    bash install.sh --step 8      ← install/update the systemd user service only
#    bash install.sh --service     ← alias: install the systemd service (step 8) only
#    bash install.sh --desktop     ← create the desktop app only (macOS .app / Linux .desktop)
#
#  After install (conda):  conda activate seiswork && seiswork gui
#  After install (venv) :  bash ~/start_seiswork.sh
#  Auto-start via service: seiswork service status   (after step 8)
# ==============================================================================

set -euo pipefail

# ── Paths ──────────────────────────────────────────────────────────────────────
SEISWORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CORE_DIR="$SEISWORK_DIR/core"
SRC_DIR="$CORE_DIR/src"
BIN_DIR="$CORE_DIR/bin"
HOME_BIN="$HOME/bin"
ENV_YML="$SEISWORK_DIR/environment.yml"
ENV_NAME="seiswork"
VENV_DIR="$HOME/.venv/seiswork"

# ── PhaseNet-native (Docker GPU picker) ─────────────────────────────────────────
PHASENET_DIR="${PHASENET_DIR:-$HOME/apps/PhaseNet}"
PN_IMAGE="seiswork/phasenet:tf2.12"
PN_DOCKER_CTX="$SEISWORK_DIR/docker/phasenet"

mkdir -p "$SRC_DIR" "$BIN_DIR" "$HOME_BIN"

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
CYN='\033[1;36m'; BLD='\033[1m';    RST='\033[0m'
ok()   { echo -e "${GRN}  ✓  ${RST}$*"; }
warn() { echo -e "${YLW}  !  ${RST}$*"; }
err()  { echo -e "${RED}  ✗  ${RST}$*"; }
hdr()  { echo -e "\n${CYN}$(printf '─%.0s' {1..60})\n  $*\n$(printf '─%.0s' {1..60})${RST}"; }
inf()  { echo    "     $*"; }
die()  { err "$*"; exit 1; }

# ==============================================================================
# Cross-platform layer — OS / arch / package manager (Linux • macOS • WSL)
# ==============================================================================
UNAME_S="$(uname -s)"
case "$UNAME_S" in
    Darwin) OS_KIND="macos" ;;
    Linux)
        if grep -qiE "(microsoft|wsl)" /proc/version 2>/dev/null; then
            OS_KIND="wsl"
        else
            OS_KIND="linux"
        fi ;;
    *)      OS_KIND="unknown" ;;
esac
IS_MAC=false; [ "$OS_KIND" = "macos" ] && IS_MAC=true
ARCH="$(uname -m)"     # x86_64 | arm64 | aarch64

# Auto-install missing system deps by default; --no-auto disables it.
AUTO_DEPS=true

# Package manager (brew on macOS; first available on Linux/WSL)
detect_pkgmgr() {
    if $IS_MAC; then command -v brew &>/dev/null && echo brew || echo ""; return; fi
    for m in apt-get dnf yum zypper pacman; do
        command -v "$m" &>/dev/null && { echo "$m"; return; }
    done
    echo ""
}
PKG="$(detect_pkgmgr)"

# Root / sudo wrapper — works as root, with sudo, or warns when neither
_SUDO() {
    if [ "$(id -u)" = 0 ]; then "$@"
    elif command -v sudo &>/dev/null; then sudo "$@"
    else warn "root required for: $*"; return 1; fi
}

# Portable in-place sed (GNU vs BSD/macOS) and CPU count
sedi()  { if $IS_MAC; then sed -i '' "$@"; else sed -i "$@"; fi; }
NPROC() { if $IS_MAC; then sysctl -n hw.ncpu 2>/dev/null || echo 2; else nproc 2>/dev/null || echo 2; fi; }

resolve_path() {
    local p="$1"
    readlink -f "$p" 2>/dev/null && return 0
    python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$p" 2>/dev/null && return 0
    printf '%s\n' "$p"
}
# launcher is ~/bin/seiswork (run 'seiswork gui'); the ~/start_seiswork.sh script is
# only created for venv installs — never hardcode it for both modes.
launch_cmd() {
    if $USE_VENV; then printf 'bash ~/start_seiswork.sh\n'
    else printf 'seiswork gui   (or: conda activate %s && seiswork gui)\n' "$ENV_NAME"; fi
}
# Append the ~/bin PATH export to a shell rc file, unless an ACTIVE line already exists.
# Anchored to '^\s*export' so a commented-out sample (common in the default macOS
# ~/.zshrc) does NOT count as present — that false match would silently leave ~/bin off
# PATH and the 'seiswork' launcher unfound.
add_home_bin_path() {
    local rc="$1"; [ -n "$rc" ] || return 0
    if grep -qE '^[[:space:]]*export PATH=.*HOME/bin' "$rc" 2>/dev/null; then
        ok "PATH ~/bin already present in ${rc/#$HOME/~}"
    else
        printf '\n# SeisWork binaries\nexport PATH="$HOME/bin:$PATH"\n' >> "$rc"
        ok "PATH ~/bin added to ${rc/#$HOME/~}"
    fi
}

# Generic package install via the detected manager
pkg_install() {
    [ $# -eq 0 ] && return 0
    case "$PKG" in
        brew)     brew install "$@" ;;
        apt-get)  _SUDO apt-get update -qq && _SUDO apt-get install -y "$@" ;;
        dnf|yum)  _SUDO "$PKG" install -y "$@" ;;
        zypper)   _SUDO zypper install -y "$@" ;;
        pacman)   _SUDO pacman -Sy --noconfirm "$@" ;;
        *)        warn "Unknown package manager — install manually: $*"; return 1 ;;
    esac
}

# Map a generic build-tool name → distro-specific package name(s)
pkg_name() {
    local tool="$1"
    case "$tool" in
        gcc|make)
            case "$PKG" in apt-get) echo build-essential ;; brew) echo gcc ;; *) echo "gcc make" ;; esac ;;
        gfortran)
            case "$PKG" in
                apt-get) echo gfortran ;;
                dnf|yum) echo gcc-gfortran ;;
                zypper|pacman) echo gcc-fortran ;;
                brew) echo gcc ;;          # Homebrew gcc ships gfortran
                *) echo gfortran ;;
            esac ;;
        cmake) echo cmake ;;
        git)   echo git ;;
        wget)  echo wget ;;
        python3)
            case "$PKG" in
                apt-get) echo "python3 python3-venv python3-pip" ;;
                dnf|yum) echo "python3 python3-pip" ;;
                brew)    echo "python@3.11" ;;
                *)       echo python3 ;;
            esac ;;
        *) echo "$tool" ;;
    esac
}

# ── Native desktop GUI deps — for `seiswork gui --native` ─────────────────────
# macOS uses the built-in WKWebView (nothing to install). On Linux, pywebview's
# GTK backend needs system WebKitGTK + GObject-Introspection typelibs AND a
# PyGObject the seiswork interpreter can import. Installed UNCONDITIONALLY (not
# gated on a graphical session) — this same install.sh run is often done over
# SSH on a headless box before anyone ever sits at it with a display, so gating
# on $DISPLAY/$WAYLAND_DISPLAY at install time just means a second, manual round
# of "conda install pygobject" + "sudo apt install gir1.2-…" later. Every
# failure here is still non-fatal: `seiswork gui --native` always falls back to
# the browser when the GTK stack turns out to be missing at runtime.
native_gui_syspkgs() {
    case "$PKG" in
        apt-get) echo "gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libgirepository-1.0-1 python3-gi python3-gi-cairo" ;;
        dnf|yum) echo "gtk3 webkit2gtk4.1 gobject-introspection python3-gobject" ;;
        zypper)  echo "gtk3 libwebkit2gtk-4_1-0 gobject-introspection python3-gobject" ;;
        pacman)  echo "gtk3 webkit2gtk gobject-introspection python-gobject" ;;
        *)       echo "" ;;
    esac
}

# Common install locations for GI (GObject-Introspection) typelibs across distros
# — searched for Gtk-3.0.typelib to find where the system's GTK/WebKit typelibs
# actually live, independent of package manager or architecture.
_gi_typelib_dirs() {
    echo "/usr/lib/x86_64-linux-gnu/girepository-1.0
/usr/lib/aarch64-linux-gnu/girepository-1.0
/usr/lib64/girepository-1.0
/usr/lib/girepository-1.0"
}

install_native_gui_deps() {
    if $IS_MAC; then return 0; fi              # WKWebView is built into macOS
    local pkgs; pkgs="$(native_gui_syspkgs)"
    if ! $AUTO_DEPS; then
        warn "Native GUI (--native) needs system WebKitGTK. Install manually, e.g.:"
        inf  "  sudo $PKG install ${pkgs:-<webkit2gtk gtk3 gobject-introspection>}"
        return 0
    fi
    if [ -z "$pkgs" ]; then
        warn "Unknown package manager — for 'seiswork gui --native' install WebKitGTK + PyGObject manually."
        return 0
    fi
    inf "Installing native-GUI system libs (WebKitGTK/GTK) for 'seiswork gui --native' …"
    if [ "$PKG" = apt-get ]; then
        # Ubuntu 24.04+ ships webkit2-4.1; 22.04 and earlier only 4.0 — try 4.1, fall back to 4.0.
        # python3-gi/-cairo provide the actual `import gi` module — the typelibs
        # alone (previous bug) let GTK/WebKit *resolve* but left `import gi` raising
        # ModuleNotFoundError since nothing installed the PyGObject bindings.
        pkg_install gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libgirepository-1.0-1 python3-gi python3-gi-cairo \
            || pkg_install gir1.2-gtk-3.0 gir1.2-webkit2-4.0 libgirepository-1.0-1 python3-gi python3-gi-cairo \
            || warn "WebKitGTK libs failed — 'seiswork gui --native' will fall back to the browser."
    else
        pkg_install $pkgs || warn "WebKitGTK libs failed — 'seiswork gui --native' will fall back to the browser."
    fi
    # A conda interpreter can't import the *system* PyGObject, so install it into
    # the env from conda-forge — then `import gi` works and binds the system
    # WebKit typelib. (venv installs rely on the system python3-gi above.)
    if ! $USE_VENV && [ -n "$CONDA" ]; then
        inf "Installing PyGObject into '$ENV_NAME' (conda-forge) …"
        "$CONDA" install -n "$ENV_NAME" -c conda-forge -y pygobject pycairo &>/dev/null \
            && ok "PyGObject ready — native window enabled" \
            || warn "PyGObject (conda) failed — native GUI will fall back to the browser."
    fi
    # venv relies on the system python3-gi installed above, visible only if the
    # venv has --system-site-packages (set at creation, or repaired in step 4
    # for pre-existing venvs). Verify it actually resolves — a stale venv that
    # skipped the repair would otherwise silently fail at `seiswork gui --native`.
    if $USE_VENV && [ -x "$VENV_DIR/bin/python" ]; then
        if "$VENV_DIR/bin/python" -c "import gi" &>/dev/null; then
            ok "PyGObject ready — native window enabled"
        else
            warn "venv still can't 'import gi' — native GUI will fall back to the browser."
            inf "  Fix: bash install.sh --step 4,5   (repairs pyvenv.cfg, re-run this step)"
        fi
    fi
    # A conda-forge PyGObject does NOT search the system's girepository dir by
    # default, so `import gi; gi.require_version('Gtk','3.0')` fails with
    # "Namespace Gtk not available" even with the system typelibs installed
    # above — GI_TYPELIB_PATH has to point at them explicitly. `seiswork.cli`
    # also sets this at runtime (belt-and-suspenders across future re-installs
    # that skip this step), but report it here so `--check` reflects reality.
    local d found=""
    for d in $(_gi_typelib_dirs); do
        [ -f "$d/Gtk-3.0.typelib" ] && { found="$d"; break; }
    done
    if [ -n "$found" ]; then
        ok "GTK typelib search path: $found"
    else
        warn "No Gtk-3.0.typelib found under any known girepository dir — native window may fail to start."
    fi

    # ── pywebview itself — install explicitly, don't just rely on
    # environment.yml/requirements.txt ─────────────────────────────────────────
    # Both files DO list pywebview, but Step 4 used to skip conda env creation
    # entirely whenever the env already existed (e.g. a previous install that
    # got interrupted partway through Step 5) — so a pre-existing-but-incomplete
    # env could reach this point without pywebview ever having been installed,
    # and 'seiswork gui' would then fail with "No module named 'webview'" on
    # WSL/Linux. Idempotent: skip if already importable.
    if $USE_VENV; then
        if "$VENV_DIR/bin/python" -c "import webview" &>/dev/null 2>&1; then
            ok "pywebview already installed"
        else
            inf "Installing pywebview (native window support) …"
            pip_venv "pywebview>=5.0" -q \
                && ok "pywebview installed" \
                || warn "pywebview failed — 'seiswork gui --native' will fall back to the browser"
        fi
    else
        if crun bash -c "PYTHONNOUSERSITE=1 python -c 'import webview'" &>/dev/null; then
            ok "pywebview already installed"
        else
            inf "Installing pywebview (native window support) …"
            cpip "pywebview>=5.0" \
                && ok "pywebview installed" \
                || warn "pywebview failed — 'seiswork gui --native' will fall back to the browser"
        fi
    fi
}

# ── Env mode: conda (default) or venv (fallback / --venv) ─────────────────────
USE_VENV=false
FORCE_VENV=false   # set by --venv flag

find_conda() {
    for c in \
        "$(command -v conda 2>/dev/null)" \
        "$HOME/miniforge3/bin/conda" \
        "$HOME/miniconda3/bin/conda" \
        "$HOME/anaconda3/bin/conda" \
        "/opt/miniforge3/bin/conda" \
        "/opt/miniconda3/bin/conda" \
        "/opt/anaconda3/bin/conda" \
        "/opt/homebrew/Caskroom/miniforge/base/bin/conda"
    do
        [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }
    done
    echo ""
}
CONDA="$(find_conda)"

# Bootstrap Miniforge when no conda exists (no root needed; mac + linux/wsl).
bootstrap_miniforge() {
    local prefix="$HOME/miniforge3"
    [ -x "$prefix/bin/conda" ] && { CONDA="$prefix/bin/conda"; ok "Miniforge already present at $prefix"; return 0; }

    local osname arch url tmp
    if $IS_MAC; then osname="MacOSX"; else osname="Linux"; fi
    case "$ARCH" in
        x86_64|amd64)  arch="x86_64" ;;
        arm64|aarch64) arch="arm64"; $IS_MAC || arch="aarch64" ;;
        *)             arch="$ARCH" ;;
    esac
    url="https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-${osname}-${arch}.sh"
    tmp="$(mktemp /tmp/miniforge_XXXX.sh)"
    inf "Downloading Miniforge ($osname-$arch) …"
    local dl_ok=false
    for _attempt in 1 2 3; do
        [ "$_attempt" -gt 1 ] && { warn "Retry $_attempt/3 downloading Miniforge …"; rm -f "$tmp"; tmp="$(mktemp /tmp/miniforge_XXXX.sh)"; }
        if command -v curl &>/dev/null; then
            curl -fsSL --http1.1 --retry 3 --retry-delay 5 "$url" -o "$tmp" && dl_ok=true && break
        elif command -v wget &>/dev/null; then
            wget -q --tries=3 "$url" -O "$tmp" && dl_ok=true && break
        else err "curl/wget not found — cannot download Miniforge"; return 1; fi
    done
    if ! $dl_ok; then err "Miniforge download failed after 3 attempts"; rm -f "$tmp"; return 1; fi
    inf "Installing Miniforge to $prefix (no root) …"
    if bash "$tmp" -b -p "$prefix"; then
        rm -f "$tmp"
        CONDA="$prefix/bin/conda"
        "$CONDA" init bash &>/dev/null || true
        $IS_MAC && "$CONDA" init zsh &>/dev/null || true
        ok "Miniforge installed → $CONDA"
        return 0
    fi
    rm -f "$tmp"
    err "Miniforge installation failed"
    return 1
}

resolve_env_mode() {
    if $FORCE_VENV; then
        USE_VENV=true
        inf "Mode: venv (forced via --venv)"
    elif [ -z "$CONDA" ]; then
        if $AUTO_DEPS; then
            # Smart bootstrap will install Miniforge in STEP 1; still conda mode.
            USE_VENV=false
            inf "Mode: conda env '$ENV_NAME' (Miniforge will be installed automatically if needed)"
        else
            USE_VENV=true
            warn "conda not found — automatically using venv (~/.venv/seiswork)"
        fi
    else
        USE_VENV=false
        inf "Mode: conda env '$ENV_NAME'"
    fi
}

# ── pip wrapper: retry + timeout (lesson: connection drops mid-download of obspy) ─
# --retries 10 --timeout 300 tolerates an unstable connection (VPN, slow
# network) — prevents ReadTimeout/IncompleteRead on large packages (torch, obspy).
PIP_FLAGS="--retries 10 --timeout 300"

pip_venv()  { "$VENV_DIR/bin/pip" install $PIP_FLAGS "$@"; }
crun()      { "$CONDA" run --no-capture-output -n "$ENV_NAME" "$@"; }
# Always drive pip through the env's OWN python by absolute path ($CONDA_PREFIX
# is set to the activated env inside `conda run`). A bare `pip`/`python3` can
# resolve to a different interpreter (e.g. Homebrew's Python) when that dir
# precedes the env bin on PATH — which silently installs into the wrong Python.
cpip()      { crun bash -c 'PYTHONNOUSERSITE=1 "$CONDA_PREFIX/bin/python" -m pip install '"$PIP_FLAGS --no-user $*"; }

# Check whether the existing venv is valid (has pip). Lesson: on Ubuntu/Debian without
# python3-venv, `python3 -m venv DIR` still creates the directory but pip is missing
# (ensurepip fails silently). As a result every later pip step fails.
is_venv_valid() {
    [ -x "$1/bin/python" ] && [ -x "$1/bin/pip" ]
}

# ── Install a binary into core/bin/ (real copy) + a ~/bin/ symlink ───────────
install_bin() {
    local src="$1" name="$2"
    local real; real="$(resolve_path "$src")"
    [ -f "$real" ] || { warn "install_bin: $real not found — skip $name"; return; }
    chmod +x "$real" 2>/dev/null || true
    local dst_core="$BIN_DIR/$name"
    cp -f "$real" "$dst_core"
    chmod +x "$dst_core"
    rm -f "$HOME_BIN/$name"
    ln -sf "$dst_core" "$HOME_BIN/$name"
    ok "$name → $dst_core"
}

link_bin() {
    local src="$1" name="$2"
    local real; real="$(resolve_path "$src")"
    [ -f "$real" ] || { warn "link_bin: $real not found — skip $name"; return; }
    chmod +x "$real" 2>/dev/null || true
    for dst in "$BIN_DIR/$name" "$HOME_BIN/$name"; do
        rm -f "$dst"; ln -sf "$real" "$dst"
    done
    ok "$name → $BIN_DIR/$name  (symlink)"
}

has_bin() {
    local name="$1"
    command -v "$name" &>/dev/null \
        || [ -f "$BIN_DIR/$name" ] \
        || [ -f "$HOME_BIN/$name" ]
}

# ── Detect CUDA version ────────────────────────────────────────────────────────
detect_cuda() {
    command -v nvidia-smi &>/dev/null || { echo ""; return; }
    # Portable parse (BSD grep has no -P/\K): pull the version after "CUDA Version:".
    nvidia-smi 2>/dev/null \
        | sed -n 's/.*CUDA Version:[[:space:]]*\([0-9][0-9.]*\).*/\1/p' \
        | head -1 \
        || echo ""
}

cuda_to_whl() {
    local ver="$1"
    [ -z "$ver" ] && { echo "https://download.pytorch.org/whl/cpu"; return; }
    local major minor
    major="${ver%%.*}"
    minor="${ver#*.}"; minor="${minor%%.*}"
    if   [ "$major" -eq 12 ] && [ "$minor" -ge 4 ]; then echo "https://download.pytorch.org/whl/cu124"
    elif [ "$major" -eq 12 ] && [ "$minor" -ge 1 ]; then echo "https://download.pytorch.org/whl/cu121"
    elif [ "$major" -eq 11 ] && [ "$minor" -ge 8 ]; then echo "https://download.pytorch.org/whl/cu118"
    else                                                   echo "https://download.pytorch.org/whl/cu118"
    fi
}

# ==============================================================================
# STEP 1 — Check OS dependencies
# ==============================================================================
step1_check_os() {
    hdr "STEP 1 — Screening & provisioning system dependencies"
    inf "OS=$OS_KIND   arch=$ARCH   pkg-manager=${PKG:-none}   auto-install=$AUTO_DEPS"
    echo

    # ── macOS: ensure Homebrew is present (prerequisite for every Mac package) ─
    if $IS_MAC && [ -z "$PKG" ]; then
        if $AUTO_DEPS; then
            warn "Homebrew not installed — installing automatically …"
            NONINTERACTIVE=1 /bin/bash -c \
                "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
                && ok "Homebrew installed" \
                || warn "Homebrew installation failed — install manually: https://brew.sh"
            # Activate brew in the current shell (Apple Silicon vs Intel)
            [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
            [ -x /usr/local/bin/brew ]    && eval "$(/usr/local/bin/brew shellenv)"
            PKG="$(detect_pkgmgr)"
        else
            die "Homebrew is required on macOS. Install: https://brew.sh then retry."
        fi
    fi

    # ── Build tools: check, and auto-install whatever is missing via the pkg manager ─
    local need=()
    chk() {  # chk <command> <generic-tool> <desc>
        local cmd="$1" tool="$2" desc="$3"
        if command -v "$cmd" &>/dev/null; then
            ok "$(printf '%-12s' "$cmd")  $("$cmd" --version 2>&1 | head -1 | cut -c1-56)"
        else
            warn "$(printf '%-12s' "$cmd")  MISSING — $desc"
            need+=("$tool")
        fi
    }
    # On macOS, 'gcc' = Apple clang (fine for C); Fortran needs brew gcc.
    chk gcc      gcc      "C compiler (REAL, MatchLocate2, FDTCC)"
    chk gfortran gfortran "Fortran compiler (HypoDD/NLLoc/GrowClust/VELEST/Hypoinverse)"
    chk cmake    cmake    "Build system (NonLinLoc)"
    chk make     make     "Make utility"
    chk git      git      "Source downloader"
    chk wget     wget     "File downloader"

    if [ ${#need[@]} -gt 0 ]; then
        # Dedup distro package names
        local pkgs=() seen=" "
        local t p
        for t in "${need[@]}"; do
            for p in $(pkg_name "$t"); do
                case "$seen" in *" $p "*) ;; *) pkgs+=("$p"); seen="$seen$p ";; esac
            done
        done
        if $AUTO_DEPS && [ -n "$PKG" ]; then
            echo; inf "Auto-installing: ${pkgs[*]}  (via $PKG)"
            pkg_install "${pkgs[@]}" && ok "Build tools installed" \
                || warn "Some packages failed to install — check the messages above"
        else
            echo; warn "Missing packages. Install manually:"
            case "$PKG" in
                brew)            inf "  brew install ${pkgs[*]}" ;;
                apt-get)         inf "  sudo apt-get install -y ${pkgs[*]}" ;;
                dnf|yum|zypper)  inf "  sudo $PKG install -y ${pkgs[*]}" ;;
                pacman)          inf "  sudo pacman -Sy ${pkgs[*]}" ;;
                *)               inf "  (install packages: ${pkgs[*]})" ;;
            esac
            inf "Then re-run: bash install.sh"
            return 1
        fi
    fi

    # ── Python environment: conda (default) or venv ────────────────────────────
    echo
    if $USE_VENV; then
        if ! command -v python3 &>/dev/null; then
            if $AUTO_DEPS && [ -n "$PKG" ]; then
                inf "python3 missing — installing …"; pkg_install $(pkg_name python3)
            else
                err "python3 not found — install then retry"; return 1
            fi
        fi
        command -v python3 &>/dev/null && ok "$(printf '%-12s' python3)  $(python3 --version 2>&1)"
        # IMPORTANT: on Ubuntu/Debian `python3 -m venv --help` runs without python3-venv,
        # but the resulting venv has no pip. Test ensurepip instead.
        if ! python3 -c "import ensurepip" &>/dev/null 2>&1; then
            warn "venv/ensurepip module missing — the venv will be created without pip"
            if $AUTO_DEPS && [ "$PKG" = apt-get ]; then
                pkg_install python3-venv python3-pip && ok "python3-venv installed"
            fi
        fi
    else
        if [ -z "$CONDA" ]; then
            if $AUTO_DEPS; then
                warn "conda not found — bootstrapping Miniforge automatically …"
                if bootstrap_miniforge; then
                    :
                else
                    warn "Bootstrap failed — falling back to venv"
                    USE_VENV=true
                    command -v python3 &>/dev/null || { $AUTO_DEPS && [ -n "$PKG" ] && pkg_install $(pkg_name python3); }
                fi
            else
                err "conda not found"
                inf "  Use --venv, or let the installer bootstrap Miniforge automatically."
                return 1
            fi
        fi
        [ -n "$CONDA" ] && ok "$(printf '%-12s' conda)  $("$CONDA" --version 2>&1)"
    fi
    return 0
}

# ==============================================================================
# STEP 2 — Download source code
# ==============================================================================
step2_download() {
    hdr "STEP 2 — Downloading source code"

    git_clone() {
        local name="$1" url="$2" dest="$SRC_DIR/$3"
        # An interrupted clone can leave a dir containing only .git with no
        # checked-out files; treat that (and an empty dir) as "not cloned" so
        # the source is actually re-fetched instead of silently skipped.
        if [ -d "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null | grep -v '^\.git$')" ]; then
            ok "$(printf '%-16s' "$name")  already at $dest"
        else
            [ -d "$dest" ] && { warn "$name exists but has no checkout — re-cloning …"; rm -rf "$dest"; }
            inf "Cloning $name …"
            local _gc_ok=false
            for _attempt in 1 2 3; do
                [ "$_attempt" -gt 1 ] && { warn "Retry $_attempt/3 clone $name …"; rm -rf "$dest"; }
                if git -c http.version=HTTP/1.1 clone --depth=1 "$url" "$dest" 2>/dev/null; then
                    _gc_ok=true; break
                fi
            done
            if $_gc_ok; then
                ok "$(printf '%-16s' "$name")  → $dest"
            else
                err "$name clone failed after 3 attempts (check the network connection)"
            fi
        fi
    }

    wget_tar() {
        local name="$1" url="$2"
        local fname; fname="$(basename "$url")"
        local tar_path="$SRC_DIR/$fname"
        local ext_dir="$SRC_DIR/${fname%.tar.gz}"
        if [ -d "$ext_dir" ]; then
            ok "$(printf '%-16s' "$name")  already at $ext_dir"
            return
        fi
        if [ ! -f "$tar_path" ]; then
            inf "Downloading $name …"
            wget -q --show-progress "$url" -O "$tar_path" \
                && ok "$(printf '%-16s' "$name")  downloaded" \
                || { err "$name download failed"; return; }
        fi
        inf "Extracting $fname …"
        tar -xzf "$tar_path" -C "$SRC_DIR" \
            && ok "$(printf '%-16s' "$name")  extracted → $ext_dir" \
            || err "$name extraction failed — check $tar_path"
    }

    git_clone "REAL"          "https://github.com/Dal-mzhang/REAL.git"                      "REAL"
    git_clone "NonLinLoc"     "https://github.com/ut-beg-texnet/NonLinLoc.git"              "NonLinLoc"
    git_clone "GrowClust"     "https://github.com/dttrugman/GrowClust.git"                  "GrowClust"
    git_clone "MatchLocate2"  "https://github.com/Dal-mzhang/MatchLocate2.git"              "MatchLocate2"
    git_clone "FDTCC"         "https://github.com/MinLiu19/FDTCC.git"                       "FDTCC"
    git_clone "slinktool"     "https://github.com/EarthScope/slinktool.git"                 "slinktool"
    git_clone "slarchive"     "https://github.com/EarthScope/slarchive.git"                 "slarchive"
    git_clone "neic-glass3"   "https://github.com/usgs/neic-glass3.git"                     "neic-glass3"
    # FocoNet (Song et al. 2026, doi:10.1029/2025JH000879) — transformer focal
    # mechanism, used alongside SKHASH in the Mechanism step. Pure Python, no
    # compile needed: the repo's own layout (FocoNet_Full/, FocoNet_O/,
    # FocoNet_SP/ at its root, each with model.py + a committed model/*.pth
    # checkpoint) is EXACTLY what seiswork/modules/mechanism/foconet_runner.py
    # expects at core/src/FocoNet/ — cloning is the entire install step.
    git_clone "FocoNet"       "https://github.com/xhsongstanford/FocoNet.git"               "FocoNet"
    wget_tar  "HypoDD"        "http://www.ldeo.columbia.edu/~felixw/HYPODD/HYPODD_1.3.tar.gz"

    echo
    inf  "VELEST & Hypoinverse — source is BUNDLED in core/src/ (velest, hypo71);"
    inf  "  compiled automatically in Step 3 (gfortran). No manual download needed."
}

# ==============================================================================
# STEP 3 — Compile binaries
# ==============================================================================
step3_compile() {
    hdr "STEP 3 — Compiling binaries"

    # ── REAL (C) ──────────────────────────────────────────────────────────────
    # NOTE: trailing '|| true' — under 'set -euo pipefail' a missing source dir makes
    # 'find' exit non-zero, pipefail propagates it to the assignment, and set -e would
    # abort the ENTIRE compile step before the '[ -z ]' guard below can warn+skip.
    local real_c; real_c="$(find "$SRC_DIR/REAL/src" -name "REAL.c" 2>/dev/null | head -1)" || true
    if [ -z "$real_c" ]; then
        warn "REAL source not found — run Step 2 first"
    else
        inf "Compiling REAL …"
        gcc -O2 -o "$BIN_DIR/REAL" "$real_c" -lm \
            && { chmod +x "$BIN_DIR/REAL"; rm -f "$HOME_BIN/REAL"; ln -sf "$BIN_DIR/REAL" "$HOME_BIN/REAL"
                 ok "REAL → $BIN_DIR/REAL"; } \
            || err "REAL compile failed"
    fi

    # ── slinktool (C) — SeedLink query/dump tool, EarthScope's own build ──────
    # Bundled as a main dependency (not just relying on the copy SeisComP ships)
    # so channel-matching/verification works even without a full SeisComP
    # install, and always gets a known-good, current version.
    local slt_src="$SRC_DIR/slinktool"
    if [ ! -f "$slt_src/Makefile" ]; then
        warn "slinktool source not found — run Step 2 first"
    else
        inf "Compiling slinktool …"
        if make -C "$slt_src" -s 2>/dev/null && [ -f "$slt_src/slinktool" ]; then
            install_bin "$slt_src/slinktool" "slinktool"
        else
            err "slinktool compile failed"
        fi
    fi

    # ── slarchive (C) — SeedLink archiving daemon, EarthScope's own build ─────
    # Bundled for the same reason as slinktool: a known-good version that
    # doesn't depend on a full SeisComP install. Writes into work/online_sds
    # (see online_sds_path() in _realtime_pipeline.py) when SeisComP's own
    # scarchive isn't already archiving.
    local sla_src="$SRC_DIR/slarchive"
    if [ ! -f "$sla_src/Makefile" ]; then
        warn "slarchive source not found — run Step 2 first"
    else
        inf "Compiling slarchive …"
        if make -C "$sla_src" -s 2>/dev/null && [ -f "$sla_src/slarchive" ]; then
            install_bin "$sla_src/slarchive" "slarchive"
        else
            err "slarchive compile failed"
        fi
    fi

    # ── HypoDD + ph2dt (Fortran) ──────────────────────────────────────────────
    local hdd_make; hdd_make="$(find "$SRC_DIR" -maxdepth 3 -path "*/HYPODD*/src/Makefile" 2>/dev/null | head -1)"
    if [ -z "$hdd_make" ]; then
        warn "HypoDD source not found — run Step 2 first"
    else
        local hdd_src; hdd_src="$(dirname "$hdd_make")"
        local gf; gf="$(command -v gfortran)"
        inf "Compiling HypoDD + ph2dt …"
        find "$hdd_src" -name "Makefile" -print0 \
            | while IFS= read -r -d '' mk; do
                  sedi "s|^FC[[:space:]]*=[[:space:]]*g77|FC = $gf|g; s|^FC[[:space:]]*=[[:space:]]*f77|FC = $gf|g" "$mk" 2>/dev/null || true
              done
        for sub in hypoDD ph2dt; do
            if make -C "$hdd_src/$sub" -s 2>/dev/null; then
                local bin_out; bin_out="$(find "$hdd_src/$sub" -name "$sub" -type f 2>/dev/null | head -1)"
                if [ -n "$bin_out" ]; then
                    cp -f "$bin_out" "$BIN_DIR/$sub"
                    chmod +x "$BIN_DIR/$sub"
                    rm -f "$HOME_BIN/$sub"; ln -sf "$BIN_DIR/$sub" "$HOME_BIN/$sub"
                    ok "$sub → $BIN_DIR/$sub"
                fi
            else
                err "$sub compile failed"
            fi
        done
    fi

    # ── Legacy Fortran flags (VELEST/Hypoinverse) ─────────────────────────────
    # Old Fortran code: gfortran 10+ turns an argument mismatch into an ERROR;
    # -fallow-argument-mismatch (gf>=10 only) turns it back into a warning.
    local FF_LEGACY="-O2 -w -std=legacy -ffixed-line-length-none"
    local gfmaj; gfmaj="$(gfortran -dumpversion 2>/dev/null | cut -d. -f1 || echo 0)"
    [ "${gfmaj:-0}" -ge 10 ] 2>/dev/null && FF_LEGACY="$FF_LEGACY -fallow-argument-mismatch" || true

    # ── VELEST (Fortran) — source is BUNDLED in core/src/velest ──────────────
    # vel_com.f is an include file (used via `include 'vel_com.f'` in velest.f), so
    # it is enough to compile velest.f from inside its own directory.
    local vel_src="$SEISWORK_DIR/core/src/velest"
    if [ -f "$vel_src/velest.f" ]; then
        inf "Compiling VELEST (bundled source) …"
        if ( cd "$vel_src" && rm -f velest && gfortran $FF_LEGACY -o velest velest.f ); then
            install_bin "$vel_src/velest" "velest"
        else
            err "VELEST compile failed"
        fi
    else
        warn "VELEST source not found at $vel_src — skipping"
    fi

    # ── Hypoinverse hyp1.40 (Fortran) — source is BUNDLED in core/src/hypo71 ──
    local hyp_src="$SEISWORK_DIR/core/src/hypo71/source"
    if [ -f "$hyp_src/makefile" ]; then
        inf "Compiling Hypoinverse (hyp1.40, bundled source) …"
        if ( cd "$hyp_src"
             objs="$(awk '/^hyp1\.40[[:space:]]*:/{f=1} f{print} /-o hyp1\.40/{f=0}' makefile \
                     | grep -oE '[A-Za-z0-9_]+\.o' | sort -u)"
             [ -n "$objs" ] || { echo "makefile: object list is empty"; exit 1; }
             rm -f ./*.o hyp1.40
             for o in $objs; do
                 base="${o%.o}"; src=""
                 if   [ -f "$base.for" ]; then src="$base.for"
                 elif [ -f "$base.f"   ]; then src="$base.f"
                 else echo "source for $o not found"; exit 1; fi
                 gfortran $FF_LEGACY -c "$src" || exit 1
             done
             gfortran $FF_LEGACY -o hyp1.40 $objs || exit 1 ); then
            install_bin "$hyp_src/hyp1.40" "hypoinverse"
        else
            err "Hypoinverse compile failed"
        fi
    else
        warn "Hypoinverse source not found at $hyp_src — skipping"
    fi

    # ── NonLinLoc suite (C, cmake) ─────────────────────────────────────────────
    # IMPORTANT: CMakeLists.txt lives in src/, not the NonLinLoc/ root
    # (lesson: cmake .. from the root fails since there is no CMakeLists.txt there)
    local nll_src="$SRC_DIR/NonLinLoc/src"
    if [ ! -f "$nll_src/CMakeLists.txt" ]; then
        warn "NonLinLoc source not found — run Step 2 first"
    else
        local nll_build="$SRC_DIR/NonLinLoc/build_seiswork"
        inf "Compiling NonLinLoc (cmake from src/) …"
        rm -rf "$nll_build" && mkdir -p "$nll_build"
        if cmake -S "$nll_src" -B "$nll_build" -DCMAKE_BUILD_TYPE=Release -Wno-dev -Wno-deprecated 2>/dev/null; then
            make -C "$nll_build" -j"$(NPROC)" 2>/dev/null || true
            local nll_bindir="$nll_src/bin"
            for b in NLLoc Grid2Time Vel2Grid Time2EQ PhsAssoc; do
                if [ -f "$nll_bindir/$b" ]; then
                    cp -f "$nll_bindir/$b" "$BIN_DIR/$b"
                    chmod +x "$BIN_DIR/$b"
                    rm -f "$HOME_BIN/$b"; ln -sf "$BIN_DIR/$b" "$HOME_BIN/$b"
                    ok "$b → $BIN_DIR/$b"
                else
                    warn "$b not found after cmake build (may not compile on this target)"
                fi
            done
        else
            err "NonLinLoc cmake configure failed"
        fi
    fi

    # ── NLLoc travel-time grid GLOBAL (IASP91) — BUNDLED in the repo ──────────
    # config/nlloc_grids/global/ ships via `git clone` (not built here), used to
    # refine realtime hypocenters (GaMMA → NonLinLoc) without an absolute ~/apps/NLLoc path.
    local nll_grid="$SEISWORK_DIR/config/nlloc_grids/global"
    if ls "$nll_grid"/iasp91.P.*.time.hdr >/dev/null 2>&1; then
        ok "Global NLLoc IASP91 grid available (bundled): $nll_grid"
    else
        warn "Global NLLoc grid not found at $nll_grid — realtime refine falls back to the GaMMA location"
    fi

    # ── GrowClust (Fortran, Makefile) ─────────────────────────────────────────
    # '|| true' so a missing GrowClust source dir doesn't abort the step under set -e/pipefail
    local gc_make; gc_make="$(find "$SRC_DIR/GrowClust" -name "Makefile" 2>/dev/null | head -1)" || true
    if [ -z "$gc_make" ]; then
        warn "GrowClust source not found — run Step 2 first"
    else
        local gc_src; gc_src="$(dirname "$gc_make")"
        inf "Compiling GrowClust …"
        if make -C "$gc_src" -s 2>/dev/null; then
            local gc_bin; gc_bin="$(find "$gc_src" -name "growclust" -type f 2>/dev/null | head -1)"
            if [ -n "$gc_bin" ]; then
                cp -f "$gc_bin" "$BIN_DIR/growclust"
                chmod +x "$BIN_DIR/growclust"
                rm -f "$HOME_BIN/growclust"; ln -sf "$BIN_DIR/growclust" "$HOME_BIN/growclust"
                ok "growclust → $BIN_DIR/growclust"
            fi
        else
            err "GrowClust compile failed"
        fi
    fi

    # ── MatchLocate2 + SelectFinal + SHIFT (C) ────────────────────────────────
    # IMPORTANT: the original Makefile uses gcc-10 (Mac) and external -lsac/-lsacio
    # which is unavailable on Linux without SAC installed.
    # Fix: swap gcc-10 → gcc, drop the external SAC dep (sacio.c already ships in src)
    local ml2_src="$SRC_DIR/MatchLocate2/src"
    if [ ! -d "$ml2_src" ]; then
        warn "MatchLocate2 source not found — run Step 2 first"
    else
        inf "Fixing MatchLocate2 Makefile (gcc-10→gcc, dropping the external SAC dep) …"
        # NOTE: -mcmodel=medium is only valid on x86_64; on aarch64/arm64 → -mcmodel=tiny|small|large
        local _ml2_mcmodel=""
        case "$ARCH" in x86_64|amd64) _ml2_mcmodel="-mcmodel=medium" ;; esac
        cat > "$ml2_src/Makefile" << MLEOF
CC = gcc -Os ${_ml2_mcmodel} -fopenmp -w
LIBS = -lm
CFLAGS =

all: MatchLocate2 SelectFinal SHIFT lsac ccsacc clean

BIN = ../bin

MatchLocate2: MatchLocate2.o sacio.o
	\$(CC) -o \$(BIN)/\$@ \$^ \$(LIBS)

SelectFinal: SelectFinal.o
	\$(CC) -o \$(BIN)/\$@ \$^ \$(LIBS)

SHIFT: SHIFT.o
	\$(CC) -o \$(BIN)/\$@ \$^ \$(LIBS)

lsac: lsac.o sacio.o
	\$(CC) -o \$(BIN)/\$@ \$^ \$(LIBS)

ccsacc: ccsacc.o sacio.o
	\$(CC) -o \$(BIN)/\$@ \$^ \$(LIBS)

clean:
	rm -f *.o
MLEOF
        mkdir -p "$SRC_DIR/MatchLocate2/bin"
        # The MatchLocate2 main program needs the 'xapiir' subroutine from the external SAC library
        # (see src/README). Companion tools (SelectFinal, SHIFT, lsac, ccsacc)
        # do not need it. A partial compile is expected — don't abort if MatchLocate2's
        # main fails as long as the other tools succeed.
        make -C "$ml2_src" 2>/dev/null || true
        local _ml2_installed=0
        for b in MatchLocate2 SelectFinal SHIFT lsac ccsacc; do
            local bpath="$SRC_DIR/MatchLocate2/bin/$b"
            if [ -f "$bpath" ]; then
                install_bin "$bpath" "$b"; _ml2_installed=$((_ml2_installed+1))
            elif [ "$b" = "MatchLocate2" ]; then
                warn "MatchLocate2 main failed to compile — likely missing 'xapiir' (needs the full SAC library)"
                inf  "  MatchLocate2 main is OPTIONAL — the other tools (SelectFinal/SHIFT/lsac/ccsacc) still install"
                inf  "  To install SAC: https://ds.iris.edu/ds/nodes/dmc/forms/sac/"
            else
                warn "$b not found after compile"
            fi
        done
        [ "$_ml2_installed" -eq 0 ] && err "MatchLocate2: every tool failed to compile — check gcc + libgomp1"
    fi

    # ── FDTCC (C) ─────────────────────────────────────────────────────────────
    # IMPORTANT: the original FDTCC Makefile uses 'gcc-9' (macOS Homebrew) and -lsac/-lsacio
    # (external SAC library). The precompiled binary in bin/FDTCC is Mach-O x86_64
    # (macOS), which can't run on Linux. Fix: rewrite the Makefile to use the system gcc +
    # a local sacio.a. The xapiir (filter) subroutine still needs SAC libs — on failure,
    # FDTCC is OPTIONAL: picking + location features still work without it.
    local fdtcc_src
    fdtcc_src="$(find "$SRC_DIR/FDTCC" -name "*.c" -path "*/src/*" 2>/dev/null | head -1 | xargs dirname 2>/dev/null || echo "")"
    if [ -z "$fdtcc_src" ]; then
        fdtcc_src="$(find "$SRC_DIR/FDTCC" -name "Makefile" 2>/dev/null | head -1 | xargs dirname 2>/dev/null || echo "")"
    fi
    if [ -z "$fdtcc_src" ]; then
        warn "FDTCC source not found — run Step 2 first"
    elif has_bin FDTCC; then
        ok "FDTCC already present — skip"
    else
        inf "Fixing FDTCC Makefile (gcc-9→gcc, using a local sacio.a) …"
        cat > "$fdtcc_src/Makefile" << 'FDEOF'
CC = gcc -Os -fopenmp -w
LIBS = -lm
BIN = ../bin

FDTCC: FDTCC.o
	$(CC) -o $(BIN)/$@ FDTCC.o sacio.a $(LIBS)

clean:
	rm -f *.o
FDEOF
        mkdir -p "$SRC_DIR/FDTCC/bin"
        if make -C "$fdtcc_src" 2>/dev/null; then
            local fdtcc_bin; fdtcc_bin="$(find "$SRC_DIR/FDTCC" -name "FDTCC" -type f -perm -u+x 2>/dev/null | head -1)"
            [ -n "$fdtcc_bin" ] && install_bin "$fdtcc_bin" "FDTCC" || warn "FDTCC binary not found after compile"
        else
            warn "FDTCC compile failed — likely 'xapiir' not found (needs the full SAC library)"
            inf  "  FDTCC is OPTIONAL — picking + location still work without it"
            inf  "  To install SAC: https://ds.iris.edu/ds/nodes/dmc/forms/sac/"
        fi
    fi

    # ── neic-glass3 (C++, cmake) — glass-app, USGS NEIC grid/Bayesian stacking ──
    # associator (see seiswork/modules/associator/glass3.py). Built with
    # BUILD_GLASS-BROKER-APP=OFF: no Kafka/ActiveMQ messaging deps needed —
    # SeisWork drives glass-app as a one-shot batch subprocess (file input/
    # output), never as the broker-based realtime daemon it was designed for.
    #
    # Two known upstream source bugs (confirmed against a fresh clone, GCC 11)
    # need patching before this compiles: util/include/threadpool.h and
    # glasscore/glasslib/include/Web.h both use std::function without
    # #include <functional> — worked with older/looser libstdc++ include
    # chains, fails now with "error: 'std::function' has not been declared".
    # Patch is idempotent (checks first) so re-running install.sh is safe.
    local glass3_root="$SRC_DIR/neic-glass3"
    if [ ! -f "$glass3_root/CMakeLists.txt" ]; then
        warn "neic-glass3 source not found — run Step 2 first"
    elif has_bin glass-app; then
        ok "glass-app already present — skip"
    else
        inf "Patching neic-glass3 for GCC 11+ (missing #include <functional>) …"
        # Anchor on the include-guard '#define ..._H' line, not a specific
        # #include — threadpool.h has no '#include <atomic>' line at all
        # (only Web.h does), so anchoring there silently patched Web.h only
        # and left threadpool.h broken (confirmed: install.sh --step 3 failed
        # to compile 'util' with this bug still present).
        for f in "$glass3_root/util/include/threadpool.h" \
                 "$glass3_root/glasscore/glasslib/include/Web.h"; do
            if [ -f "$f" ] && ! grep -q '#include <functional>' "$f"; then
                # Insert after the include guard. Use awk, not `sed .../a` — BSD
                # sed (macOS) rejects the one-line `a text` form GNU sed accepts
                # ("command a expects \ followed by text") and would abort here.
                awk '{print}
                     /^#define [A-Za-z_]*_H$/ && !ins {print "#include <functional>"; ins=1}' \
                    "$f" > "$f.swtmp" && mv "$f.swtmp" "$f"
            fi
        done

        local glass3_build="$glass3_root/build_seiswork"
        local glass3_install="$glass3_root/build-install"
        rm -rf "$glass3_build" && mkdir -p "$glass3_build"
        inf "Compiling neic-glass3 (glass-app, cmake) …"
        # neic-glass3 quirks on a modern toolchain:
        #  - Its CMakeLists asks for cmake_minimum_required < 3.5, which CMake 4.x
        #    refuses. CMAKE_POLICY_VERSION_MINIMUM=3.5 lets it configure — and it
        #    must be EXPORTED (not just -D on the top configure) so the nested
        #    ExternalProject sub-builds (SuperEasyJSON, DetectionFormats, …)
        #    inherit it when they configure during `make`.
        #  - GIT_CLONE_PUBLIC defaults OFF → it clones deps over SSH
        #    (git@github.com:) and stalls on ssh-askpass; ON forces https.
        #    DetectionFormats is still fetched from github at build time, so this
        #    step needs network; it fails cleanly (below) if github is unreachable.
        export CMAKE_POLICY_VERSION_MINIMUM=3.5
        if cmake -S "$glass3_root" -B "$glass3_build" \
                 -DBUILD_GLASS-APP=ON -DBUILD_GLASS-BROKER-APP=OFF -DRUN_TESTS=OFF \
                 -DCMAKE_BUILD_TYPE=Release \
                 -DGIT_CLONE_PUBLIC=ON \
                 -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
                 -DCMAKE_INSTALL_PREFIX="$glass3_install" \
                 -Wno-dev 2>/dev/null; then
            make -C "$glass3_build" -j"$(NPROC)" 2>/dev/null || true
            local glass3_bin="$glass3_install/glass-app/glass-app"
            # The nested ExternalProject builds can lose a race on the first
            # PARALLEL pass while deps are still downloading; a second, serial
            # make reliably finishes glass-app once the deps are in place.
            [ -f "$glass3_bin" ] || make -C "$glass3_build" 2>/dev/null || true
            if [ -f "$glass3_bin" ]; then
                install_bin "$glass3_bin" "glass-app"
            else
                err "glass-app compile failed (binary not found after make)"
            fi
        else
            err "neic-glass3 cmake configure failed"
        fi

        # Travel-time tables (P.trv/S.trv) bundled repo-relative into config/
        # (same principle as REAL's config/ttdb.txt) so Glass3Associator never
        # needs an absolute path into core/src/neic-glass3.
        local glass3_tt_src="$glass3_root/glasscore/traveltime/tt-files"
        local glass3_tt_dst="$SEISWORK_DIR/config/glass3_tt"
        if [ -f "$glass3_tt_src/P.trv" ] && [ -f "$glass3_tt_src/S.trv" ]; then
            mkdir -p "$glass3_tt_dst"
            cp -f "$glass3_tt_src/P.trv" "$glass3_tt_src/S.trv" "$glass3_tt_dst/"
            ok "glass3 travel-time tables → $glass3_tt_dst"
        else
            warn "neic-glass3 tt-files not found at $glass3_tt_src — Glass3Associator will fail to run until this is fixed (re-run Step 2/3)"
        fi
    fi

    # (VELEST & Hypoinverse are compiled from core/src/ in the block above — see
    #  the "VELEST (Fortran)" & "Hypoinverse hyp1.40" sections after HypoDD.)

    # '|| true' — the Docker fallback is an OPTIONAL bonus for engines that didn't
    # compile natively. It returns 1 when Docker is installed but the image build
    # fails; without this guard that non-zero would (under set -e) abort the whole
    # installer right after native compilation — before Python deps (step 5) etc.
    step3b_docker_fallback || true
}

# ── Step 3b — Docker fallback for whichever binaries above didn't compile ────
# Native compilation stays the primary path (no per-call container overhead —
# matters for the live/online pipeline, which calls NLLoc repeatedly). Docker
# only fills the gaps: a binary missing from core/bin/ after step 3 (compiler
# not installed, source needs a lib this OS doesn't have, ...) gets a wrapper
# script that runs it inside the seiswork-corebin image instead, so the
# feature keeps working cross-OS without requiring a local toolchain.
step3b_docker_fallback() {
    if ! command -v docker &>/dev/null; then
        inf "Docker not found — skipping the Docker fallback for any binary above that didn't compile."
        inf "  Install Docker for a cross-OS fallback: https://docs.docker.com/get-docker/"
        return 0
    fi

    hdr "STEP 3b — Docker fallback for binaries that didn't compile natively"
    local wrap_dir="$CORE_DIR/bin-docker"
    # NOTE: the XXXXXX must be at the END of the template — BSD/macOS mktemp does NOT
    # substitute X's followed by a suffix (e.g. '_XXXX.log'); it creates that literal
    # filename, which then fails the next run with "File exists" and leaves build_log
    # empty (→ 'redirect: No such file or directory', misreported as a Docker failure).
    local build_log; build_log="$(mktemp "${TMPDIR:-/tmp}/seiswork_corebin_build.XXXXXX")"
    if ! bash "$SEISWORK_DIR/docker/build_corebin.sh" --wrappers "$wrap_dir" >"$build_log" 2>&1; then
        warn "seiswork-corebin Docker build failed — see $build_log"
        return 1
    fi
    ok "seiswork-corebin Docker image built — wrappers in $wrap_dir"

    local fell_back=()
    local b
    for b in $(ls "$wrap_dir" 2>/dev/null); do
        if [ -e "$BIN_DIR/$b" ]; then
            continue   # native binary already compiled — it wins, leave it alone
        fi
        link_bin "$wrap_dir/$b" "$b"
        fell_back+=("$b")
    done

    if [ "${#fell_back[@]}" -gt 0 ]; then
        ok "Using Docker for: ${fell_back[*]}  (native compile failed or was unavailable for these)"
    else
        ok "All binaries compiled natively — the Docker fallback isn't needed for any of them."
    fi
}

# ==============================================================================
# STEP 4 — Create Python environment (conda or venv)
# ==============================================================================
step4_create_env() {
    if $USE_VENV; then
        hdr "STEP 4 — Creating Python venv at $VENV_DIR"
        # IMPORTANT: a venv created without python3-venv (Ubuntu/Debian) will have
        # python but no pip → every later pip install fails with
        # "No such file or directory: $VENV_DIR/bin/pip". Auto-rebuild when broken.
        if [ -d "$VENV_DIR" ] && ! is_venv_valid "$VENV_DIR"; then
            warn "venv at $VENV_DIR has no pip — rebuilding"
            if $AUTO_DEPS && [ "$PKG" = apt-get ] && ! python3 -c "import ensurepip" &>/dev/null; then
                inf "Installing python3-venv + python3-pip …"
                pkg_install python3-venv python3-pip || warn "pkg_install failed"
            fi
            rm -rf "$VENV_DIR"
        fi
        if [ -d "$VENV_DIR" ]; then
            ok "venv already exists at $VENV_DIR"
            inf "To rebuild: rm -rf $VENV_DIR && bash install.sh --venv --step 4,5"
            # Repair venvs created before this script passed --system-site-packages
            # (see below) — python reads this cfg value on every startup, so
            # flipping it in place fixes `import gi` without recreating the venv.
            local pycfg="$VENV_DIR/pyvenv.cfg"
            if [ -f "$pycfg" ] && grep -q "^include-system-site-packages = false" "$pycfg"; then
                inf "Enabling system-site-packages on existing venv (needed for PyGObject/GTK) …"
                sed -i.bak "s/^include-system-site-packages = false/include-system-site-packages = true/" "$pycfg" \
                    && rm -f "$pycfg.bak" \
                    && ok "venv now sees system site-packages (python3-gi, etc.)"
            fi
        else
            # --system-site-packages: PyGObject (the `gi` module used by pywebview's
            # GTK backend on Linux) has no reliable pip wheel — it's meant to be used
            # via the distro's python3-gi package. Without this flag the venv is
            # isolated from that system package and `seiswork gui --native` dies with
            # "ModuleNotFoundError: No module named 'gi'" even though python3-gi is
            # installed on the host (confirmed 2026-07-06).
            inf "Creating venv (--system-site-packages, for system PyGObject/GTK) …"
            python3 -m venv --system-site-packages "$VENV_DIR" \
                && ok "venv created: $VENV_DIR" \
                || die "python3 -m venv failed — install: sudo apt-get install python3-venv"
            # Double-check: venv exists but pip is still missing? Give an explicit hint
            if ! is_venv_valid "$VENV_DIR"; then
                rm -rf "$VENV_DIR"
                die "venv created without pip — install python3-venv: sudo apt-get install python3-venv python3-pip"
            fi
        fi
        inf "Upgrade pip …"
        pip_venv --upgrade pip wheel setuptools -q \
            && ok "pip upgraded" \
            || warn "pip upgrade failed (not fatal)"
    else
        hdr "STEP 4 — Creating conda environment '$ENV_NAME'"
        if "$CONDA" env list 2>/dev/null | grep -q "^$ENV_NAME\b"; then
            ok "Environment '$ENV_NAME' already exists"
            inf "To rebuild from scratch: conda env remove -n $ENV_NAME && bash install.sh --step 4,5"
            # IMPORTANT: don't just skip — a pre-existing env may be leftover from
            # an install that got interrupted partway through Step 5 (network drop,
            # Ctrl-C, OOM-killed conda-forge solve, ...), missing packages that
            # environment.yml declares. 'env update' (no --prune) is additive-only:
            # it installs/upgrades whatever environment.yml lists but never removes
            # packages, so it's safe to run on every install.sh invocation.
            [ ! -f "$ENV_YML" ] && die "environment.yml not found at $ENV_YML"
            inf "Reconciling '$ENV_NAME' with environment.yml (conda env update, additive only) …"
            "$CONDA" env update -f "$ENV_YML" --name "$ENV_NAME" \
                && ok "Environment '$ENV_NAME' up to date" \
                || warn "conda env update failed — the env may still be missing some packages"
        else
            [ ! -f "$ENV_YML" ] && die "environment.yml not found at $ENV_YML"
            inf "Creating '$ENV_NAME' from environment.yml …"
            inf "(conda-forge: obspy, cartopy, numba, scikit-learn — may take 5–10 min)"
            "$CONDA" env create -f "$ENV_YML" --name "$ENV_NAME" \
                || die "conda env create failed"
            ok "Environment '$ENV_NAME' created"
        fi
    fi
}

# ==============================================================================
# STEP 5 — Install Python packages
# ==============================================================================
step5_python() {
    hdr "STEP 5 — Installing Python packages"

    # ── Detect CUDA ───────────────────────────────────────────────────────────
    local cuda_ver; cuda_ver="$(detect_cuda)"
    local whl_url;  whl_url="$(cuda_to_whl "$cuda_ver")"
    if [ -n "$cuda_ver" ]; then
        ok "GPU detected: CUDA $cuda_ver  →  whl: $whl_url"
    else
        warn "No GPU found — installing PyTorch CPU-only"
        inf  "(picking will be slower; for GPU install the NVIDIA driver + CUDA)"
    fi

    if $USE_VENV; then
        # ── venv: install requirements.txt first ──────────────────────────────
        if [ -f "$SEISWORK_DIR/requirements.txt" ]; then
            inf "Installing requirements.txt (--retries 5 --timeout 120) …"
            pip_venv -r "$SEISWORK_DIR/requirements.txt" -q \
                && ok "requirements.txt installed" \
                || err "requirements.txt install failed — retry: bash install.sh --venv --step 5"
        fi

        # ── PyTorch (CPU or GPU per detection) ─────────────────────────────────
        if "$VENV_DIR/bin/python" -c "import torch" &>/dev/null 2>&1; then
            local tv; tv="$("$VENV_DIR/bin/python" -c "import torch; print(torch.__version__)")"
            ok "PyTorch already installed ($tv)"
        else
            inf "Installing PyTorch (index: $whl_url) …"
            pip_venv torch --index-url "$whl_url" -q \
                && ok "PyTorch installed" \
                || err "PyTorch install failed — try manually: pip install torch --index-url $whl_url"
        fi

        # IMPORTANT: torch can downgrade setuptools (a torch 2.x bug)
        # Reinstall the latest setuptools after torch
        inf "Reinstalling setuptools (guard against a torch downgrade) …"
        pip_venv --upgrade setuptools -q 2>/dev/null || true

        # ── seisbench ─────────────────────────────────────────────────────────
        if "$VENV_DIR/bin/python" -c "import seisbench" &>/dev/null 2>&1; then
            ok "seisbench already installed"
        else
            inf "Installing seisbench …"
            pip_venv seisbench -q \
                && ok "seisbench installed" \
                || err "seisbench install failed"
        fi

        # ── SeisWork package itself (editable install) ────────────────────────
        inf "Installing seiswork package (editable) …"
        pip_venv -e "$SEISWORK_DIR" -q \
            && ok "seiswork package installed" \
            || err "seiswork package install failed"

        # ── GaMMA (optional) ──────────────────────────────────────────────────
        if "$VENV_DIR/bin/python" -c "from gamma.utils import association" &>/dev/null 2>&1; then
            ok "GaMMA already installed"
        else
            inf "Installing GaMMA from GitHub (optional — skip on failure) …"
            pip_venv "git+https://github.com/AI4EPS/GaMMA.git" -q 2>/dev/null \
                && ok "GaMMA installed" \
                || warn "GaMMA failed — associator --method gamma will be unavailable"
        fi

        # ── SKHASH (focal mechanism, optional) — --no-deps, see the note in
        # conda mode below (pinning numpy<2.0 for SKHASH isn't really needed).
        if "$VENV_DIR/bin/python" -c "import SKHASH" &>/dev/null 2>&1; then
            ok "SKHASH already installed"
        else
            inf "Installing SKHASH (focal mechanism / HASH, optional) …"
            pip_venv "--no-deps SKHASH" -q 2>/dev/null \
                && ok "SKHASH installed" \
                || warn "SKHASH failed — the Mechanism step (focal mechanism) will be unavailable"
        fi

        # ── PyOcto (associator, optional) — --no-deps for the SAME reason as
        # SKHASH above: pyocto's own metadata pins numpy<2.0, but it runs fine
        # on numpy>=2 (verified 2026-07-05); letting pip resolve deps normally
        # would silently downgrade numpy and break xarray/simulflow elsewhere.
        if "$VENV_DIR/bin/python" -c "import pyocto" &>/dev/null 2>&1; then
            ok "PyOcto already installed"
        else
            inf "Installing PyOcto (associator, optional) …"
            pip_venv "--no-deps pyocto" -q 2>/dev/null \
                && ok "PyOcto installed" \
                || warn "PyOcto failed — associator --method pyocto will be unavailable"
        fi

        # ── pyrocko (FocoNet focal mechanism, optional) — --no-deps, same
        # numpy<2 regression as SKHASH/PyOcto above (verified 2026-07-05: a
        # plain `pip install pyrocko` resolves numpy back to 1.26.x even
        # though pyrocko itself runs fine on numpy>=2).
        if "$VENV_DIR/bin/python" -c "import pyrocko" &>/dev/null 2>&1; then
            ok "pyrocko already installed"
        else
            inf "Installing pyrocko (FocoNet focal mechanism, optional) …"
            pip_venv "--no-deps pyrocko" -q 2>/dev/null \
                && ok "pyrocko installed" \
                || warn "pyrocko failed — FocoNet focal mechanism will be unavailable"
        fi

    else
        # ── conda mode ────────────────────────────────────────────────────────
        local torch_ok=false
        if crun bash -c "PYTHONNOUSERSITE=1 python -c \
            'import torch; assert torch.cuda.is_available()'" &>/dev/null; then
            local tv; tv="$(crun bash -c "PYTHONNOUSERSITE=1 python -c 'import torch; print(torch.__version__)'")"
            ok "PyTorch already installed, GPU=True  ($tv)"
            torch_ok=true
        fi

        if ! $torch_ok; then
            inf "Installing PyTorch (index: $whl_url) …"
            crun bash -c "PYTHONNOUSERSITE=1 \"\$CONDA_PREFIX/bin/python\" -m pip install $PIP_FLAGS \
                torch torchvision torchaudio \
                --index-url $whl_url" \
                && ok "PyTorch installed" \
                || { err "PyTorch install failed"; warn "Try: conda run -n $ENV_NAME pip install torch --index-url $whl_url"; }
        fi

        # Guard against torch downgrading setuptools
        crun bash -c "PYTHONNOUSERSITE=1 \"\$CONDA_PREFIX/bin/python\" -m pip install $PIP_FLAGS --upgrade setuptools -q" 2>/dev/null || true

        cpip "bottleneck" 2>/dev/null || true

        # ── Slab2 deps (xarray/netCDF4/bs4) — for an existing conda env ───────
        if crun bash -c "PYTHONNOUSERSITE=1 python -c 'import xarray, netCDF4, bs4'" &>/dev/null; then
            ok "slab deps (xarray/netCDF4/beautifulsoup4) already present"
        else
            inf "Installing slab deps (xarray, netCDF4, beautifulsoup4) via conda-forge …"
            "$CONDA" install -n "$ENV_NAME" -c conda-forge -y xarray netcdf4 beautifulsoup4 &>/dev/null \
                && ok "slab deps installed" \
                || { cpip "xarray netCDF4 beautifulsoup4" && ok "slab deps installed (pip)" \
                     || warn "slab deps failed — the Slab2 auto-contour feature is disabled"; }
        fi

        if crun bash -c "PYTHONNOUSERSITE=1 python -c 'import seisbench.models'" &>/dev/null; then
            ok "seisbench already installed"
        else
            inf "Installing seisbench …"
            cpip "seisbench" && ok "seisbench installed" || err "seisbench install failed"
        fi

        if crun bash -c "PYTHONNOUSERSITE=1 python -c 'from gamma.utils import association'" &>/dev/null; then
            ok "GaMMA already installed"
        else
            inf "Installing GaMMA from GitHub …"
            cpip "git+https://github.com/AI4EPS/GaMMA.git" \
                && ok "GaMMA installed" \
                || {
                    warn "GitHub install failed — trying local …"
                    local gamma_local="$HOME/apps/smartworkflow/core/GaMMA"
                    if [ -d "$gamma_local" ]; then
                        cpip "-e $gamma_local" && ok "GaMMA installed (local)" || err "GaMMA failed"
                    else
                        err "GaMMA not installed"
                    fi
                }
        fi

        # ── SKHASH (focal mechanism inversion, optional) ───────────────────────
        # Pure Python (no Fortran/compiler needed — the optional Fortran
        # speedup subroutine is intentionally NOT enabled here, see manual).
        # --no-deps: SKHASH's own pyproject.toml pins numpy<2.0, but it runs
        # fine on numpy>=2 (verified 2026-06-22) — letting pip resolve deps
        # normally would silently downgrade numpy and break xarray/simulflow
        # (Imaging step) elsewhere in this same env.
        if crun bash -c "PYTHONNOUSERSITE=1 python -c 'import SKHASH'" &>/dev/null; then
            ok "SKHASH already installed"
        else
            inf "Installing SKHASH (focal mechanism / HASH) …"
            cpip "--no-deps SKHASH" && ok "SKHASH installed" \
                || warn "SKHASH failed — the Mechanism step (focal mechanism) will be unavailable"
        fi

        # ── PyOcto (associator, optional) — --no-deps, same reason as SKHASH
        # above: pyocto's own metadata pins numpy<2.0, but it runs fine on
        # numpy>=2 (verified 2026-07-05) — letting pip resolve deps normally
        # would silently downgrade numpy and break xarray/simulflow elsewhere.
        if crun bash -c "PYTHONNOUSERSITE=1 python -c 'import pyocto'" &>/dev/null; then
            ok "PyOcto already installed"
        else
            inf "Installing PyOcto (associator, optional) …"
            cpip "--no-deps pyocto" && ok "PyOcto installed" \
                || warn "PyOcto failed — associator --method pyocto will be unavailable"
        fi

        # ── pyrocko (FocoNet focal mechanism, optional) — --no-deps, same
        # numpy<2 regression as SKHASH/PyOcto above.
        if crun bash -c "PYTHONNOUSERSITE=1 python -c 'import pyrocko'" &>/dev/null; then
            ok "pyrocko already installed"
        else
            inf "Installing pyrocko (FocoNet focal mechanism, optional) …"
            cpip "--no-deps pyrocko" && ok "pyrocko installed" \
                || warn "pyrocko failed — FocoNet focal mechanism will be unavailable"
        fi
    fi

    # Optional native desktop window (`seiswork gui --native`). Linux-only work;
    # auto-skips on macOS, headless servers, and with --no-auto.
    install_native_gui_deps
}

# ==============================================================================
# STEP 6 — Verify + build launcher + setup PATH
# ==============================================================================
step6_verify() {
    hdr "STEP 6 — Verifying installation"

    # ── Binary check ──────────────────────────────────────────────────────────
    echo "  Compiled binaries:"
    local all_bins=(REAL hypoDD ph2dt NLLoc Grid2Time Vel2Grid velest hypoinverse
                    growclust MatchLocate2 SelectFinal FDTCC slinktool slarchive)
    for b in "${all_bins[@]}"; do
        if has_bin "$b"; then
            local p; p="$(command -v "$b" 2>/dev/null || echo "$HOME_BIN/$b")"
            local tag=""; [[ "$(resolve_path "$BIN_DIR/$b")" == "$CORE_DIR/bin-docker/"* ]] && tag="  (docker fallback)"
            ok "  $(printf '%-16s' "$b")  $p$tag"
        else
            warn "  $(printf '%-16s' "$b")  not found (optional or needs compiling)"
        fi
    done

    # ── Python check ──────────────────────────────────────────────────────────
    echo
    echo "  Python packages:"
    local test_py; test_py="$(mktemp /tmp/sw_verify_XXXX.py)"
    cat > "$test_py" << 'PYEOF'
import importlib, sys, os
os.environ["PYTHONNOUSERSITE"] = "1"
ok_count = 0; fail_count = 0
checks = [
    ("yaml","pyyaml"),("numpy","numpy"),("pandas","pandas"),
    ("scipy","scipy"),("matplotlib","matplotlib"),("obspy","obspy"),
    ("flask","flask"),("requests","requests"),("tqdm","tqdm"),
    ("seisbench","seisbench"),
    ("xarray","xarray"),("netCDF4","netCDF4"),("bs4","beautifulsoup4"),
]
for mod, label in checks:
    try:
        m = importlib.import_module(mod)
        print(f"  OK  {label:<18} {getattr(m,'__version__','?')}")
        ok_count += 1
    except Exception as e:
        print(f"  ✗   {label:<18} {e}")
        fail_count += 1
try:
    import torch
    cuda = torch.cuda.is_available()
    dev  = torch.cuda.get_device_name(0) if cuda else "CPU-only"
    print(f"  OK  torch              {torch.__version__}  GPU={cuda}  [{dev}]")
    ok_count += 1
except Exception as e:
    print(f"  ✗   torch              {e}")
    fail_count += 1
try:
    import webview, importlib.metadata as _md
    print(f"  OK  pywebview          native window ({_md.version('pywebview')})")
    ok_count += 1
except Exception as e:
    print(f"  !   pywebview (optional) {e}  — `seiswork gui --native` falls back to browser")
try:
    from gamma.utils import association
    print("  OK  GaMMA              gamma.utils.association OK")
    ok_count += 1
except Exception as e:
    print(f"  !   GaMMA (optional)   {e}")
try:
    import SKHASH
    print("  OK  SKHASH             focal mechanism (Mechanism step) OK")
    ok_count += 1
except Exception as e:
    print(f"  !   SKHASH (optional)  {e}")
try:
    import pyocto
    print("  OK  PyOcto             associator --method pyocto OK")
    ok_count += 1
except Exception as e:
    print(f"  !   PyOcto (optional)  {e}")
try:
    import pyrocko
    print("  OK  pyrocko            FocoNet focal mechanism OK")
    ok_count += 1
except Exception as e:
    print(f"  !   pyrocko (optional) {e}")
print(f"\n  Result: {ok_count} OK,  {fail_count} failed")
sys.exit(0 if fail_count == 0 else 1)
PYEOF

    local rc=0
    if $USE_VENV; then
        "$VENV_DIR/bin/python" "$test_py" || rc=$?
    else
        export PYTHONNOUSERSITE=1
        "$CONDA" run -n "$ENV_NAME" python "$test_py" || rc=$?
    fi
    rm -f "$test_py"

    # ── PATH: add ~/bin to the user's shell rc (bash AND zsh) ─────────────────
    # macOS defaults to zsh and never sources ~/.bashrc, so a .bashrc-only PATH
    # entry leaves the 'seiswork' launcher unfound. Cover .zshrc too when it's the
    # login shell / on macOS / when ~/.zshrc exists.
    echo
    add_home_bin_path "$HOME/.bashrc"
    if $IS_MAC || [ "${SHELL##*/}" = "zsh" ] || [ -f "$HOME/.zshrc" ]; then
        add_home_bin_path "$HOME/.zshrc"
    fi

    # ── Create the ~/start_seiswork.sh launcher ───────────────────────────────
    if $USE_VENV; then
        cat > "$HOME/start_seiswork.sh" << LAUNCHEOF
#!/bin/bash
# SeisWork Web GUI launcher — by HakimBMKG
source "$VENV_DIR/bin/activate"
export PATH="$HOME/bin:\$PATH"
cd "$SEISWORK_DIR"
exec seiswork gui --host 0.0.0.0 --port 5000
LAUNCHEOF
        chmod +x "$HOME/start_seiswork.sh"
        ok "Launcher: ~/start_seiswork.sh"
        inf "Run: bash ~/start_seiswork.sh"
    else
        # conda launcher
        cat > "$HOME_BIN/seiswork" << LAUNCH
#!/usr/bin/env bash
export PYTHONNOUSERSITE=1
exec conda run --no-capture-output -n seiswork \\
    python "$SEISWORK_DIR/seiswork.py" "\$@"
LAUNCH
        chmod +x "$HOME_BIN/seiswork"
        ok "Launcher: ~/bin/seiswork"
        inf "Run: conda activate seiswork && seiswork gui"
    fi

    echo
    if [ $rc -eq 0 ]; then
        ok "All Python packages verified"
    else
        warn "Some packages failed — see the output above"
    fi

    return $rc
}

# ==============================================================================
# Status overview (--check)
# ==============================================================================
show_status() {
    hdr "SeisWork — Installation Status"

    # Env
    if $USE_VENV; then
        printf "  venv '%s': " "$VENV_DIR"
        [ -d "$VENV_DIR" ] \
            && echo -e "${GRN}EXISTS${RST}" \
            || echo -e "${RED}NOT CREATED${RST}  →  run: bash install.sh --venv --step 4,5"
    else
        printf "  conda env '%s': " "$ENV_NAME"
        if [ -n "$CONDA" ] && "$CONDA" env list 2>/dev/null | grep -q "^$ENV_NAME\b"; then
            echo -e "${GRN}EXISTS${RST}"
        else
            echo -e "${RED}NOT CREATED${RST}  →  run: bash install.sh --step 4,5"
        fi
    fi
    echo

    echo "  Compiled binaries:"
    for b in REAL hypoDD ph2dt NLLoc Grid2Time Vel2Grid velest hypoinverse \
              growclust MatchLocate2 SelectFinal FDTCC slinktool slarchive glass-app; do
        if has_bin "$b"; then
            local p; p="$(command -v "$b" 2>/dev/null || echo "$HOME_BIN/$b")"
            local tag=""; [[ "$(resolve_path "$BIN_DIR/$b")" == "$CORE_DIR/bin-docker/"* ]] && tag="  (docker fallback)"
            ok "  $(printf '%-16s' "$b")  $p$tag"
        else
            warn "  $(printf '%-16s' "$b")  not found"
        fi
    done

    echo
    # FocoNet (pure Python, no compile — see step2/step3 comments) — checked as
    # a source-tree presence, not a binary, since it's imported via sys.path.
    if [ -f "$SRC_DIR/FocoNet/FocoNet_O/model.py" ] && [ -f "$SRC_DIR/FocoNet/FocoNet_O/model/FocoNet_O.pth" ]; then
        ok "  FocoNet (mechanism)  $SRC_DIR/FocoNet"
    else
        warn "  FocoNet (mechanism)  not found — run: bash install.sh --step 2"
    fi

    echo
    if $USE_VENV; then
        [ -f "$HOME/start_seiswork.sh" ] \
            && ok "  Launcher: ~/start_seiswork.sh  (run: bash ~/start_seiswork.sh)" \
            || warn "  Launcher not created yet — run: bash install.sh --step 6"
    else
        [ -x "$HOME_BIN/seiswork" ] \
            && ok "  Launcher: ~/bin/seiswork  (run: seiswork gui)" \
            || warn "  Launcher not created yet — run: bash install.sh --step 6"
    fi
}

# ==============================================================================
# REBUILD BINS — recompile & reinstall ONLY the binaries still missing
# (invoked by --rebuild-bins / --fix-bins). Reuses Step 2 + Step 3, both of
# which are idempotent: existing sources and working binaries are left alone,
# so this safely fills in whatever failed to compile the first time.
# ==============================================================================
step_rebuild_bins() {
    hdr "REBUILD — Checking compiled binaries"

    # Same expected set as step6_verify's binary check, plus glass-app.
    local expected=(REAL hypoDD ph2dt NLLoc Grid2Time Vel2Grid velest hypoinverse
                    growclust MatchLocate2 SelectFinal FDTCC slinktool slarchive glass-app)

    local missing=()
    local b
    for b in "${expected[@]}"; do has_bin "$b" || missing+=("$b"); done

    if [ ${#missing[@]} -eq 0 ]; then
        ok "All ${#expected[@]} binaries already present — nothing to rebuild."
        return 0
    fi

    warn "Missing (${#missing[@]}): ${missing[*]}"
    inf "Fetching any missing source (Step 2, idempotent) then recompiling (Step 3) …"
    step2_download
    step3_compile

    # ── Final report ──────────────────────────────────────────────────────────
    hdr "REBUILD — Result"
    local still=()
    for b in "${expected[@]}"; do
        if has_bin "$b"; then
            ok   "  $(printf '%-14s' "$b")  present"
        else
            warn "  $(printf '%-14s' "$b")  STILL missing"
            still+=("$b")
        fi
    done

    if [ ${#still[@]} -eq 0 ]; then
        echo; ok "All previously-missing binaries were rebuilt successfully."
        return 0
    fi

    # Known hard cases → point at the specific requirement instead of a bare fail.
    echo
    warn "Still missing: ${still[*]}"
    for b in "${still[@]}"; do
        case "$b" in
            MatchLocate2|SelectFinal)
                if [ ! -d "$SRC_DIR/MatchLocate2/src" ]; then
                    inf "  $b: source missing — the MatchLocate2 clone failed (usually the" \
                        "network). Re-run '--rebuild-bins' when the connection is stable."
                elif [ "$b" = "MatchLocate2" ]; then
                    inf "  MatchLocate2 (main) needs the 'xapiir' subroutine from the full SAC" \
                        "library: https://ds.iris.edu/ds/nodes/dmc/forms/sac/ (companions build fine)."
                else
                    inf "  SelectFinal failed to compile — see the Step 3 output above." ;
                fi ;;
            FDTCC)
                inf "  FDTCC also needs SAC's 'xapiir' — install SAC:" \
                    "https://ds.iris.edu/ds/nodes/dmc/forms/sac/" ;;
            glass-app)
                if [ ! -d "$SRC_DIR/neic-glass3" ]; then
                    inf "  glass-app: source (neic-glass3) missing — re-run to clone it."
                else
                    inf "  glass-app: neic-glass3 cmake configure/build failed — see the Step 3" \
                        "output above for the error."
                fi ;;
            *)
                inf "  $b failed to compile — see the Step 3 output above for the error." ;;
        esac
    done
    return 1
}

# ==============================================================================
# Documentation — auto-generate on every install/update (best-effort; never
# fails the install if it doesn't work, since it's a convenience, not a
# requirement — see docs/generate_full_doc.py for the generator itself).
# ==============================================================================
generate_docs() {
    local gen="$SEISWORK_DIR/docs/generate_full_doc.py"
    [ -f "$gen" ] || return 0

    if $USE_VENV; then
        [ -x "$VENV_DIR/bin/python" ] || return 0
        hdr "Generating documentation"
        inf "Installing markdown/weasyprint (best-effort — PDF/HTML export only) …"
        pip_venv markdown weasyprint -q &>/dev/null
        "$VENV_DIR/bin/python" "$gen" \
            && ok "Documentation generated in docs/" \
            || warn "Documentation generation failed — see docs/ for any partial output"
    else
        "$CONDA" env list 2>/dev/null | grep -q "^$ENV_NAME\b" || return 0
        hdr "Generating documentation"
        inf "Installing markdown/weasyprint (best-effort — PDF/HTML export only) …"
        cpip "markdown weasyprint" &>/dev/null
        crun python "$gen" \
            && ok "Documentation generated in docs/" \
            || warn "Documentation generation failed — see docs/ for any partial output"
    fi
}

# ==============================================================================
# STEP 7 — Docker + PhaseNet-native GPU image (optional)
# ==============================================================================
step7_docker_phasenet() {
    hdr "STEP 7 — Docker + PhaseNet-native GPU image"
    local os; os="$(uname -s)"
    local gpu=false
    if command -v nvidia-smi &>/dev/null && nvidia-smi &>/dev/null; then gpu=true; fi
    inf "OS=$os   NVIDIA GPU=$gpu"

    if ! command -v docker &>/dev/null; then
        warn "Docker not installed."
        if [ "$os" = "Darwin" ]; then
            inf "Install Docker Desktop for Mac: https://docs.docker.com/desktop/install/mac-install/"
        else
            local pm; pm="$PKG"
            inf "Install Docker Engine:"
            case "$pm" in
              apt-get) inf "  curl -fsSL https://get.docker.com | sudo sh" ;;
              dnf|yum) inf "  sudo $pm install -y docker && sudo systemctl enable --now docker" ;;
              *)       inf "  see https://docs.docker.com/engine/install/" ;;
            esac
        fi
        warn "phasenet_native disabled — the seisbench picker still works."
        inf  "Re-run: bash install.sh --step 7"
        return 0
    fi
    ok "docker $(docker --version 2>/dev/null | cut -d, -f1 | awk '{print $3}')"

    local DRUN="docker"
    if ! docker info &>/dev/null; then
        if id -nG 2>/dev/null | grep -qw docker; then
            DRUN="sg docker -c"
        elif [ "$os" = "Linux" ]; then
            if _SUDO usermod -aG docker "$USER"; then
                ok "added to docker group — active in new shells"
                DRUN="sg docker -c"
            else
                DRUN="sudo docker"
            fi
        fi
    fi
    dk() { if [ "$DRUN" = "docker" ]; then docker "$@"; elif [ "$DRUN" = "sudo docker" ]; then sudo docker "$@"; else sg docker -c "docker $*"; fi; }

    if [ "$os" = "Darwin" ]; then
        warn "macOS Docker does not support NVIDIA GPU passthrough."
    elif $gpu; then
        if dk info 2>/dev/null | grep -qi 'Runtimes:.*nvidia' || command -v nvidia-ctk &>/dev/null; then
            ok "nvidia container runtime present"
        else
            warn "nvidia-container-toolkit missing → installing …"
            local pm; pm="$PKG"
            local ctk_ok=false
            case "$pm" in
              apt-get)
                # Debian / Ubuntu — official NVIDIA repo
                # '|| warn' on each pipeline: under set -euo pipefail a failing curl
                # (no network) would otherwise pipefail+set -e abort the whole step.
                curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
                  | _SUDO gpg --batch --yes --dearmor \
                      -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg 2>/dev/null \
                  || warn "failed to fetch the nvidia-container-toolkit GPG key (network?)"
                curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
                  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
                  | _SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null \
                  || warn "failed to add the nvidia-container-toolkit apt repo (network?)"
                _SUDO apt-get update -qq \
                  && _SUDO apt-get install -y nvidia-container-toolkit \
                  && ctk_ok=true \
                  || warn "apt install nvidia-container-toolkit failed"
                ;;
              dnf|yum)
                # RHEL / CentOS / Fedora / Rocky / Alma — official NVIDIA repo
                local _base_url="https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo"
                if _SUDO "$pm" config-manager --add-repo "$_base_url" 2>/dev/null \
                   || _SUDO tee /etc/yum.repos.d/nvidia-container-toolkit.repo \
                        < <(curl -fsSL "$_base_url") >/dev/null 2>&1; then
                    _SUDO "$pm" install -y nvidia-container-toolkit \
                      && ctk_ok=true \
                      || warn "$pm install nvidia-container-toolkit failed"
                else
                    warn "Failed to add the nvidia-container-toolkit repo for $pm"
                fi
                ;;
              zypper)
                # openSUSE / SLES
                local _zyp_url="https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo"
                _SUDO zypper addrepo "$_zyp_url" nvidia-ctk 2>/dev/null || true
                _SUDO zypper --non-interactive install nvidia-container-toolkit \
                  && ctk_ok=true \
                  || warn "zypper install nvidia-container-toolkit failed"
                ;;
              *)
                warn "Package manager '$pm' not recognized for auto-installing nvidia-container-toolkit."
                inf  "  Install manual: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html"
                ;;
            esac
            if $ctk_ok || command -v nvidia-ctk &>/dev/null; then
                _SUDO nvidia-ctk runtime configure --runtime=docker \
                  && _SUDO systemctl restart docker \
                  && ok "nvidia-container-toolkit configured" \
                  || warn "nvidia-ctk configure failed — check the docker log"
            else
                warn "nvidia-container-toolkit not installed — PhaseNet native will run CPU-only"
                inf  "  Check: https://docs.nvidia.com/datacenter/cloud-native/"
            fi
        fi
    else
        warn "No NVIDIA GPU found → the container runs CPU-only (slow)."
    fi

    if [ ! -f "$PHASENET_DIR/phasenet/predict.py" ]; then
        inf "Cloning AI4EPS PhaseNet → $PHASENET_DIR …"
        mkdir -p "$(dirname "$PHASENET_DIR")"
        git clone --depth 1 https://github.com/AI4EPS/PhaseNet "$PHASENET_DIR" \
            || warn "clone failed — set PHASENET_DIR or clone manually"
    fi
    [ -f "$PHASENET_DIR/phasenet/predict.py" ] && ok "PhaseNet repo: $PHASENET_DIR" || warn "PhaseNet repo missing"

    if [ ! -f "$PN_DOCKER_CTX/Dockerfile" ]; then
        warn "Dockerfile not found at $PN_DOCKER_CTX"; return 0
    fi
    if dk info &>/dev/null; then
        inf "Building $PN_IMAGE (TF 2.12 GPU + obspy/pandas/h5py) — ~3GB …"
        dk build -t "$PN_IMAGE" "$PN_DOCKER_CTX" \
            && ok "image built: $PN_IMAGE" \
            || warn "image build failed"
    fi

    if $gpu && [ "$os" = "Linux" ] && dk info &>/dev/null; then
        dk run --rm --gpus all "$PN_IMAGE" \
            python -c "import tensorflow as tf;print('TF',tf.__version__,'GPU',len(tf.config.list_physical_devices('GPU')))" 2>/dev/null \
            | grep -q GPU && ok "GPU-in-container verified" || warn "container cannot see the GPU"
    fi
    inf "phasenet_native ready: GUI → Picking → 'PhaseNet Native'"
    return 0
}

# ==============================================================================
# STEP 9 — SeisWork Agent  (optional; install on the SeisComP server)
# ==============================================================================
step9_agent() {
    hdr "STEP 9 — SeisWork Agent (integrator SeisComP)"

    local agent_src="$BASE_DIR/seiswork-agent/agent.py"
    local install_script="$BASE_DIR/seiswork-agent/install_agent.sh"

    if [ ! -f "$agent_src" ]; then
        warn "seiswork-agent/agent.py not found — skipping"
        return 0
    fi

    inf "SeisWork Agent lets SeisWork control SeisComP remotely:"
    inf "  • Push inventory XML to SeisComP"
    inf "  • Generate binding key files automatically"
    inf "  • Restart SeisComP modules from the GUI"
    inf "  • Forward picks from the message bus to SeisWork (bridge)"
    echo ""

    read -r -p "  Install SeisWork Agent now? [y/N] " ans
    case "$ans" in
        [Yy]*) ;;
        *) inf "Skipping agent install. Can be installed later:"; inf "  bash seiswork-agent/install_agent.sh --init"; return 0 ;;
    esac

    bash "$install_script" --init
    ok "SeisWork Agent installed. Run: bash ~/.seiswork-agent/start_agent.sh"
}

# Print the first TCP port from the args that is NOT already listening, else the
# first candidate. On macOS port 5000 (and 7000) are held by ControlCenter's AirPlay
# Receiver (Server: AirTunes) — it answers 403, which looks like an auth error in the
# browser — so the GUI must avoid it.
_pick_free_port() {
    local p
    for p in "$@"; do
        if command -v lsof >/dev/null 2>&1; then
            lsof -iTCP:"$p" -sTCP:LISTEN -P >/dev/null 2>&1 || { printf '%s\n' "$p"; return 0; }
        else
            printf '%s\n' "$p"; return 0
        fi
    done
    printf '%s\n' "$1"
}

# Resolve the env's python interpreter (conda env or venv). Prints the path or "".
_env_python() {
    local py=""
    if ! $USE_VENV; then
        py="$("$CONDA" run -n "$ENV_NAME" which python 2>/dev/null || true)"
        if [ -z "$py" ] || [ ! -x "$py" ]; then
            local _c
            for _c in "$HOME/miniforge3/envs/$ENV_NAME/bin/python" \
                      "$HOME/miniconda3/envs/$ENV_NAME/bin/python" \
                      "/opt/miniconda3/envs/$ENV_NAME/bin/python"; do
                [ -x "$_c" ] && { py="$_c"; break; }
            done
        fi
    else
        py="$VENV_DIR/bin/python"
    fi
    [ -x "$py" ] && printf '%s\n' "$py"
}

# ==============================================================================
# STEP 8 (macOS) — launchd LaunchAgent: auto-start the GUI at login, restart on
# crash. The macOS equivalent of the Linux systemd user service below.
# ==============================================================================
step8_macos_launchd() {
    hdr "STEP 8 — launchd LaunchAgent (auto-start SeisWork GUI at login)"

    local py; py="$(_env_python)"
    if [ -z "$py" ]; then
        warn "python for env '$ENV_NAME' not found — run steps 4,5 first, then: bash install.sh --step 8"
        inf  "  Or start manually: $(launch_cmd)"
        return 0
    fi
    ok "Interpreter: $py"

    # macOS holds 5000/7000 for AirPlay Receiver → pick a free port (5000 first if
    # the user actually freed it, else 5001/8080/8000). SEISWORK_PORT overrides.
    local port="${SEISWORK_PORT:-}"
    if [ -z "$port" ]; then
        port="$(_pick_free_port 5000 5001 8080 8000)"
        if [ "$port" != "5000" ]; then
            warn "Port 5000 is busy on macOS (usually AirPlay Receiver / ControlCenter) → using $port."
            inf  "  To use 5000: System Settings → General → AirDrop & Handoff → turn OFF 'AirPlay Receiver'."
        fi
    fi
    local agent_dir="$HOME/Library/LaunchAgents"
    local log_dir="$HOME/Library/Logs"
    local plist="$agent_dir/com.seiswork.gui.plist"
    mkdir -p "$agent_dir" "$log_dir"
    local py_dir; py_dir="$(dirname "$py")"
    local path_env="$py_dir:$SEISWORK_DIR/core/bin:$HOME_BIN:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    cat > "$plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.seiswork.gui</string>
    <key>ProgramArguments</key>
    <array>
        <string>$py</string>
        <string>$SEISWORK_DIR/seiswork.py</string>
        <string>gui</string>
        <string>--host</string>
        <string>0.0.0.0</string>
        <string>--port</string>
        <string>$port</string>
        <string>--no-browser</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SEISWORK_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$path_env</string>
        <key>PYTHONNOUSERSITE</key>
        <string>1</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$log_dir/seiswork.gui.out.log</string>
    <key>StandardErrorPath</key>
    <string>$log_dir/seiswork.gui.err.log</string>
</dict>
</plist>
PLIST
    ok "LaunchAgent: $plist"

    # (Re)load so RunAtLoad starts it now and at every login. 'load -w' clears the
    # Disabled flag; unload first so an existing agent is replaced cleanly.
    launchctl unload "$plist" 2>/dev/null || true
    if launchctl load -w "$plist" 2>/dev/null; then
        ok "Agent loaded — GUI auto-starts now and at login"
    else
        warn "launchctl load failed — load manually: launchctl load -w \"$plist\""
    fi

    echo
    inf "GUI: http://localhost:$port"
    inf "Logs: $log_dir/seiswork.gui.out.log  (and .err.log)"
    inf "Manage the service:"
    inf "  launchctl unload ~/Library/LaunchAgents/com.seiswork.gui.plist   # stop + disable"
    inf "  launchctl load -w ~/Library/LaunchAgents/com.seiswork.gui.plist  # start + enable"
}

# ==============================================================================
# STEP 8 — Systemd user service  (Linux only; macOS uses launchd above)
# ==============================================================================
step8_service() {
    if $IS_MAC; then
        step8_macos_launchd
        return 0
    fi
    hdr "STEP 8 — Systemd user service (auto-start SeisWork GUI)"
    if ! command -v systemctl &>/dev/null; then
        warn "systemctl not found — skipping service setup"
        return 0
    fi

    # Auto-set XDG_RUNTIME_DIR when missing (headless SSH / sudo / CI case).
    # systemctl --user needs this variable to know which socket manager to use.
    if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
        local uid; uid="$(id -u)"
        local candidate="/run/user/$uid"
        if [ -d "$candidate" ]; then
            export XDG_RUNTIME_DIR="$candidate"
            inf "XDG_RUNTIME_DIR not set — auto: $XDG_RUNTIME_DIR"
        else
            warn "XDG_RUNTIME_DIR is unset and /run/user/$uid has not been created yet."
            inf  "  The systemd user manager may not be running yet for UID $uid."
            inf  "  Try enabling linger first: loginctl enable-linger $USER"
            inf  "  Then log back in and run: seiswork service install"
            return 0
        fi
    fi

    # Make sure the user service manager is responsive before continuing
    if ! systemctl --user status &>/dev/null 2>&1; then
        # If linger is active, /run/user/UID exists but the manager may not have spawned yet.
        # Try triggering it with systemd-run as a fallback.
        if command -v loginctl &>/dev/null; then
            loginctl enable-linger "$USER" 2>/dev/null || true
        fi
        # Wait briefly for the user manager to spawn
        local _i
        for _i in 1 2 3; do
            sleep 1
            systemctl --user status &>/dev/null 2>&1 && break
        done
    fi
    if ! systemctl --user status &>/dev/null 2>&1; then
        warn "The systemd user session is unresponsive — skipping auto-service."
        inf  "  Run manually after a full login: seiswork service install"
        return 0
    fi

    local sw_bin=""
    if ! $USE_VENV; then
        # Prefer the real console-script INSIDE the conda env directly — never
        # the ~/bin/seiswork wrapper (it calls `conda run`, which needs `conda`
        # on PATH; systemd's restricted service PATH doesn't have it, causing
        # a "conda: not found" / status=127 crash-loop). Check the actual
        # conda install's base first, then the common fallback locations.
        local conda_base=""
        [ -n "$CONDA" ] && conda_base="$(cd "$(dirname "$CONDA")/.." && pwd)"
        # $HOME/.conda/envs is conda's OTHER default envs_dirs entry — used when
        # the base install is shared/system-wide and this user has no writable
        # envs/ under it, so envs land in ~/.conda instead (unrelated to
        # $conda_base, easy to miss since only conda itself knows both live
        # together — it's the actual layout hit on a real remote VM install).
        for _cand in "$conda_base/envs/$ENV_NAME/bin/seiswork" \
                     "$HOME/.conda/envs/$ENV_NAME/bin/seiswork" \
                     "/opt/miniconda3/envs/$ENV_NAME/bin/seiswork" \
                     "$HOME/miniforge3/envs/$ENV_NAME/bin/seiswork" \
                     "$HOME/miniconda3/envs/$ENV_NAME/bin/seiswork"; do
            [ -f "$_cand" ] && { sw_bin="$_cand"; break; }
        done
        # Last resort: ask conda directly, but reject the wrapper if `which`
        # resolves to it (happens when ~/bin precedes the env's bin on PATH
        # inside the `conda run` subshell).
        if [ -z "$sw_bin" ]; then
            local _found; _found="$("$CONDA" run -n "$ENV_NAME" which seiswork 2>/dev/null || true)"
            [ -n "$_found" ] && [ -f "$_found" ] && [ "$_found" != "$HOME_BIN/seiswork" ] && sw_bin="$_found"
        fi
    else
        sw_bin="$VENV_DIR/bin/seiswork"
    fi
    if [ ! -f "$sw_bin" ]; then
        warn "seiswork binary not found — run steps 4,5 first."
        inf  "  Or: seiswork service install  (after conda activate seiswork)"
        return 1
    fi
    ok "Binary seiswork: $sw_bin"

    local svc_dir="$HOME/.config/systemd/user"
    local svc_file="$svc_dir/seiswork.service"
    mkdir -p "$svc_dir"

    local core_bin="$SEISWORK_DIR/core/bin"
    local py_dir; py_dir="$(dirname "$sw_bin")"
    local env_path="$py_dir:$core_bin:$HOME_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    cat > "$svc_file" << UNITEOF
[Unit]
Description=SeisWork — Seismological Processing Web GUI
Documentation=https://github.com/HakimBMKG/seiswork
After=network.target

[Service]
Type=simple
WorkingDirectory=$SEISWORK_DIR
ExecStart=$sw_bin gui --host 0.0.0.0 --port 5000 --no-browser
Environment=PYTHONNOUSERSITE=1
Environment=PATH=$env_path
Restart=on-failure
RestartSec=5
# Only signal the main PID on stop/restart: long-lived companions spawned by the
# server (slarchive SDS archiver, pipeline runners, the port-3346 viewer mirror)
# are designed to survive a service restart and be reused/re-adopted by the fresh
# instance. The default KillMode=control-group would SIGTERM the whole cgroup and
# silently kill in-flight archiving/jobs on every restart.
KillMode=process
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
UNITEOF
    ok "Unit file: $svc_file"

    systemctl --user daemon-reload || warn "daemon-reload failed — the service may not be picked up"
    systemctl --user enable seiswork \
        && ok "seiswork.service enabled (auto-start at boot)" \
        || warn "enabling the service failed — check: systemctl --user status seiswork"

    if command -v loginctl &>/dev/null; then
        loginctl enable-linger "$USER" 2>/dev/null && ok "loginctl linger active for $USER" || true
    fi

    if systemctl --user is-active seiswork &>/dev/null; then
        systemctl --user restart seiswork && ok "seiswork.service restarted" \
            || warn "restart failed — check: journalctl --user -u seiswork -n 30"
    else
        systemctl --user start seiswork && ok "seiswork.service started" \
            || warn "start failed — check: journalctl --user -u seiswork -n 30"
    fi

    local state; state="$(systemctl --user is-active seiswork 2>/dev/null || echo unknown)"
    if [ "$state" = "active" ]; then
        ok "Service active: $state"
        echo
        inf "GUI available at: http://localhost:5000"
        inf "             or: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo '...'):5000"
        inf ""
        inf "Service commands:"
        inf "  seiswork service status   — check status"
        inf "  seiswork service stop     — stop it"
        inf "  seiswork service log      — view the log"
    else
        warn "Service state: $state — check the log:"
        inf  "  journalctl --user -u seiswork -n 30"
    fi
}

# ==============================================================================
# STEP 10 — Desktop app / launcher  (permanent Dock/menu icon + name)
#   macOS : a real SeisWork.app bundle (CFBundleName + .icns) → the Dock shows
#           "SeisWork" with the SeisWork icon, not "Python" + the rocket.
#   Linux : a .desktop entry + hicolor theme icon → appears in the app menu.
#   Both launchers run `seiswork gui --native` (falls back to a browser tab if
#   pywebview is missing). No sudo, no Python bundling.
# ==============================================================================
_seiswork_version() {
    # Parse __version__ = "x.y.z" from seiswork/__init__.py (fallback: 0.0.0).
    local f="$SEISWORK_DIR/seiswork/__init__.py" v=""
    [ -f "$f" ] && v="$(grep -E '^__version__' "$f" | head -1 | sed -E 's/.*=[[:space:]]*["'\'']([^"'\'']+)["'\''].*/\1/')"
    echo "${v:-0.0.0}"
}

step10_desktop_macos() {
    hdr "STEP 10 — SeisWork.app bundle (permanent Dock icon + name)"
    local py; py="$(_env_python)"
    if [ -z "$py" ]; then
        warn "python for env '$ENV_NAME' not found — run steps 4,5 first, then: bash install.sh --step 10"
        return 0
    fi
    local ver; ver="$(_seiswork_version)"
    local app_root="$HOME/Applications"; mkdir -p "$app_root"
    local app="$app_root/SeisWork.app"
    rm -rf "$app"
    mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"

    # Icon: build a multi-resolution .icns from the 1024px PNG.
    local png="$SEISWORK_DIR/seiswork/web/static/img/seiswork-icon.png"
    local icns="$app/Contents/Resources/seiswork.icns"
    if [ -f "$png" ] && command -v iconutil &>/dev/null && command -v sips &>/dev/null; then
        local iconset; iconset="$(mktemp -d)/SeisWork.iconset"; mkdir -p "$iconset"
        local b
        for b in 16 32 128 256 512; do
            sips -z "$b"        "$b"        "$png" --out "$iconset/icon_${b}x${b}.png"     &>/dev/null
            sips -z "$((b*2))"  "$((b*2))"  "$png" --out "$iconset/icon_${b}x${b}@2x.png"  &>/dev/null
        done
        iconutil -c icns "$iconset" -o "$icns" 2>/dev/null || cp "$png" "$icns"
        rm -rf "$(dirname "$iconset")"
        ok "Icon: $icns"
    else
        [ -f "$png" ] && cp "$png" "$icns"
        warn "iconutil/sips not found — using the raw PNG as the icon"
    fi

    # Launcher executable → runs the native GUI from this repo.
    cat > "$app/Contents/MacOS/SeisWork" << LAUNCH
#!/bin/bash
export PYTHONNOUSERSITE=1
cd "$SEISWORK_DIR" || exit 1
exec "$py" "$SEISWORK_DIR/seiswork.py" gui --native "\$@"
LAUNCH
    chmod +x "$app/Contents/MacOS/SeisWork"

    cat > "$app/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>              <string>SeisWork</string>
    <key>CFBundleDisplayName</key>       <string>SeisWork</string>
    <key>CFBundleIdentifier</key>        <string>id.go.bmkg.seiswork</string>
    <key>CFBundleVersion</key>           <string>$ver</string>
    <key>CFBundleShortVersionString</key><string>$ver</string>
    <key>CFBundleExecutable</key>        <string>SeisWork</string>
    <key>CFBundleIconFile</key>          <string>seiswork.icns</string>
    <key>CFBundlePackageType</key>       <string>APPL</string>
    <key>LSMinimumSystemVersion</key>    <string>11.0</string>
    <key>NSHighResolutionCapable</key>   <true/>
    <key>LSApplicationCategoryType</key> <string>public.app-category.education</string>
</dict>
</plist>
PLIST
    ok "App bundle: $app  (v$ver)"

    # Nudge Launch Services so Finder/Dock pick up the icon & name immediately.
    touch "$app"
    local lsreg="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
    [ -x "$lsreg" ] && "$lsreg" -f "$app" 2>/dev/null || true

    echo
    inf "Launch from Spotlight/Finder ('SeisWork'), or:  open \"$app\""
    inf "The Dock now shows 'SeisWork' + the SeisWork icon (permanent — no --native hack needed)."
}

step10_desktop_linux() {
    hdr "STEP 10 — Desktop launcher (.desktop + themed icon)"
    local py; py="$(_env_python)"
    if [ -z "$py" ]; then
        warn "python for env '$ENV_NAME' not found — run steps 4,5 first, then: bash install.sh --step 10"
        return 0
    fi
    local apps_dir="$HOME/.local/share/applications"
    local icon_root="$HOME/.local/share/icons/hicolor"
    mkdir -p "$apps_dir" "$icon_root/512x512/apps" "$icon_root/scalable/apps"

    local png="$SEISWORK_DIR/seiswork/web/static/img/seiswork-icon.png"
    local svg="$SEISWORK_DIR/seiswork/web/static/img/seiswork-icon.svg"
    if [ -f "$png" ]; then
        cp "$png" "$icon_root/512x512/apps/seiswork.png" \
            && ok "Icon (png): $icon_root/512x512/apps/seiswork.png"
    fi
    [ -f "$svg" ] && cp "$svg" "$icon_root/scalable/apps/seiswork.svg"

    local desktop="$apps_dir/seiswork.desktop"
    cat > "$desktop" << DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=SeisWork
GenericName=Seismological Processing
Comment=SeisWork — Seismological Processing Framework
Exec=$py $SEISWORK_DIR/seiswork.py gui --native
Path=$SEISWORK_DIR
Icon=seiswork
Terminal=false
Categories=Science;Education;Geoscience;Physics;
Keywords=seismology;earthquake;waveform;phasenet;
StartupNotify=true
StartupWMClass=SeisWork
DESKTOP
    chmod +x "$desktop"
    ok "Launcher: $desktop"

    command -v update-desktop-database &>/dev/null && update-desktop-database "$apps_dir" 2>/dev/null || true
    command -v gtk-update-icon-cache   &>/dev/null && gtk-update-icon-cache -f -t "$icon_root" 2>/dev/null || true
    echo
    inf "Find 'SeisWork' in your application menu (Science/Education)."
    inf "For --native on Linux it needs the GTK WebKit stack (installed in step 5's native deps)."
}

step10_desktop_app() {
    if $IS_MAC; then step10_desktop_macos; else step10_desktop_linux; fi
}

# ==============================================================================
# Banner + argument parsing + main
# ==============================================================================
print_banner() {
    echo -e "${BLD}"
    cat << 'BANNER'
 ____       _     __        __         _
/ ___|  ___(_)___ \ \      / /__  _ __| | __
\___ \ / _ \ / __| \ \ /\ / / _ \| '__| |/ /
 ___) |  __/ \__ \  \ V  V / (_) | |  |   <
|____/ \___|_|___/   \_/\_/ \___/|_|  |_|\_\

  Seismological Processing Framework — Installer
  by HakimBMKG
BANNER
    echo -e "${RST}"
    echo "  dir  : $SEISWORK_DIR"
    echo "  mode : $(${USE_VENV} && echo "venv ($VENV_DIR)" || echo "conda env '$ENV_NAME'")"
    echo
}

parse_steps() {
    echo "$1" | tr ',' '\n' | grep -E '^[0-9]+$' | sort -un
}

main() {
    local do_check=false
    local do_rebuild_bins=false
    local steps_arg="1,2,3,4,5,6,7,8,10"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check|-c)      do_check=true ;;
            --venv)          FORCE_VENV=true ;;
            --no-auto)       AUTO_DEPS=false ;;
            --service)       steps_arg="8" ;;
            --desktop)       steps_arg="10" ;;
            --rebuild-bins|--fix-bins|--bins) do_rebuild_bins=true ;;
            --step|-s)       steps_arg="$2"; shift ;;
            --step=*)        steps_arg="${1#*=}" ;;
            -h|--help)
                echo "  Usage: bash install.sh [--venv] [--no-auto] [--check] [--step N[,N...]] [--service]"
                echo
                echo "  Cross-platform: Linux • macOS • WSL (automatic OS + pkg-manager detection)"
                echo
                echo "  --venv     Use a Python venv (no conda required)"
                echo "  --no-auto  Do not auto-install system dependencies (report only)"
                echo "  --check    Show installation status only"
                echo "  --step N   Run a specific step (combinable: --step 4,5)"
                echo "  --service  Alias for --step 8: install/update the systemd user service only"
                echo "  --desktop  Alias for --step 10: create the desktop app (macOS .app / Linux .desktop)"
                echo "  --rebuild-bins  Rebuild ONLY the compiled binaries that are still missing"
                echo "                  (fetches any missing source, recompiles, reinstalls; alias: --fix-bins)"
                echo
                echo "  Steps: 1=OS 2=download 3=compile 4=env 5=python 6=verify 7=docker 8=service 10=desktop-app"
                echo "  Default runs steps 1-8 + 10. Step 9=agent (SeisWork Agent / SeisComP bridge)"
                echo "  NOT automatic — opt-in, installed into the SeisComP Python on the SeisComP host:"
                echo "      bash install.sh --step 9     (or: bash seiswork-agent/install_agent.sh --init)"
                echo
                echo "  VELEST/Hypoinverse: source ships in core/src/ — compiled automatically in step 3."
                echo
                echo "  Docker fallback: step 3 automatically runs any binary it couldn't compile"
                echo "  natively (missing compiler, OS-specific build failure, ...) through the"
                echo "  seiswork-corebin Docker image instead — see core/bin-docker/ after install."
                echo "  Native compilation always stays the default when it succeeds."
                echo
                exit 0 ;;
            *) warn "Unknown argument: $1" ;;
        esac
        shift
    done

    resolve_env_mode
    print_banner

    if $do_check; then
        show_status
        exit 0
    fi

    if $do_rebuild_bins; then
        step_rebuild_bins
        exit $?
    fi

    # bash 3.2-safe (macOS): no mapfile available
    local steps=()
    while IFS= read -r _s; do [ -n "$_s" ] && steps+=("$_s"); done < <(parse_steps "$steps_arg")

    for step in "${steps[@]}"; do
        case $step in
            1) step1_check_os || warn "OS check reported an issue — proceeding with caution" ;;
            2) step2_download ;;
            3) step3_compile  ;;
            4) step4_create_env ;;
            5) step5_python ;;
            6) step6_verify ;;
            7) step7_docker_phasenet ;;
            8) step8_service ;;
            9) step9_agent ;;
            10) step10_desktop_app ;;
            *) warn "Unknown step: $step" ;;
        esac
    done

    generate_docs
    show_status

    echo -e "${BLD}  Installation complete!${RST}"
    echo
    if $USE_VENV; then
        echo "  Run SeisWork (manual):"
        echo "    bash ~/start_seiswork.sh"
        echo "  Or:"
        echo "    source $VENV_DIR/bin/activate && seiswork gui"
    else
        echo "  Run SeisWork (manual):"
        echo "    conda activate $ENV_NAME && seiswork gui --native"
    fi
    echo
    if command -v systemctl &>/dev/null && systemctl --user is-enabled seiswork &>/dev/null 2>&1; then
        ok "Service active — the GUI is automatically available at http://localhost:5000 after boot"
        echo "  Manage the service:"
        echo "    seiswork service status"
        echo "    seiswork service stop / start / restart"
        echo "    seiswork service log"
    else
        inf "For auto-start at boot:"
        inf "  seiswork service install"
        inf "  or: bash install.sh --service"
    fi
    echo
    warn "IMPORTANT: open a NEW terminal (or 'source ~/.zshrc' on macOS / 'source ~/.bashrc') so the PATH takes effect"
    echo
}

main "$@"
