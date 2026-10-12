#!/usr/bin/env bash
set -e

export DEVKITPRO="${DEVKITPRO:-/opt/devkitpro}"
export RENPY_VER=8.4.1
export PYGAME_SDL2_VER=2.1.0

# ─── Directories & Caching Setup ─────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
CACHE_DIR="$SCRIPT_DIR/downloads"

mkdir -p "$CACHE_DIR"

FORCE_CLEAN=false
if [ "${1:-}" = "--force" ] || [ "${1:-}" = "-f" ]; then
    FORCE_CLEAN=true
    echo ">>> '--force' specified. Will re-extract and re-patch source directories."
fi

download_cached() {
    local url="$1"
    local filename
    filename="$(basename "$url")"
    local target="$CACHE_DIR/$filename"

    if [ -f "$target" ]; then
        echo ">>> [Cache Hit] $filename"
    else
        echo ">>> Downloading $url -> $target..."
        curl -L -C - "$url" -o "$target"
    fi
}

# ─── System Packages & Dependencies ──────────────────────────────────────────
apt-get -y update
apt-get -y upgrade

apt-get -y install build-essential \
    libncurses-dev libssl-dev libsqlite3-dev tk-dev libgdbm-dev libc6-dev \
    python3 python3-dev python3-pip python3-venv \
    p7zip-full libsdl2-dev libsdl2-image-dev libjpeg-dev libpng-dev \
    libsdl2-ttf-dev libsdl2-mixer-dev libavformat-dev libfreetype-dev \
    libswscale-dev libglew-dev libfribidi-dev libavcodec-dev libswresample-dev \
    libsdl2-gfx-dev libgl1 libegl-dev libgles-dev unzip curl cmake icoutils

# ─── Create / reuse virtual environment ──────────────────────────────────────
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo ">>> Creating Python venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
else
    echo ">>> Using existing Python venv at $VENV_DIR"
fi

# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# Upgrade pip & Install build-time Python dependencies inside the venv
pip install Cython==3.0.10 setuptools future six

echo ">>> Python: $(which python) — $(python --version)"
echo ">>> Pip:    $(which pip) — $(pip --version)"

# ─── NXPython — Python 3.14 for Switch (Cached Check) ─────────────────────────
if [ -f "$DEVKITPRO/portlibs/switch/lib/libpython3.14.a" ] && [ -f "$DEVKITPRO/portlibs/switch/include/python3.14/pyconfig.h" ]; then
    echo ">>> [Cache Hit] NXPython (Python 3.14 portlib) is already installed at $DEVKITPRO/portlibs/switch."
else
    echo ">>> Building NXPython (Python 3.14 for Switch)..."
    NXPYTHON_COMMIT=1b1c61a94f0685bb0be707b5374376f3084159fe
    rm -rf /tmp/NXPython
    git clone https://github.com/jvrcruzGAMES/NXPython /tmp/NXPython
    cd /tmp/NXPython
    CPYTHON_REF=$NXPYTHON_COMMIT bash build.sh
    cd "$SCRIPT_DIR"
fi

echo ">>> Installing devkitPro Switch portlibs (Mesa, SDL2, FFmpeg, etc.)..."
dkp-pacman -S switch-mesa switch-glad switch-libdrm_nouveau \
    switch-sdl2 switch-sdl2_gfx switch-sdl2_image switch-sdl2_mixer switch-sdl2_ttf \
    switch-ffmpeg switch-freetype switch-curl switch-mbedtls switch-liblzma \
    switch-libzstd switch-zlib switch-bzip2 --noconfirm --needed

# ─── devkitPro Portlibs Packages (Cached Downloads) ─────────────────────────
#HELPERS_PKG="devkitpro-pkgbuild-helpers-2.2.4-2-any.pkg.tar.xz"
#FRIBIDI_PKG="switch-libfribidi-1.0.12-1-any.pkg.tar.xz"

#download_cached "https://github.com/knautilus/Utils/releases/download/v1.0/$HELPERS_PKG"
#download_cached "https://github.com/knautilus/Utils/releases/download/v1.0/$FRIBIDI_PKG"

