#!/usr/bin/env bash
# ============================================================
#  Build the seiswork-corebin Docker image and (optionally)
#  generate per-binary wrapper scripts in core/bin-docker/.
#
#  Usage:
#    bash docker/build_corebin.sh                    # build image only
#    bash docker/build_corebin.sh --wrappers         # build + generate wrappers → core/bin-docker/
#    bash docker/build_corebin.sh --wrappers DIR     # build + generate wrappers → DIR
#
#  The wrappers let any tool that expects a native binary run the
#  Dockerized one transparently, e.g.:
#    export PATH="$REPO/core/bin-docker:$PATH"
#    NLLoc run.in
#
#  Each wrapper mounts $HOME at the same path inside the container
#  and keeps the current working directory, so absolute paths under
#  your home (work/, config/, grids, …) resolve unchanged.
# ============================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="seiswork-corebin:latest"

command -v docker >/dev/null 2>&1 || {
    echo "ERROR: docker is not installed or not on PATH." >&2
    echo "Install Docker Desktop (macOS/Windows) or docker-ce (Linux) first." >&2
    exit 1
}

echo "[corebin] Building $IMAGE (context: $REPO_DIR) ..."
docker build --platform linux/amd64 \
    -f "$REPO_DIR/docker/Dockerfile.corebin" -t "$IMAGE" "$REPO_DIR"

echo "[corebin] Image built. Binaries inside:"
docker run --rm "$IMAGE" list

if [ "${1:-}" = "--wrappers" ]; then
    WRAP_DIR="${2:-$REPO_DIR/core/bin-docker}"
    mkdir -p "$WRAP_DIR"
    BINARIES="$(docker run --rm "$IMAGE" list | tail -n +2)"
    for b in $BINARIES; do
        cat > "$WRAP_DIR/$b" <<EOF
#!/usr/bin/env bash
# Auto-generated wrapper: runs the Dockerized SeisWork core binary '$b'.
# Mounts \$HOME at the same path and keeps the current working directory,
# so relative AND home-absolute file paths work as with a native binary.
exec docker run --rm -i --platform linux/amd64 \\
    -u "\$(id -u):\$(id -g)" \\
    -v "\$HOME:\$HOME" \\
    -w "\$PWD" \\
    $IMAGE $b "\$@"
EOF
        chmod +x "$WRAP_DIR/$b"
    done
    echo "[corebin] Wrappers written to $WRAP_DIR ($(echo "$BINARIES" | wc -w) binaries)."
    echo "[corebin] Add to PATH or point SeisWork 'exec' settings at them, e.g.:"
    echo "          export PATH=\"$WRAP_DIR:\$PATH\""
fi
