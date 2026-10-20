#!/usr/bin/env bash
# Re-packages the generic Ren'Py launcher (raw/renpy-switch.elf + raw/romfs)
# into an .nro with your own title, author, version and icon.
#
# By default the game is NOT embedded: copy the game's "game" folder next to
# the .nro on the SD card instead (no FAT32 4 GB file limit, easy updates):
#
#     sdmc:/switch/subahibi/subahibi.nro
#     sdmc:/switch/subahibi/game/...
#
# Use --embed <game folder> to put the game inside the .nro's RomFS.
#
# Usage:
#   bash pack_nro.bash <output.nro> "<Title>" [-a author] [-v version]
#                      [--icon icon.jpg] [--embed path/to/game]
set -euo pipefail

export DEVKITPRO="${DEVKITPRO:-/opt/devkitpro}"
export PATH="$DEVKITPRO/tools/bin:$PATH"

usage() { sed -n '2,16p' "$0"; }

OUT=""; TITLE=""; AUTHOR="Ren'Py"; VERSION="1.0.0"; ICON=""; EMBED=""
POS=()
while [ $# -gt 0 ]; do
    case "$1" in
        -a|--author) AUTHOR="$2"; shift 2 ;;
        -v|--version) VERSION="$2"; shift 2 ;;
        --icon) ICON="$2"; shift 2 ;;
        --embed) EMBED="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option $1" >&2; usage; exit 1 ;;
        *) POS+=("$1"); shift ;;
    esac
done
OUT="${POS[0]:-}"; TITLE="${POS[1]:-}"
[ -n "$OUT" ] && [ -n "$TITLE" ] || { usage; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RAW="$SCRIPT_DIR/raw"
[ -f "$RAW/renpy-switch.elf" ] && [ -d "$RAW/romfs/Contents/renpy" ] || {
    echo "ERROR: $RAW is incomplete. Build first (build.bash) or unpack the CI artifact into ./raw." >&2
    exit 1
}
for t in elf2nro nacptool; do
    command -v "$t" >/dev/null || { echo "ERROR: $t not found (install devkitPro switch-tools)." >&2; exit 1; }
done

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cp -r "$RAW/romfs" "$STAGE/romfs"

if [ -n "$EMBED" ]; then
    [ -d "$EMBED" ] || { echo "ERROR: $EMBED is not a directory." >&2; exit 1; }
    [ "$(basename "$EMBED")" = "game" ] || [ ! -d "$EMBED/game" ] || EMBED="$EMBED/game"
    echo ">>> Embedding $EMBED"
    mkdir -p "$STAGE/romfs/Contents/game"
    cp -r "$EMBED/." "$STAGE/romfs/Contents/game/"
fi

[ -n "$ICON" ] || ICON="$DEVKITPRO/libnx/default_icon.jpg"

nacptool --create "$TITLE" "$AUTHOR" "$VERSION" "$STAGE/control.nacp"
elf2nro "$RAW/renpy-switch.elf" "$OUT" --romfsdir="$STAGE/romfs" --nacp="$STAGE/control.nacp" --icon="$ICON"

echo ">>> Created $OUT"
if [ -z "$EMBED" ]; then
    echo "    Copy it to sdmc:/switch/<name>/ and put the game's \"game\" folder next to it."
fi
