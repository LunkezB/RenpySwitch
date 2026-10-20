#!/usr/bin/env bash
# Prepares everything needed to build Ren'Py 8.5.3 for the Nintendo Switch:
# devkitPro portlibs, NXPython (CPython 3.14), Ren'Py sources (patched) and
# the Ren'Py SDK (used on the host to precompile renpy/common).
#
# Run inside the devkitpro/devkita64 container (or a machine with devkitPro):
#     sudo bash setup.bash            # first time
#     sudo bash setup.bash --force    # re-extract and re-patch sources
set -euo pipefail

export DEVKITPRO="${DEVKITPRO:-/opt/devkitpro}"
export RENPY_VER=8.5.3
export CYTHON_VER=3.2.4

# NXPython (patch set for CPython 3.14) and the CPython commit it targets.
NXPYTHON_REPO=https://github.com/jvrcruzGAMES/NXPython
NXPYTHON_REV=86cde18
CPYTHON_REF=1b1c61a94f0685bb0be707b5374376f3084159fe

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CACHE_DIR="$SCRIPT_DIR/downloads"
cd "$SCRIPT_DIR"
mkdir -p "$CACHE_DIR"

FORCE_CLEAN=false
if [ "${1:-}" = "--force" ] || [ "${1:-}" = "-f" ]; then
    FORCE_CLEAN=true
fi

download_cached() {
    local url="$1"
    local target="$CACHE_DIR/$(basename "$url")"
    if [ -f "$target" ]; then
        echo ">>> [cache] $(basename "$url")"
    else
        echo ">>> Downloading $url"
        curl -fL --retry 3 -o "$target.part" "$url"
        mv "$target.part" "$target"
    fi
}

# ─── Host packages ───────────────────────────────────────────────────────────
if command -v apt-get >/dev/null 2>&1; then
    apt-get -y update
    apt-get -y install --no-install-recommends \
        build-essential git curl ca-certificates unzip p7zip-full bzip2 \
        cmake pkg-config python3 python3-dev python3-pip python3-venv \
        libssl-dev zlib1g-dev libffi-dev libbz2-dev liblzma-dev libsqlite3-dev
fi

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    python3 -m venv "$VENV_DIR"
fi
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install "Cython==$CYTHON_VER" setuptools

# ─── devkitPro portlibs (before NXPython: it links against several) ──────────
# The devkitpro/devkita64 image already ships switch-dev + switch-portlibs, and
# pkg.devkitpro.org may refuse CI runners, so only install what is missing.
NEEDED_PKGS=(
    switch-pkg-config switch-cmake libnx switch-tools
    switch-mesa switch-libdrm_nouveau switch-glad
    switch-sdl2 switch-sdl2_image
    switch-freetype switch-harfbuzz switch-libfribidi
    switch-ffmpeg switch-dav1d
    switch-libpng switch-libjpeg-turbo switch-libwebp
    switch-zlib switch-bzip2 switch-liblzma switch-libzstd
    switch-mbedtls switch-curl
)
MISSING_PKGS=()
for p in "${NEEDED_PKGS[@]}"; do
    dkp-pacman -Q "$p" >/dev/null 2>&1 || MISSING_PKGS+=("$p")
done
if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo ">>> Installing missing devkitPro packages: ${MISSING_PKGS[*]}"
    dkp-pacman -Sy --noconfirm
    dkp-pacman -S --noconfirm --needed "${MISSING_PKGS[@]}"
else
    echo ">>> All devkitPro packages already installed."
fi

if [ -f "$DEVKITPRO/cmake/Switch.cmake" ] && ! grep -q 'LINKER_FLAGS_INIT "-fPIC' "$DEVKITPRO/cmake/Switch.cmake"; then
    sed -i.bak 's/set(CMAKE_EXE_LINKER_FLAGS_INIT "/set(CMAKE_EXE_LINKER_FLAGS_INIT "-fPIC /' "$DEVKITPRO/cmake/Switch.cmake"
fi

# ─── NXPython: CPython 3.14 for the Switch ───────────────────────────────────
PORTLIBS="$DEVKITPRO/portlibs/switch"
if [ -f "$PORTLIBS/lib/libpython3.14.a" ] && [ -d "$PORTLIBS/share/python-switch/romfs/python/lib/python3.14" ]; then
    echo ">>> [cache] NXPython already installed in portlibs."
else
    echo ">>> Building NXPython (CPython 3.14 for Switch)..."
    rm -rf /tmp/NXPython
    git clone "$NXPYTHON_REPO" /tmp/NXPython
    git -C /tmp/NXPython checkout "$NXPYTHON_REV"
    (cd /tmp/NXPython && CPYTHON_REF="$CPYTHON_REF" bash build.sh -j "$(nproc)")
fi

# ─── Ren'Py source + SDK ─────────────────────────────────────────────────────
SOURCE_TAR="renpy-$RENPY_VER-source.tar.bz2"
SDK_TAR="renpy-$RENPY_VER-sdk.tar.bz2"
download_cached "https://www.renpy.org/dl/$RENPY_VER/$SOURCE_TAR"
download_cached "https://www.renpy.org/dl/$RENPY_VER/$SDK_TAR"

if [ "$FORCE_CLEAN" = true ] || [ ! -f renpy-source/setup.py ]; then
    echo ">>> Extracting and patching Ren'Py $RENPY_VER source..."
    rm -rf renpy-source "renpy-$RENPY_VER-source"
    tar -xf "$CACHE_DIR/$SOURCE_TAR"
    mv "renpy-$RENPY_VER-source" renpy-source
    (cd renpy-source && patch -p1 --no-backup-if-mismatch < ../renpy.patch)
fi

if [ "$FORCE_CLEAN" = true ] || [ ! -d renpy_sdk ]; then
    echo ">>> Extracting Ren'Py $RENPY_VER SDK..."
    rm -rf renpy_sdk "renpy-$RENPY_VER-sdk"
    tar -xf "$CACHE_DIR/$SDK_TAR"
    mv "renpy-$RENPY_VER-sdk" renpy_sdk
fi

echo "=========================================================================="
echo " Setup complete. Now run: sudo bash build.bash"
echo "=========================================================================="
