#!/usr/bin/env bash
# Builds Ren'Py 8.5.3 for the Nintendo Switch. Run setup.bash first.
#
# Output (./raw):
#   raw/renpy-switch.nro     generic launcher: put it in sdmc:/switch/<name>/
#                            together with the game's "game" folder
#   raw/renpy-switch.elf     ELF, for re-packing with a custom title/icon
#   raw/exefs/main           NSO, for ExeFS / installable builds
#   raw/exefs/main.npdm      NPDM for installable builds (also in the RomFS)
#   raw/romfs/               runtime RomFS (Ren'Py + CPython stdlib)
set -euo pipefail

export DEVKITPRO="${DEVKITPRO:-/opt/devkitpro}"
export PATH="$DEVKITPRO/tools/bin:$DEVKITPRO/devkitA64/bin:$PATH"
export RENPY_VER=8.5.3

APP_NAME="${APP_NAME:-Ren'Py}"
APP_AUTHOR="${APP_AUTHOR:-Ren'Py Switch}"
APP_VERSION="${APP_VERSION:-$RENPY_VER}"
APP_ICON="${APP_ICON:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
cd "$SCRIPT_DIR"

CLEAN=false
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=true ;;
        -h|--help) echo "Usage: $0 [--clean]"; exit 0 ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

for d in renpy-source renpy_sdk; do
    [ -d "$d" ] || { echo "ERROR: $d not found. Run setup.bash first." >&2; exit 1; }
done
[ -f "$VENV_DIR/bin/activate" ] || { echo "ERROR: venv missing. Run setup.bash first." >&2; exit 1; }

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
export RENPY_CYTHON="$VENV_DIR/bin/cython"

PORTLIBS="$DEVKITPRO/portlibs/switch"
PY_RUNTIME="$PORTLIBS/share/python-switch/romfs/python"
[ -d "$PY_RUNTIME/lib/python3.14" ] || { echo "ERROR: NXPython runtime not found at $PY_RUNTIME" >&2; exit 1; }

if [ "$CLEAN" = true ]; then
    rm -rf build-switch raw renpy-source/tmp
fi

# ─── 1. Cython → C, module list, inittab ─────────────────────────────────────
echo ">>> Generating Cython sources..."
mkdir -p build-switch
python scripts/switch_modules.py renpy-source build-switch/gen

# ─── 2. Cross-compile the executable ─────────────────────────────────────────
echo ">>> Compiling for the Switch..."
cmake -S switch -B build-switch/cmake \
    -DCMAKE_TOOLCHAIN_FILE="$DEVKITPRO/cmake/Switch.cmake" \
    -DCMAKE_BUILD_TYPE=Release \
    -DRENPY_GEN_OUT="$SCRIPT_DIR/build-switch/gen"
cmake --build build-switch/cmake -j "$(nproc)"

ELF="build-switch/cmake/renpy-switch"
[ -f "$ELF" ] || ELF="build-switch/cmake/renpy-switch.elf"

# ─── 3. Precompile renpy/common with the official SDK (host) ─────────────────
echo ">>> Compiling renpy/common .rpy -> .rpyc..."
rm -rf build-switch/compile
mkdir -p build-switch/compile/game
cp renpy_sdk/renpy.sh build-switch/compile/
cp -r renpy_sdk/lib build-switch/compile/lib
cp -r renpy-source/renpy build-switch/compile/renpy
cp renpy-source/renpy.py build-switch/compile/renpy.py
cp script.rpy build-switch/compile/game/script.rpy
(cd build-switch/compile && SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy ./renpy.sh . compile)

# ─── 4. Stage the RomFS ──────────────────────────────────────────────────────
echo ">>> Staging RomFS..."
rm -rf raw
mkdir -p raw/romfs/Contents raw/exefs

