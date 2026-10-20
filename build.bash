#!/usr/bin/env bash
# Builds Ren'Py 8.5.3 for the Nintendo Switch. Run setup.bash first.
#
# Output (./raw):
#   raw/renpy-switch.nro     generic launcher: put it in sdmc:/switch/<name>/
#                            together with the game's "game" folder
#   raw/renpy-switch.elf     ELF, for re-packing with a custom title/icon
#   raw/exefs/main           NSO, for ExeFS / installable builds
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
find raw/romfs/Contents/renpy \( -name '*.pyx' -o -name '*.pxd' -o -name '*.pxi' \
    -o -name '*.c' -o -name '*.h' -o -name '*.rpym' -o -name '__pycache__' \) -prune -exec rm -rf {} +
rm -rf raw/romfs/Contents/renpy/common/_layout

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
rm -rf raw/romfs/python/site/*.dist-info raw/romfs/python/site/bin
# Stand-ins for CPython modules NXPython does not build (see switch/site).
cp switch/site/*.py raw/romfs/python/site/

# Precompile to bytecode with a host CPython 3.14 when one is available
# (NXPython builds one when the host lacks it). Saves a lot of startup time.
HOST_PY314="$(command -v python3.14 || true)"
if [ -z "$HOST_PY314" ]; then
    HOST_PY314="$(find /tmp/NXPython -type f -name python -path '*build-python*' -perm -u+x 2>/dev/null | head -1 || true)"
fi
if [ -n "$HOST_PY314" ] && "$HOST_PY314" -c 'import sys; assert sys.version_info[:2] == (3, 14)' 2>/dev/null; then
    echo ">>> Precompiling .pyc with $HOST_PY314"
    # -b: legacy foo.pyc next to foo.py (what the RomFS importer finds first).
    "$HOST_PY314" -OO -m compileall -b -q -j 0 raw/romfs/Contents/renpy raw/romfs/Contents/renpy.py \
        raw/romfs/python/lib/python3.14 raw/romfs/python/site || true
else
    echo ">>> WARNING: no host CPython 3.14 found; .py will be compiled on the console at every start."
fi

# ─── 5. NRO / NSO ────────────────────────────────────────────────────────────
echo ">>> Packaging..."
cp "$ELF" raw/renpy-switch.elf
elf2nso "$ELF" raw/exefs/main

ICON="${APP_ICON:-$DEVKITPRO/libnx/default_icon.jpg}"
nacptool --create "$APP_NAME" "$APP_AUTHOR" "$APP_VERSION" raw/control.nacp
elf2nro "$ELF" raw/renpy-switch.nro --romfsdir=raw/romfs --nacp=raw/control.nacp --icon="$ICON"

echo ">>> Done. Output in ./raw:"
ls -la raw
