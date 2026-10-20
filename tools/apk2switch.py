#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apk2switch: turn a Ren'Py Android APK into a folder for the Nintendo Switch
homebrew build of Ren'Py 8.5 (RenpySwitch).

What it does
  1. Extracts the game from the APK (assets/x-game/...) and removes the "x-"
     prefixes Ren'Py's Android packager (RAPT) adds to every file and folder.
  2. Uses the APK's presplash as presplash.jpg (shown while loading).
  3. Adds zz_switch_controls.rpy (Joy-Con + touch controls).
  4. Checks the files: Ren'Py version, image/video formats the Switch build
     can't decode (AVIF/JXL/SVG), size. --convert-avif converts AVIF images
     in place (needs Pillow >= 11.2).
  5. Optionally copies the launcher .nro and gives it its own title, author
     and icon (the icon is taken from the APK; needs Pillow).

Result (copy the "switch" folder to the root of the SD card):
  <out>/switch/<name>/<name>.nro
  <out>/switch/<name>/game/...

Usage
  python apk2switch.py game.apk
  python apk2switch.py game.apk --nro renpy-switch.nro --name subahibi --title "Subarashiki Hibi" --pack
  python apk2switch.py path/to/switch/subahibi/game --pack      (pack an already converted folder)

--pack puts images, audio and video into a few .rpa archives (Ren'Py's own
archive format), so copying to the SD card is fast; scripts stay loose.