# --- Purge conflicting legacy package before running local package installer ---
#dkp-pacman -Rns --noconfirm dkp-meson-scripts || true

#dkp-pacman -U --noconfirm "$CACHE_DIR/$HELPERS_PKG"
#dkp-pacman -U --noconfirm "$CACHE_DIR/$FRIBIDI_PKG"

#/bin/bash -c 'sed -i'"'"'.bak'"'"' '"'"'s/set(CMAKE_EXE_LINKER_FLAGS_INIT "/set(CMAKE_EXE_LINKER_FLAGS_INIT "-fPIC /'"'"' $DEVKITPRO/switch.cmake'
if [ -f "$DEVKITPRO/cmake/Switch.cmake" ]; then
    sed -i.bak 's/set(CMAKE_EXE_LINKER_FLAGS_INIT "/set(CMAKE_EXE_LINKER_FLAGS_INIT "-fPIC /' "$DEVKITPRO/cmake/Switch.cmake"
    echo ">>> Successfully patched $DEVKITPRO/cmake/Switch.cmake"
else
    echo ">>> WARNING: $DEVKITPRO/cmake/Switch.cmake not found. Skipping patch."
fi

# ─── Ren'Py & SDL2 Downloads (Cached in ./downloads/) ─────────────────────────
PYGAME_TAR="pygame_sdl2-$PYGAME_SDL2_VER+renpy$RENPY_VER.tar.gz"
SDK_ZIP="renpy-$RENPY_VER-sdk.zip"
SOURCE_TAR="renpy-$RENPY_VER-source.tar.bz2"

download_cached "https://www.renpy.org/dl/$RENPY_VER/$PYGAME_TAR"
download_cached "https://www.renpy.org/dl/$RENPY_VER/$SDK_ZIP"
download_cached "https://www.renpy.org/dl/$RENPY_VER/$SOURCE_TAR"

# ─── Source Extraction & Patching (Cached Folders) ─────────────────────────────
# 1. pygame_sdl2-source
if [ "$FORCE_CLEAN" = true ] || [ ! -f "pygame_sdl2-source/setup.py" ]; then
    echo ">>> Extracting pygame_sdl2 source..."
    rm -rf pygame_sdl2-source "pygame_sdl2-$PYGAME_SDL2_VER+renpy$RENPY_VER"
    tar -xf "$CACHE_DIR/$PYGAME_TAR"
    mv "pygame_sdl2-$PYGAME_SDL2_VER+renpy$RENPY_VER" pygame_sdl2-source
    pushd pygame_sdl2-source >/dev/null
    rm -rf gen gen-static gen3 gen3-static
    popd >/dev/null
else
    echo ">>> [Cache Hit] Existing 'pygame_sdl2-source' directory used."
fi

# 2. renpy-source & renpy.patch
if [ "$FORCE_CLEAN" = true ] || [ ! -d "renpy-source/module" ]; then
    echo ">>> Extracting and patching renpy-source..."
    rm -rf renpy-source "renpy-$RENPY_VER-source"
    tar -xf "$CACHE_DIR/$SOURCE_TAR"
    mv "renpy-$RENPY_VER-source" renpy-source
    pushd renpy-source >/dev/null
    patch -p1 --no-backup-if-mismatch < ../renpy.patch
    pushd module >/dev/null
    rm -rf gen gen-static gen3 gen3-static
    popd >/dev/null
    popd >/dev/null
else
    echo ">>> [Cache Hit] Existing 'renpy-source' directory used."
fi

# 3. renpy_sdk
if [ "$FORCE_CLEAN" = true ] || [ ! -d "renpy_sdk" ]; then
    echo ">>> Unzipping renpy_sdk..."
    rm -rf renpy_sdk "renpy-$RENPY_VER-sdk"
    unzip -qq "$CACHE_DIR/$SDK_ZIP" -d renpy_sdk
else
    echo ">>> [Cache Hit] Existing 'renpy_sdk' directory used."
fi

echo "=========================================================================="
echo " SUCCESS: Setup complete! All downloads and sources are cached."
echo " Virtual environment: $VENV_DIR"
echo " Downloads cache:    $CACHE_DIR"
echo " Run 'bash build.bash' to compile."
echo "=========================================================================="
