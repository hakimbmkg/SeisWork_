#!/bin/sh
# Entrypoint of the seiswork-corebin image.
# Usage: docker run --rm seiswork-corebin <binary> [args...]
# With no arguments it lists the available binaries.
BIN_DIR=/opt/seiswork/bin

if [ "$#" -eq 0 ] || [ "$1" = "list" ] || [ "$1" = "--list" ]; then
    echo "SeisWork core binaries available in this image:"
    ls -1 "$BIN_DIR"
    exit 0
fi

name="$1"; shift
if [ -x "$BIN_DIR/$name" ]; then
    exec "$BIN_DIR/$name" "$@"
fi
# Not a known core binary — run the command as-is (e.g. sh for debugging)
exec "$name" "$@"