Only plain Python 3.8+ is required; Pillow is optional (icon, AVIF).
"""

import argparse
import io
import os
import pickle
import shutil
import struct
import sys
import zipfile
import zlib
from collections import Counter

CONTROLS_RPY = r'''# Nintendo Switch controls for Ren'Py games (RenpySwitch).
#
# Drop this file into the game's "game" folder. It only does anything on the
# Switch build (renpy.switch); on PC/Android it is ignored.
#
# Joy-Con / Pro Controller (Nintendo button labels):
#   A            next line / select
#   B            back / open the game menu
#   X, Minus     hide the text box (look at the picture)
#   Y            history (backlog)
#   ZR           skip on/off
#   ZL           auto mode on/off
#   L            quick save
#   R            quick load
#   Plus         game menu
#   Right stick  up = previous line (rollback), down = next line (rollforward)
#   Left stick / D-pad   move between buttons and menu choices
#
# Touch screen (handheld):
#   tap = next line / press a button, long press = game menu,
#   swipe up = history, swipe down = hide the text box.
#
# Button names below are SDL's positional (Xbox-style) names. With the
# devkitPro SDL2 mapping the physical Nintendo buttons are:
#   A -> "b", B -> "a", X -> "y", Y -> "x", L/R -> shoulders, ZL/ZR -> triggers,
#   Plus -> "start", Minus -> "back".

init 1000 python:

    if getattr(renpy, "switch", False):

        def _switch_bind(control, *events):
            config.pad_bindings[control] = list(events)

        # Face buttons.
        _switch_bind("pad_b_press", "dismiss", "button_select", "bar_activate", "bar_deactivate")  # A
        _switch_bind("pad_a_press", "game_menu")                                                   # B
        _switch_bind("pad_y_press", "hide_windows")                                                # X
        _switch_bind("pad_x_press", "switch_history")                                              # Y

        # Plus / Minus.
        _switch_bind("pad_start_press", "game_menu")
        _switch_bind("pad_guide_press", "game_menu")
        _switch_bind("pad_back_press", "hide_windows")
        config.pad_bindings.pop("repeat_pad_back_press", None)

        # Triggers: ZR skip toggle, ZL auto-forward toggle.
        _switch_bind("pad_righttrigger_pos", "switch_skip")
        _switch_bind("pad_lefttrigger_pos", "switch_auto")
        config.pad_bindings.pop("repeat_pad_lefttrigger_pos", None)

        # Shoulders: quick save / quick load.
        _switch_bind("pad_leftshoulder_press", "switch_quicksave")
        _switch_bind("pad_rightshoulder_press", "switch_quickload")
        config.pad_bindings.pop("repeat_pad_leftshoulder_press", None)
        config.pad_bindings.pop("repeat_pad_rightshoulder_press", None)

        # Right stick vertical: rollback / rollforward (like a mouse wheel).
        # Horizontal stays on focus movement.
        for _prefix in ("", "repeat_"):
            config.pad_bindings[_prefix + "pad_righty_neg"] = ["rollback"]
            config.pad_bindings[_prefix + "pad_righty_pos"] = ["rollforward"]

        # Touch gestures (only active in handheld mode, "touch" variant).
        config.gestures.update({
            "n": "switch_history",
            "s": "hide_windows",
        })

        # Actions for the custom event names used above. The underlay is
        # active everywhere, so these work in game and in menus.
        config.underlay.append(
            renpy.Keymap(
                switch_history=ShowMenu("history") if renpy.has_screen("history") else NullAction(),
                switch_skip=Skip(),
                switch_auto=Preference("auto-forward", "toggle"),
                switch_quicksave=QuickSave(),
                switch_quickload=QuickLoad(confirm=False),
            )
        )

        # Register the custom event names (no keyboard keys bound to them).
        config.keymap.setdefault("switch_history", [])
        config.keymap.setdefault("switch_skip", [])
        config.keymap.setdefault("switch_auto", [])
        config.keymap.setdefault("switch_quicksave", [])
        config.keymap.setdefault("switch_quickload", [])
'''

UNSUPPORTED_IMAGES = {".avif": "AVIF", ".jxl": "JPEG XL", ".svg": "SVG"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".jxl", ".svg", ".bmp", ".gif", ".tga"}
VIDEO_EXTS = {".webm", ".mp4", ".mkv", ".ogv", ".avi", ".mpg", ".mpeg", ".mov"}
AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".flac", ".m4a"}


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "{:.1f} {}".format(n, unit) if unit != "B" else "{} B".format(n)
        n /= 1024.0


def strip_x(component):
    return component[2:] if component.startswith("x-") else component


def say(*args):
    print(*args)
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Extraction


def extract_game(apk, out_game):
    """Extracts assets/x-game/** into out_game with RAPT prefixes removed."""

    prefix = "assets/x-game/"
    members = [i for i in apk.infolist() if i.filename.startswith(prefix) and not i.is_dir()]

    if not members:
        raise SystemExit(
            "ERROR: assets/x-game/ not found in the APK.\n"
            "This does not look like a Ren'Py Android build (or the game is in an .obb file).")

    total = sum(i.file_size for i in members)
    say("Extracting {} files ({}) ...".format(len(members), human(total)))

    done = 0
    last_pct = -1
    names = []

    for info in members:
        rel = info.filename[len(prefix):]
        parts = [strip_x(p) for p in rel.split("/") if p]
        if not parts or any(p in ("..", ".") for p in parts):
            continue

        dest = os.path.join(out_game, *parts)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        with apk.open(info) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)

        names.append("/".join(parts))
        done += info.file_size
        pct = int(done * 100 / max(total, 1))
        if pct // 5 != last_pct // 5:
            say("  {:3d}%".format(pct))
            last_pct = pct

    return names


def read_member(apk, name):
    try:
        return apk.read(name)
    except KeyError:
        return None


# ---------------------------------------------------------------------------
# Checks


def check_game(out_game, names):
    report = {}

    version_file = os.path.join(out_game, "script_version.txt")
    if os.path.exists(version_file):
        with open(version_file, "r", encoding="utf-8", errors="replace") as f:
            report["script_version"] = f.read().strip()

    exts = Counter(os.path.splitext(n)[1].lower() for n in names)
    report["exts"] = exts

    report["rpy"] = exts.get(".rpy", 0)
    report["rpyc"] = exts.get(".rpyc", 0)
    report["rpa"] = exts.get(".rpa", 0)

    report["unsupported"] = [n for n in names if os.path.splitext(n)[1].lower() in UNSUPPORTED_IMAGES]

    biggest = 0
    total = 0
    for n in names:
        s = os.path.getsize(os.path.join(out_game, *n.split("/")))
        total += s
        biggest = max(biggest, s)
    report["total"] = total
    report["biggest"] = biggest

    return report


def print_report(r):
    say("")
    say("=== Game check ===")
    say("Ren'Py version the game was built with: {}".format(r.get("script_version", "unknown")))
    say("Total size: {}   largest file: {}".format(human(r["total"]), human(r["biggest"])))
    say("Scripts: {} .rpy, {} .rpyc, {} .rpa archives".format(r["rpy"], r["rpyc"], r["rpa"]))

    def group(exts):
        return ", ".join("{} {}".format(r["exts"][e], e) for e in sorted(exts) if r["exts"].get(e))

    say("Images: " + (group(IMAGE_EXTS) or "-"))
    say("Audio:  " + (group(AUDIO_EXTS) or "-"))
    say("Video:  " + (group(VIDEO_EXTS) or "-"))

    if r["unsupported"]:
        kinds = Counter(UNSUPPORTED_IMAGES[os.path.splitext(n)[1].lower()] for n in r["unsupported"])
        say("")
        say("WARNING: {} image(s) in formats the Switch build cannot decode: {}".format(
            len(r["unsupported"]), ", ".join("{} {}".format(v, k) for k, v in kinds.items())))
        say("         Run again with --convert-avif to convert AVIF images (needs Pillow >= 11.2).")


def convert_avif(out_game, names):
    try:
        from PIL import Image, features
    except ImportError:
        say("Pillow is not installed: pip install --upgrade pillow")
        return

    if not features.check("avif"):
        say("This Pillow has no AVIF support. Update it: pip install --upgrade pillow")
        return

    todo = [n for n in names if n.lower().endswith(".avif")]
    say("Converting {} AVIF images to WebP (file names stay the same) ...".format(len(todo)))

    for i, n in enumerate(todo, 1):
        path = os.path.join(out_game, *n.split("/"))
        with Image.open(path) as im:
            im.load()
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=90, method=4)
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        if i % 200 == 0:
            say("  {}/{}".format(i, len(todo)))


# ---------------------------------------------------------------------------
# Packing into Ren'Py archives (.rpa)

# Kept as loose files: scripts (so Ren'Py can write .rpyc next to .rpy),
# Python code, existing archives, and files Ren'Py opens by path.
LOOSE_EXTS = {".rpy", ".rpyc", ".rpym", ".rpymc", ".py", ".pyc", ".pyo", ".rpa", ".rpi", ".rpyb"}
LOOSE_TOP_DIRS = {"cache", "saves", "python-packages"}
LOOSE_TOP_PREFIXES = ("presplash", "android-presplash", "icon", "script_version")

RPA_KEY = 0x42424242
RPA_PAD = b"Made with Ren'Py."


def keep_loose(rel):
    parts = rel.split("/")
    if parts[0] in LOOSE_TOP_DIRS:
        return True
    if len(parts) == 1 and parts[0].lower().startswith(LOOSE_TOP_PREFIXES):
        return True
    return os.path.splitext(rel)[1].lower() in LOOSE_EXTS


def write_rpa(path, files, game_dir):
    """
    Writes an RPA-3.0 archive (the format of Ren'Py's own archiver),
    streaming the data so large videos are never loaded into memory.
    `files` are paths relative to game_dir, with forward slashes.
    """
    index = {}
    with open(path, "wb") as f:
        f.write(b"RPA-3.0 XXXXXXXXXXXXXXXX XXXXXXXX\n")
        for rel in files:
            f.write(RPA_PAD)
            offset = f.tell()
            with open(os.path.join(game_dir, *rel.split("/")), "rb") as src:
                shutil.copyfileobj(src, f, 4 * 1024 * 1024)
            dlen = f.tell() - offset
            index[rel] = [(offset ^ RPA_KEY, dlen ^ RPA_KEY, b"")]
        index_offset = f.tell()
        f.write(zlib.compress(pickle.dumps(index, 2)))
        f.seek(0)
        f.write(b"RPA-3.0 %016x %08x\n" % (index_offset, RPA_KEY))


def read_rpa_index(path):
    with open(path, "rb") as f:
        head = f.read(40)
        offset = int(head[8:24], 16)
        key = int(head[25:33], 16)
        f.seek(offset)
        index = pickle.loads(zlib.decompress(f.read()))
    return {k: [(o ^ key, l ^ key) for (o, l, *_rest) in v] for k, v in index.items()}


def pack_game(game_dir, max_bytes, prefix="switch"):
    """Packs everything except LOOSE files into prefix00.rpa, prefix01.rpa..."""

    todo = []
    for root, dirs, files in os.walk(game_dir):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, game_dir).replace(os.sep, "/")
            if not keep_loose(rel):
                todo.append((rel, os.path.getsize(full)))

    if not todo:
        say("Nothing to pack.")
        return

    todo.sort()

    # Split into archives of at most max_bytes (a single bigger file gets an
    # archive of its own; FAT32 allows up to 4 GB per file).
    groups, cur, cur_size = [], [], 0
    for rel, size in todo:
        if cur and cur_size + size > max_bytes:
            groups.append(cur)
            cur, cur_size = [], 0
        cur.append(rel)
        cur_size += size + len(RPA_PAD)
    if cur:
        groups.append(cur)

    existing = [f for f in os.listdir(game_dir) if f.startswith(prefix) and f.endswith(".rpa")]
    if existing:
        raise SystemExit("ERROR: {} already contains {}*.rpa; remove them first.".format(game_dir, prefix))

    total = sum(size for _, size in todo)
    say("")
    say("Packing {} files ({}) into {} archive(s) ...".format(len(todo), human(total), len(groups)))

    for i, group in enumerate(groups):
        name = "{}{:02d}.rpa".format(prefix, i)
        path = os.path.join(game_dir, name)
        write_rpa(path, group, game_dir)

        # Verify the archive before deleting the loose files.
        index = read_rpa_index(path)
        if sorted(index) != sorted(group):
            raise SystemExit("ERROR: {} index does not match its contents.".format(name))
        with open(path, "rb") as f:
            for rel in group:
                off, ln = index[rel][0]
                if ln != os.path.getsize(os.path.join(game_dir, *rel.split("/"))):
                    raise SystemExit("ERROR: size mismatch for {} in {}.".format(rel, name))
            f.seek(0, 2)

        for rel in group:
            os.remove(os.path.join(game_dir, *rel.split("/")))
        say("  {}  {} files, {}".format(name, len(group), human(os.path.getsize(path))))

    # Remove directories that are now empty.
    for root, dirs, files in os.walk(game_dir, topdown=False):
        if root != game_dir and not os.listdir(root):
            os.rmdir(root)

    loose = sum(len(files) for _, _, files in os.walk(game_dir))
    say("Game folder now has {} files (archives + scripts).".format(loose))


# ---------------------------------------------------------------------------
# NRO metadata (title / author / icon)

NACP_SIZE = 0x4000


def find_apk_icon(apk):
    """Returns the bytes of the largest launcher-like PNG in res/, or None."""
    best = None
    for info in apk.infolist():
        n = info.filename.lower()
        if not n.startswith("res/") or not n.endswith(".png"):
            continue
        base = os.path.basename(n)
        if not any(k in base for k in ("icon", "ic_launcher", "app_icon")):
            continue
        if "foreground" in base or "background" in base or "round" in base:
            continue
        if best is None or info.file_size > best.file_size:
            best = info
    return apk.read(best) if best else None


def make_icon_jpeg(png_bytes):
    try:
        from PIL import Image
    except ImportError:
        return None
    with Image.open(io.BytesIO(png_bytes)) as im:
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[3])
        bg = bg.resize((256, 256), Image.LANCZOS)
        out = io.BytesIO()
        bg.save(out, "JPEG", quality=92)
        return out.getvalue()


def rewrite_nro(src, dst, title, author, version, icon_jpeg):
    """
    Copies an .nro, replacing the title/author/version in its NACP and,
    optionally, its icon. The RomFS (Ren'Py runtime) is copied unchanged.
    """
    with open(src, "rb") as f:
        head = f.read(0x20)
        if head[0x10:0x14] != b"NRO0":
            raise SystemExit("ERROR: {} is not an .nro file.".format(src))
        nro_size = struct.unpack_from("<I", head, 0x18)[0]

        f.seek(nro_size)
        aset = f.read(0x38)
        if aset[0:4] != b"ASET":
            raise SystemExit("ERROR: {} has no asset section.".format(src))

        (_, _, icon_off, icon_size, nacp_off, nacp_size, romfs_off, romfs_size) = struct.unpack("<4sIQQQQQQ", aset)

        f.seek(nro_size + icon_off)
        icon = f.read(icon_size)
        f.seek(nro_size + nacp_off)
        nacp = bytearray(f.read(nacp_size))

        if len(nacp) != NACP_SIZE:
            raise SystemExit("ERROR: unexpected NACP size in {}.".format(src))

        def put(offset, size, text):
            data = text.encode("utf-8")[: size - 1]
            nacp[offset:offset + size] = data + b"\0" * (size - len(data))

        # 16 language entries of 0x300 bytes: name (0x200) + author (0x100).
        for lang in range(16):
            put(lang * 0x300, 0x200, title)
            put(lang * 0x300 + 0x200, 0x100, author)

        if version:
            put(0x3060, 0x10, version)

        if icon_jpeg:
            icon = icon_jpeg

        f.seek(0)
        with open(dst, "wb") as out:
            # NRO body.
            remaining = nro_size
            while remaining:
                chunk = f.read(min(remaining, 1 << 20))
                out.write(chunk)
                remaining -= len(chunk)

            def align(x):
                return (x + 0xF) & ~0xF

            new_icon_off = align(0x38)
            new_nacp_off = align(new_icon_off + len(icon))
            new_romfs_off = align(new_nacp_off + len(nacp))

            out.write(struct.pack("<4sIQQQQQQ", b"ASET", 0,
                                  new_icon_off, len(icon),
                                  new_nacp_off, len(nacp),
                                  new_romfs_off if romfs_size else 0, romfs_size))

            def pad_to(rel):
                cur = out.tell() - nro_size
                out.write(b"\0" * (rel - cur))

            pad_to(new_icon_off)
            out.write(icon)
            pad_to(new_nacp_off)
            out.write(bytes(nacp))

            if romfs_size:
                pad_to(new_romfs_off)
                f.seek(nro_size + romfs_off)
                remaining = romfs_size
                while remaining:
                    chunk = f.read(min(remaining, 1 << 20))
                    out.write(chunk)
                    remaining -= len(chunk)


# ---------------------------------------------------------------------------


def make_nro(args, apk_icon, sd_dir, name):
    icon_jpeg = None
    if args.icon:
        with open(args.icon, "rb") as f:
            icon_jpeg = f.read()
    elif apk_icon:
        icon_jpeg = make_icon_jpeg(apk_icon)
        if icon_jpeg is None:
            say("(Pillow not installed: keeping the default icon. pip install pillow)")

    os.makedirs(sd_dir, exist_ok=True)
    dst = os.path.join(sd_dir, name + ".nro")
    rewrite_nro(args.nro, dst, args.title or name, args.author, args.version, icon_jpeg)
    say("Created {}".format(dst))


def main():
    ap = argparse.ArgumentParser(description="Convert a Ren'Py Android APK for the Switch homebrew build.")
    ap.add_argument("apk", help="the Ren'Py .apk, or an already converted \"game\" folder (with --pack)")
    ap.add_argument("-o", "--out", default=None, help="output folder (default: <apk name>-switch)")
    ap.add_argument("--name", default=None, help="folder name on the SD card, e.g. subahibi")
    ap.add_argument("--nro", default=None, help="renpy-switch.nro from the CI build, to copy and rename")
    ap.add_argument("--title", default=None, help="title in the Homebrew Menu (with --nro)")
    ap.add_argument("--author", default="Ren'Py", help="author in the Homebrew Menu (with --nro)")
    ap.add_argument("--version", default=None, help="version in the Homebrew Menu (with --nro)")
    ap.add_argument("--icon", default=None, help="256x256 JPEG icon (with --nro); default: from the APK")
    ap.add_argument("--no-controls", action="store_true", help="do not add zz_switch_controls.rpy")
    ap.add_argument("--convert-avif", action="store_true", help="convert AVIF images (needs Pillow >= 11.2)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing game folder")
    ap.add_argument("--pack", action="store_true",
                    help="pack images/audio/video into a few .rpa archives (fast to copy)")
    ap.add_argument("--pack-size", type=int, default=1900,
                    help="maximum size of one archive in MB (default 1900; FAT32 limit is 4096)")
    args = ap.parse_args()

    # Pack an existing game folder in place.
    if os.path.isdir(args.apk):
        game_dir = os.path.abspath(args.apk)
        if os.path.isdir(os.path.join(game_dir, "game")):
            game_dir = os.path.join(game_dir, "game")
        if not args.pack:
            raise SystemExit("ERROR: {} is a folder; add --pack to pack it.".format(game_dir))
        pack_game(game_dir, args.pack_size * 1024 * 1024)
        say("")
        say("Done. Copy {} to the SD card (replace the old game folder).".format(game_dir))
        return

    apk_path = os.path.abspath(args.apk)
    base = os.path.splitext(os.path.basename(apk_path))[0]
    name = args.name or base.split("-")[0].lower() or "renpygame"
    out = os.path.abspath(args.out or (os.path.splitext(apk_path)[0] + "-switch"))

    sd_dir = os.path.join(out, "switch", name)
    out_game = os.path.join(sd_dir, "game")

    if os.path.exists(out_game) and not args.force:
        # The game was converted before: only (re)make the .nro.
        if not args.nro:
            raise SystemExit("ERROR: {} already exists (use --force to replace it).".format(out_game))
        say("The game folder already exists, keeping it as is:")
        say("  {}".format(out_game))
        say("Only updating the .nro (use --force to convert the game again).")
        with zipfile.ZipFile(apk_path) as apk:
            apk_icon = find_apk_icon(apk)
        make_nro(args, apk_icon, sd_dir, name)
        say("Copy {} to sdmc:/switch/{}/ (replace the old one).".format(name + ".nro", name))
        return

    if os.path.exists(out_game):
        shutil.rmtree(out_game)

    os.makedirs(out_game, exist_ok=True)

    with zipfile.ZipFile(apk_path) as apk:
        names = extract_game(apk, out_game)

        # Presplash.
        if not any(os.path.exists(os.path.join(out_game, "presplash" + e)) for e in (".png", ".jpg")):
            data = read_member(apk, "assets/android-presplash.jpg") or read_member(apk, "assets/android-presplash.png")
            if data:
                ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
                with open(os.path.join(out_game, "presplash" + ext), "wb") as f:
                    f.write(data)
                say("Added presplash{} from the APK.".format(ext))

        apk_icon = find_apk_icon(apk)

    if not args.no_controls:
        controls = CONTROLS_RPY
        here = os.path.dirname(os.path.abspath(__file__))
        for candidate in (os.path.join(here, "zz_switch_controls.rpy"),
                          os.path.join(here, "..", "switch", "game_addons", "zz_switch_controls.rpy")):
            if os.path.exists(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    controls = f.read()
                break
        with open(os.path.join(out_game, "zz_switch_controls.rpy"), "w", encoding="utf-8", newline="\n") as f:
            f.write(controls)
        say("Added zz_switch_controls.rpy.")

    report = check_game(out_game, names)
    print_report(report)

    if args.convert_avif and any(n.lower().endswith(".avif") for n in names):
        convert_avif(out_game, names)

    if args.pack:
        pack_game(out_game, args.pack_size * 1024 * 1024)

    if args.nro:
        make_nro(args, apk_icon, sd_dir, name)

    say("")
    say("Done. Copy the folder")
    say("  {}".format(os.path.join(out, "switch")))
    say("to the root of the SD card, so that you get")
    say("  sdmc:/switch/{0}/{0}.nro".format(name))
    say("  sdmc:/switch/{}/game/".format(name))
    if not args.nro:
        say("and put renpy-switch.nro into sdmc:/switch/{0}/ (any name works).".format(name))
    say("Start it from the Homebrew Menu opened by holding R while launching a game")
    say("(full memory mode). Saves go to sdmc:/switch/{}/save/.".format(name))


if __name__ == "__main__":
    main()
