#!/bin/bash
# ============================================================
#  Runs INSIDE the seiswork-corebin builder stage.
#  Downloads + compiles every SeisWork core binary into /out.
#  Recipes mirror install.sh Step 2/3 (Linux path).
# ============================================================
set -uo pipefail

SRC=/build/src        # bundled sources copied from core/src (velest, hypo71)
EXT=/build/ext        # external sources cloned/downloaded here
OUT=/out
mkdir -p "$EXT" "$OUT"

FF="-O2 -w -std=legacy -ffixed-line-length-none"
GFMAJ="$(gfortran -dumpversion | cut -d. -f1)"
[ "$GFMAJ" -ge 10 ] && FF="$FF -fallow-argument-mismatch"

ok()   { echo "[corebin-build] OK   $*"; }
fail() { echo "[corebin-build] FAIL $*"; }

# Several upstream repos (NonLinLoc, MatchLocate2, FDTCC, …) ship prebuilt
# macOS (Mach-O) binaries committed alongside the source. A naive `find` for
# an executable by name can match those instead of what we just compiled,
# silently producing a non-Linux binary that only fails at `docker run` time
# with "Exec format error". Always verify the ELF magic before copying.
is_elf() { [ -f "$1" ] && [ "$(head -c4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')" = "7f454c46" ]; }

# Shallow clones over flaky networks (seen: HTTP/2 stream resets on large
# repos like NonLinLoc) intermittently die mid-transfer. Retry a few times
# before giving up, same pattern as install.sh's git_clone().
git_clone_retry() {
    local url="$1" dest="$2" n
    for n in 1 2 3; do
        rm -rf "$dest"
        git -c http.version=HTTP/1.1 clone --depth 1 "$url" "$dest" 2>&1 && return 0
        echo "[corebin-build] clone retry $n/3 failed for $url"
    done
    return 1
}

# ── VELEST (bundled source) ──────────────────────────────────────────────────
( cd "$SRC/velest" && gfortran $FF -o "$OUT/velest" velest.f ) \
    && ok velest || { fail velest; exit 1; }

# ── Hypoinverse hyp1.40 (bundled source) ─────────────────────────────────────
( cd "$SRC/hypo71/source" && \
  objs="$(gawk '/^hyp1\.40[[:space:]]*:/{f=1} f{print} /-o hyp1\.40/{f=0}' makefile \
          | grep -oE '[A-Za-z0-9_]+\.o' | sort -u)" && \
  for o in $objs; do
      base="${o%.o}"
      if   [ -f "$base.for" ]; then gfortran $FF -c "$base.for" || exit 1
      elif [ -f "$base.f"   ]; then gfortran $FF -c "$base.f"   || exit 1
      else echo "missing source for $o"; exit 1; fi
  done && \
  gfortran $FF -o "$OUT/hypoinverse" $objs ) \
    && ok hypoinverse || { fail hypoinverse; exit 1; }

# ── REAL (C) ─────────────────────────────────────────────────────────────────
git_clone_retry https://github.com/Dal-mzhang/REAL.git "$EXT/REAL" || { fail "REAL clone"; exit 1; }
real_c="$(find "$EXT/REAL" -name REAL.c | head -1)"
gcc -O2 -o "$OUT/REAL" "$real_c" -lm \
    && is_elf "$OUT/REAL" && ok REAL || { fail REAL; exit 1; }

# ── slinktool (C) — SeedLink query/dump tool ─────────────────────────────────
git_clone_retry https://github.com/EarthScope/slinktool.git "$EXT/slinktool" || { fail "slinktool clone"; exit 1; }
make -C "$EXT/slinktool"
is_elf "$EXT/slinktool/slinktool" && cp -f "$EXT/slinktool/slinktool" "$OUT/slinktool" && ok slinktool \
    || { fail slinktool; exit 1; }