# Ren'Py package (pure Python + renpy/common), without build-only files.
cp -r build-switch/compile/renpy raw/romfs/Contents/renpy
cp renpy-source/renpy.py raw/romfs/Contents/renpy.py
# (renpy/common/_layout/*.rpym must stay: games using the classic layout
# system, layout.defaults(), load them at init.)
find raw/romfs/Contents/renpy \( -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' \
    -o -name '*.c' -o -name '*.h' -o -name '__pycache__' \) -prune -exec rm -rf {} +

# CPython standard library from NXPython (without static libs / tests).
mkdir -p raw/romfs/python
cp -r "$PY_RUNTIME/lib" raw/romfs/python/lib
rm -rf raw/romfs/python/lib/python3.14/config-3.14* \
       raw/romfs/python/lib/python3.14/site-packages \
       raw/romfs/python/lib/python3.14/lib2to3 \
       raw/romfs/python/lib/python3.14/pydoc_data
find raw/romfs/python -name '__pycache__' -prune -exec rm -rf {} +

# Pure-Python packages Ren'Py imports at startup (savetoken -> ecdsa, six).
pip install --no-deps --no-compile --target raw/romfs/python/site "six==1.17.0" "ecdsa==0.19.2"
rm -rf raw/romfs/python/site/*.dist-info raw/romfs/python/site/bin raw/romfs/python/site/ecdsa/test_*.py
# Stand-ins for CPython modules NXPython does not build (see switch/site).
cp switch/site/*.py raw/romfs/python/site/

# Precompile to bytecode with a host CPython 3.14 whose bytecode magic matches
# NXPython's. Hash-based "unchecked" pycs in __pycache__: RomFS has no
# timestamps, so mtime-based pycs would always look stale.
HOST_PY314="$(command -v python3.14 || true)"
if [ -z "$HOST_PY314" ]; then
    HOST_PY314="$(find /tmp/NXPython -type f -name python -path '*build-python*' -perm -u+x 2>/dev/null | head -1 || true)"
fi
if [ -z "$HOST_PY314" ] || ! "$HOST_PY314" -c 'import sys; assert sys.version_info[:2] == (3, 14)' 2>/dev/null; then
    pip install -q uv && uv python install 3.14 && HOST_PY314="$(uv python find 3.14)" || HOST_PY314=""
fi

TARGET_MAGIC="$(sed -n 's/^#define PYC_MAGIC_NUMBER \([0-9][0-9]*\).*/\1/p' "$PORTLIBS/include/python3.14/internal/pycore_magic_number.h" 2>/dev/null || true)"
HOST_MAGIC=""
if [ -n "$HOST_PY314" ]; then
    HOST_MAGIC="$("$HOST_PY314" -c 'import importlib.util as u; print(int.from_bytes(u.MAGIC_NUMBER[:2], "little"))' 2>/dev/null || true)"
fi

if [ -n "$TARGET_MAGIC" ] && [ "$TARGET_MAGIC" = "$HOST_MAGIC" ]; then
    echo ">>> Precompiling bytecode with $HOST_PY314 (magic $HOST_MAGIC)"
    "$HOST_PY314" -m compileall -q -j 0 --invalidation-mode unchecked-hash \
        raw/romfs/Contents/renpy raw/romfs/python/lib/python3.14 raw/romfs/python/site
else
    echo ">>> WARNING: no host CPython with bytecode magic '$TARGET_MAGIC' (got '$HOST_MAGIC' from '$HOST_PY314');"
    echo ">>>          .py files will be compiled on the console at every start."
fi

# NPDM for installable (NSP) builds. apk2switch takes it from the .nro's
# RomFS, together with the code, and hacbrewpack sets each game's title id.
mkdir -p raw/exefs raw/romfs/nsp
npdmtool switch/npdm.json raw/exefs/main.npdm
cp raw/exefs/main.npdm raw/romfs/nsp/main.npdm

# Sanity check: files Ren'Py needs at runtime.
for f in nsp/main.npdm Contents/renpy.py Contents/renpy/bootstrap.py Contents/renpy/common/00start.rpyc \
         Contents/renpy/common/_layout/screen_main_menu.rpym Contents/renpy/gl2/assimp.py \
         python/lib/python3.14/os.py python/site/six.py python/site/ecdsa/__init__.py \
         python/site/_posixsubprocess.py python/site/_switch_compat.py; do
    [ -f "raw/romfs/$f" ] || { echo "ERROR: missing raw/romfs/$f" >&2; exit 1; }
done

# ─── 5. NRO / NSO ────────────────────────────────────────────────────────────
echo ">>> Packaging..."
cp "$ELF" raw/renpy-switch.elf
elf2nso "$ELF" raw/exefs/main

ICON="${APP_ICON:-$DEVKITPRO/libnx/default_icon.jpg}"
nacptool --create "$APP_NAME" "$APP_AUTHOR" "$APP_VERSION" raw/control.nacp
elf2nro "$ELF" raw/renpy-switch.nro --romfsdir=raw/romfs --nacp=raw/control.nacp --icon="$ICON"

echo ">>> Done. Output in ./raw:"
ls -la raw
