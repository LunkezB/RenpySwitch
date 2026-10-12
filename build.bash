#!/usr/bin/env bash
set -euo pipefail

export DEVKITPRO=/opt/devkitpro
export RENPY_VER=8.4.1

CLEAN=false

usage() {
    echo "Usage: $0 [--clean]"
    echo "  --clean   remove generated build caches before rebuilding"
}

for arg in "$@"; do
    case "$arg" in
        --clean)
            CLEAN=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            echo "ERROR: unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

# ─── Activate venv created by setup.bash ─────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
cd "$SCRIPT_DIR"

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "ERROR: Venv not found at $VENV_DIR"
    echo "       Please run setup.bash first."
    exit 1
fi

if [ ! -d renpy_sdk ]; then
    echo "ERROR: renpy_sdk not found."
    echo "       Please run setup.bash first to extract the Ren'Py SDK."
    exit 1
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
export PATH="$VENV_DIR/bin:$PATH"
export RENPY_CYTHON="$(which cython 2>/dev/null || echo "$VENV_DIR/bin/cython")"

echo ">>> Building C extensions using Cython: $RENPY_CYTHON"

echo ">>> Using Python: $(which python) — $(python --version)"
echo ">>> Using Pip:    $(which pip)"

if [ "$CLEAN" = true ]; then
    echo ">>> --clean specified. Removing generated build caches."
    rm -rf build-switch switch/build
    rm -rf pygame_sdl2-source/build pygame_sdl2-source/gen pygame_sdl2-source/gen-static pygame_sdl2-source/gen3 pygame_sdl2-source/gen3-static
    rm -rf renpy-source/module/build renpy-source/module/gen renpy-source/module/gen-static renpy-source/module/gen3 renpy-source/module/gen3-static
fi

# ─── Build pygame_sdl2 C extensions ──────────────────────────────────────────
pushd pygame_sdl2-source
mkdir -p gen3 gen3-static
python setup.py build || true
PYGAME_SDL2_STATIC=1 python setup.py build || true
popd

# ─── Build Ren'Py C extensions ───────────────────────────────────────────────
pushd renpy-source/module
mkdir -p gen3 gen3-static
RENPY_DEPS_INSTALL=/usr/lib/x86_64-linux-gnu:/usr:/usr/local python setup.py build || true
RENPY_DEPS_INSTALL=/usr/lib/x86_64-linux-gnu:/usr:/usr/local RENPY_STATIC=1 python setup.py build || true
popd

# ─── Install pygame_sdl2 into the venv ───────────────────────────────────────
pushd pygame_sdl2-source
python setup.py build
python setup.py install_headers
python setup.py install
popd

# ─── Install Ren'Py module into the venv ─────────────────────────────────────
pushd renpy-source/module
RENPY_DEPS_INSTALL=/usr/lib/x86_64-linux-gnu:/usr:/usr/local python setup.py build
RENPY_DEPS_INSTALL=/usr/lib/x86_64-linux-gnu:/usr:/usr/local python setup.py install
popd

# ─── Link generated C sources for CMake build ────────────────────────────────
bash link_sources.bash

export PREFIXARCHIVE=$(realpath renpy-switch-modules.tar.gz)

mkdir -p build-switch
pushd build-switch
mkdir -p local_prefix
export LOCAL_PREFIX=$(realpath local_prefix)
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build .
mkdir -p $LOCAL_PREFIX/lib
cp librenpy-switch-modules.a $LOCAL_PREFIX/lib/librenpy-switch-modules.a
popd

tar -czvf $PREFIXARCHIVE -C $LOCAL_PREFIX .
tar -xf renpy-switch-modules.tar.gz -C $DEVKITPRO/portlibs/switch
rm renpy-switch-modules.tar.gz

source /opt/devkitpro/switchvars.sh

# ─── Cross-compile Switch executable ─────────────────────────────────────────
if [ ! -f switch/CMakeLists.txt ]; then
    echo "ERROR: switch/CMakeLists.txt is missing."
    echo "       The Switch executable sources are required to build raw/switch/exefs/main."
    exit 1
fi

pushd switch
mkdir -p build
pushd build
cmake ..
cmake --build .
popd
popd

mkdir -p ./raw/switch/exefs
cp ./switch/build/renpy-switch.nso ./raw/switch/exefs/main

# ─── Assemble Ren'Py project tree ────────────────────────────────────────────
rm -rf renpy_clear
mkdir renpy_clear
cp ./renpy_sdk/*/renpy.sh ./renpy_clear/renpy.sh
cp -r ./renpy_sdk/*/lib ./renpy_clear/lib
mkdir ./renpy_clear/game
cp -r ./renpy-source/module ./renpy_clear/module
cp -r ./renpy-source/renpy ./renpy_clear/renpy
cp ./renpy-source/renpy.py ./renpy_clear/renpy.py
cp ./script.rpy ./renpy_clear/game/script.rpy
cp ./renpy_sdk/*/*.exe ./renpy_clear/ || true
rm -rf ./renpy_clear/lib/*mac*
rm -rf ./renpy_clear/renpy/common/_layout

pushd renpy_clear
./renpy.sh . compile
find ./renpy/ -regex ".*\.\(pxd\|pyx\|rpym\|pxi\)" -delete
popd

PYTHON_LIB_DIR="$(find renpy_clear/lib -maxdepth 1 -type d -name 'python3.*' | sort -V | tail -n 1)"
if [ -z "$PYTHON_LIB_DIR" ]; then
    echo "ERROR: could not find a Python stdlib directory under renpy_clear/lib."
    exit 1
fi

# ─── Generate private archive ────────────────────────────────────────────────
rm -rf private
mkdir private
mkdir private/lib
cp -r renpy_clear/renpy private/renpy
cp -r "$PYTHON_LIB_DIR" private/lib/
cp renpy_clear/renpy.py private/main.py
rm -rf private/renpy/common
python generate_private.py
rm -rf private

# ─── Assemble final Switch romfs layout ──────────────────────────────────────
rm -rf ./raw/lib ./raw/renpy_clear
rm -rf ./raw/switch/romfs/Contents/lib.zip
rm -rf ./raw/switch/romfs/Contents/renpy/common
rm -f ./raw/switch/romfs/Contents/renpy.py
mkdir -p ./raw/switch/romfs/Contents/renpy
mkdir -p ./raw/lib
cp -r ./renpy_clear/renpy/common ./raw/switch/romfs/Contents/renpy/
cp ./renpy_clear/renpy.py ./raw/switch/romfs/Contents/
cp -r "$PYTHON_LIB_DIR"/. ./raw/lib
cp -r ./renpy_clear/renpy ./raw/lib
rm -rf ./raw/lib/renpy/common/
7z a -tzip ./raw/switch/romfs/Contents/lib.zip ./raw/lib/*
rm -rf ./raw/lib
rm -rf ./renpy_clear/game
mv ./renpy_clear/ ./raw/

echo ">>> build.bash complete. Output is in ./raw/"