# ── slarchive (C) — SeedLink archiving daemon ────────────────────────────────
git_clone_retry https://github.com/EarthScope/slarchive.git "$EXT/slarchive" || { fail "slarchive clone"; exit 1; }
make -C "$EXT/slarchive"
is_elf "$EXT/slarchive/slarchive" && cp -f "$EXT/slarchive/slarchive" "$OUT/slarchive" && ok slarchive \
    || { fail slarchive; exit 1; }

# ── NonLinLoc suite (C, cmake — CMakeLists.txt lives in src/) ────────────────
# NonLinLoc's CMakeLists.txt sets RUNTIME_OUTPUT_DIRECTORY to src/bin (NOT the
# -B build dir), so the freshly linked binaries land in src/bin/. A clean
# git_clone_retry (rm -rf then clone) means nothing stale from a prior layer
# can already be sitting there; the ELF check below still guards against any
# repo-committed prebuilt binary sharing that filename.
git_clone_retry https://github.com/ut-beg-texnet/NonLinLoc.git "$EXT/NonLinLoc" || { fail "NonLinLoc clone"; exit 1; }
cmake -S "$EXT/NonLinLoc/src" -B "$EXT/NonLinLoc/build" -DCMAKE_BUILD_TYPE=Release -Wno-dev \
  && make -C "$EXT/NonLinLoc/build" -j"$(nproc)"
# Errors are printed (not swallowed) so a real compile failure is visible in
# the build log instead of just the generic "NLLoc not found" below.
for b in NLLoc Grid2Time Vel2Grid Time2EQ PhsAssoc; do
    src_b="$(find "$EXT/NonLinLoc/src/bin" "$EXT/NonLinLoc/build" -type f -name "$b" -perm -u+x 2>/dev/null | head -1)"
    if is_elf "$src_b"; then cp -f "$src_b" "$OUT/$b"; ok "$b"
    else fail "$b (not found or not an ELF binary in src/bin or build/)"; exit 1; fi
done

# ── HypoDD 1.3 + ph2dt (Fortran) ─────────────────────────────────────────────
wget -q --tries=3 http://www.ldeo.columbia.edu/~felixw/HYPODD/HYPODD_1.3.tar.gz \
     -O "$EXT/HYPODD.tar.gz" \
  && tar -xzf "$EXT/HYPODD.tar.gz" -C "$EXT"
hdd_src="$(find "$EXT" -maxdepth 3 -path "*HYPODD*/src" -type d | head -1)"
if [ -n "$hdd_src" ]; then
    find "$(dirname "$hdd_src")" -name Makefile -exec \
        sed -i "s|^FC[[:space:]]*=[[:space:]]*g77|FC = gfortran|g; s|^FC[[:space:]]*=[[:space:]]*f77|FC = gfortran|g" {} \;
    # Old F77 code: force legacy flags through both common Makefile variables
    for sub in hypoDD ph2dt; do
        rm -f "$hdd_src/$sub/$sub"   # drop any checked-in binary before (re)building
        make -C "$hdd_src/$sub" FFLAGS="$FF" FLAGS="$FF" >/dev/null 2>&1 || \
        make -C "$hdd_src/$sub" >/dev/null 2>&1 || true
        bin_out="$(find "$hdd_src/$sub" -maxdepth 1 -name "$sub" -type f | head -1)"
        if is_elf "$bin_out"; then cp -f "$bin_out" "$OUT/$sub"; ok "$sub"
        else fail "$sub"; exit 1; fi
    done
else
    fail "HypoDD download"; exit 1
fi

# ── GrowClust (Fortran) ──────────────────────────────────────────────────────
git_clone_retry https://github.com/dttrugman/GrowClust.git "$EXT/GrowClust" || { fail "GrowClust clone"; exit 1; }
gc_make="$(find "$EXT/GrowClust" -name Makefile | head -1)"
gc_dir="$(dirname "$gc_make")"
find "$EXT/GrowClust" -name growclust -type f -delete   # drop any checked-in binary
make -C "$gc_dir"   # errors printed, not swallowed
gc_bin="$(find "$gc_dir" -maxdepth 2 -name growclust -type f | head -1)"
if is_elf "$gc_bin"; then cp -f "$gc_bin" "$OUT/growclust"; ok growclust
else fail growclust; exit 1; fi

