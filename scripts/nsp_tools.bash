#!/usr/bin/env bash
# Builds the tools for installable (NSP) builds and checks the NSP path.
# Run after build.bash (needs raw/renpy-switch.nro and raw/exefs/main).
#
#   hacbrewpack  (Linux, and hacbrewpack.exe for Windows): makes the NSP on
#                the user's PC from apk2switch.py's files and the user's keys
#   hactool      (Linux, CI only): unpacks the test NSP again
#
# Output: dist/  renpy-switch.nro, apk2switch.py, hacbrewpack.exe,
#                hacbrewpack (Linux), hacbrewpack-LICENSE.txt
set -euo pipefail
cd "$(dirname "$0")/.."

HBP_REPO=https://github.com/rlaphoenix/hacBrewPack.git     # mirror of The-4n/hacBrewPack 3.05
HBP_REV=745b16ecfc9ce055743067d200572204cb2aac6c
HACTOOL_REPO=https://github.com/SciresM/hactool.git
HACTOOL_REV=1d64a83450e025622f3468c28fc4164dad2c5ef6

T=tools-build
rm -rf "$T" dist
mkdir -p "$T" dist

need=()
command -v x86_64-w64-mingw32-gcc >/dev/null || need+=(gcc-mingw-w64-x86-64)
command -v gcc >/dev/null || need+=(gcc)
command -v make >/dev/null || need+=(make)
command -v git >/dev/null || need+=(git)
python3 -c "import venv" 2>/dev/null || need+=(python3-venv)
if [ ${#need[@]} -gt 0 ]; then
    echo ">>> apt-get install ${need[*]}"
    apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" >/dev/null
fi

fetch() {  # repo rev dir
    git init -q "$3"
    git -C "$3" fetch -q --depth 1 "$1" "$2"
    git -C "$3" checkout -q FETCH_HEAD
}

echo ">>> hacbrewpack (Linux)"
fetch "$HBP_REPO" "$HBP_REV" "$T/hbp-linux"
cp -r "$T/hbp-linux" "$T/hbp-win"
(cd "$T/hbp-linux" && cp config.mk.template config.mk && make -j"$(nproc)" >build.log 2>&1) \
    || { tail -30 "$T/hbp-linux/build.log"; exit 1; }

echo ">>> hacbrewpack.exe (Windows, mingw-w64, static)"
(
    cd "$T/hbp-win"
    printf 'CC = x86_64-w64-mingw32-gcc\nAR = x86_64-w64-mingw32-ar\nCFLAGS = -O2 -Wall -std=gnu11 -DWIN32 -D_WIN32\nLDFLAGS = -lmbedtls -lmbedx509 -lmbedcrypto -static\n' > config.mk
    make -C mbedtls lib CC=x86_64-w64-mingw32-gcc AR=x86_64-w64-mingw32-ar WINDOWS_BUILD=1 >build.log 2>&1
    make hacbrewpack >>build.log 2>&1
) || { tail -30 "$T/hbp-win/build.log"; exit 1; }

echo ">>> hactool (Linux, for the check)"
fetch "$HACTOOL_REPO" "$HACTOOL_REV" "$T/hactool"
(cd "$T/hactool" && cp config.mk.template config.mk && make -j"$(nproc)" >build.log 2>&1) \
    || { tail -30 "$T/hactool/build.log"; exit 1; }

echo ">>> Python with Pillow (icons)"
python3 -m venv "$T/venv"
"$T/venv/bin/pip" install -q pillow

echo ">>> Check: apk2switch --pack"
"$T/venv/bin/python" scripts/check_nsp.py pack "$T/packtest"

echo ">>> Check: engine code taken from the .nro == elf2nso output"
"$T/venv/bin/python" scripts/check_nsp.py nso raw/renpy-switch.nro raw/exefs/main

echo ">>> Check: NSP made by apk2switch.py (test game, throwaway keys)"
"$T/venv/bin/python" scripts/check_nsp.py nsp raw/renpy-switch.nro \
    "$T/hbp-linux/hacbrewpack" "$T/hactool/hactool" "$T/nsptest"

cp raw/renpy-switch.nro tools/apk2switch.py "$T/hbp-win/hacbrewpack.exe" "$T/hbp-linux/hacbrewpack" dist/
cp "$T/hbp-linux/LICENSE" dist/hacbrewpack-LICENSE.txt
ls -la dist