# ── MatchLocate2 + SelectFinal + tools (C) ───────────────────────────────────
# Original Makefile targets macOS (gcc-10, external SAC libs). Rewrite it for
# plain gcc; sacio.c ships in src. MatchLocate2 main needs SAC's xapiir on some
# checkouts — treat main as best-effort, the companion tools always build.
# The repo's bin/ may already carry prebuilt macOS binaries — wipe it first so
# a failed `make` cannot leave a stale Mach-O file for us to copy by mistake.
if git_clone_retry https://github.com/Dal-mzhang/MatchLocate2.git "$EXT/MatchLocate2"; then
    ml2_src="$EXT/MatchLocate2/src"
    rm -rf "$EXT/MatchLocate2/bin"; mkdir -p "$EXT/MatchLocate2/bin"
    cat > "$ml2_src/Makefile" <<'MLEOF'
CC = gcc -Os -mcmodel=medium -fopenmp -w
LIBS = -lm
BIN = ../bin
all: MatchLocate2 SelectFinal SHIFT lsac ccsacc clean
MatchLocate2: MatchLocate2.o sacio.o
	$(CC) -o $(BIN)/$@ $^ $(LIBS)
SelectFinal: SelectFinal.o
	$(CC) -o $(BIN)/$@ $^ $(LIBS)
SHIFT: SHIFT.o
	$(CC) -o $(BIN)/$@ $^ $(LIBS)
lsac: lsac.o sacio.o
	$(CC) -o $(BIN)/$@ $^ $(LIBS)
ccsacc: ccsacc.o sacio.o
	$(CC) -o $(BIN)/$@ $^ $(LIBS)
clean:
	rm -f *.o
MLEOF
    # -k (keep going): MatchLocate2 needs SAC's xapiir and may fail to link on
    # some checkouts — without -k that failure aborts `make all` before the
    # companion tools (SelectFinal/SHIFT/lsac/ccsacc) are even attempted.
    make -k -C "$ml2_src"
    for b in MatchLocate2 SelectFinal SHIFT lsac ccsacc; do
        if is_elf "$EXT/MatchLocate2/bin/$b"; then
            cp -f "$EXT/MatchLocate2/bin/$b" "$OUT/$b"; ok "$b"
        else
            echo "[corebin-build] skip $b (optional, did not compile)"
        fi
    done
else
    echo "[corebin-build] skip MatchLocate2 suite (optional, clone failed)"
fi

# ── FDTCC (C, optional — needs local sacio) ──────────────────────────────────
git_clone_retry https://github.com/MinLiu19/FDTCC.git "$EXT/FDTCC" || true
fdtcc_c="$(find "$EXT/FDTCC" -name "FDTCC*.c" 2>/dev/null | head -1)"
if [ -n "$fdtcc_c" ]; then
    fdtcc_dir="$(dirname "$fdtcc_c")"
    sacio_c="$(find "$EXT/FDTCC" -name "sacio.c" | head -1)"
    gcc -O2 -fopenmp -w -o "$OUT/FDTCC" "$fdtcc_c" ${sacio_c:+"$sacio_c"} -lm \
        -I"$fdtcc_dir" 2>/dev/null
    if is_elf "$OUT/FDTCC"; then ok FDTCC
    else rm -f "$OUT/FDTCC"; echo "[corebin-build] skip FDTCC (optional, did not compile)"; fi
fi

chmod a+rx "$OUT"/*
echo "[corebin-build] Done. Binaries in /out:"
ls -1 "$OUT"
