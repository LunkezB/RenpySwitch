#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apk2switch: turn a Ren'Py Android APK into a game for the Nintendo Switch
build of Ren'Py 8.5 (RenpySwitch).

What it does
  1. Extracts the game from the APK (assets/x-game/...) and removes the "x-"
     prefixes Ren'Py's Android packager (RAPT) adds to every file and folder.
  2. Uses the APK's presplash as presplash.jpg (shown while loading).
  3. Adds zz_switch_controls.rpy (Joy-Con + touch controls).
  4. Checks the files: Ren'Py version, image/video formats the Switch build
     can't decode (AVIF/JXL/SVG), size. --convert-avif converts AVIF images
     in place (needs Pillow >= 11.2).
  5. With --nro: copies the engine (renpy-switch.nro from the CI build) as
     <name>.nro with the game's title, version and icon (taken from the APK).
  6. With --nsp: also makes <name>.nsp, an installable title for the HOME
     menu with the engine and the game inside. Needs hacbrewpack (from the
     CI build) and prod.keys dumped from your own console.

Result (copy the "switch" folder to the root of the SD card):
  <out>/switch/<name>/<name>.nro
  <out>/switch/<name>/game/...
  <out>/<name>.nsp                     (with --nsp)

Usage
  python apk2switch.py game.apk --nro renpy-switch.nro --name mygame --pack
  python apk2switch.py game.apk --nro renpy-switch.nro --name mygame --nsp
  python apk2switch.py path/to/switch/mygame/game --pack      (pack an already converted folder)

--pack puts images, audio and video into a few .rpa archives (Ren'Py's own
archive format), so copying to the SD card is fast; scripts stay loose.

Running it again when the game folder exists keeps the folder and only
makes the .nro/.nsp (--force converts the game again).

Only plain Python 3.8+ is required; Pillow is needed for icons
(pip install pillow) and AVIF conversion.
"""

import argparse
import hashlib
import io
import os
import pickle
import shutil
import struct
import subprocess
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


class _IndexUnpickler(pickle.Unpickler):
    """Archive indexes are plain data; refuse anything that would run code."""

    def find_class(self, module, name):
        if module in ("_codecs", "codecs") and name == "encode":
            import codecs
            return codecs.encode
        raise pickle.UnpicklingError("unexpected object in an archive index: {}.{}".format(module, name))


def read_rpa_index(path):
    """{name: [(offset, length, prefix)]} of an RPA-3.0 or RPA-2.0 archive."""
    with open(path, "rb") as f:
        parts = f.readline(200).split()
        if parts[:1] == [b"RPA-3.0"] and len(parts) >= 3:
            offset, key = int(parts[1], 16), int(parts[2], 16)
        elif parts[:1] == [b"RPA-2.0"] and len(parts) >= 2:
            offset, key = int(parts[1], 16), 0
        else:
            raise ValueError("{} is not an RPA-2.0/3.0 archive".format(path))
        f.seek(offset)
        index = _IndexUnpickler(io.BytesIO(zlib.decompress(f.read())), encoding="bytes").load()
    out = {}
    for k, v in index.items():
        if isinstance(k, bytes):
            k = k.decode("utf-8", "replace")
        entries = []
        for t in v:
            prefix = t[2] if len(t) > 2 else b""
            if isinstance(prefix, str):
                prefix = prefix.encode("latin-1")
            entries.append((t[0] ^ key, t[1] ^ key, prefix))
        out[k] = entries
    return out


def read_rpa_file(path, entry):
    offset, length, prefix = entry
    with open(path, "rb") as f:
        f.seek(offset)
        return prefix + f.read(length - len(prefix))


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
                off, ln, _prefix = index[rel][0]
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
# Android: icon, name and version, read the way Android itself does it
# (AndroidManifest.xml -> resources.arsc -> file), so this works with any APK,
# including release builds whose resource files were renamed (res/a1.png).

ATTR_LABEL = 0x01010001         # android:label
ATTR_ICON = 0x01010002          # android:icon
ATTR_SRC = 0x01010119           # android:src
ATTR_DRAWABLE = 0x01010199      # android:drawable
ATTR_VERSION_NAME = 0x0101021C  # android:versionName
BITMAP_EXTS = (".png", ".webp", ".jpg", ".jpeg")


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _string_pool(b, off):
    """Strings of the ResStringPool chunk at off."""
    header_size = _u16(b, off + 2)
    count, _styles, flags, strings_start = struct.unpack_from("<IIII", b, off + 8)
    offsets = struct.unpack_from("<%dI" % count, b, off + header_size)
    base = off + strings_start
    out = []
    for so in offsets:
        p = base + so
        try:
            if flags & 0x100:   # UTF-8: char count, byte count, bytes
                p += 2 if b[p] & 0x80 else 1
                n = b[p]
                if n & 0x80:
                    n = ((n & 0x7F) << 8) | b[p + 1]
                    p += 2
                else:
                    p += 1
                out.append(b[p:p + n].decode("utf-8", "replace"))
            else:               # UTF-16: char count, chars
                n = _u16(b, p)
                p += 2
                if n & 0x8000:
                    n = ((n & 0x7FFF) << 16) | _u16(b, p)
                    p += 2
                out.append(b[p:p + 2 * n].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out


def axml_elements(b):
    """
    Start elements of an Android binary XML file, as (tag, attrs) pairs.
    attrs maps each attribute's resource id (or its name, when it has none)
    to (value type, value data, string value or None).
    """
    if len(b) < 8 or _u16(b, 0) != 0x0003:
        return
    strings, resmap = [], ()
    off = _u16(b, 2)
    end = min(len(b), _u32(b, 4))
    while off + 8 <= end:
        ctype, hsize, csize = struct.unpack_from("<HHI", b, off)
        if csize < 8:
            break
        if ctype == 0x0001:
            strings = _string_pool(b, off)
        elif ctype == 0x0180:
            resmap = struct.unpack_from("<%dI" % ((csize - hsize) // 4), b, off + hsize)
        elif ctype == 0x0102:
            ext = off + hsize
            _ns, name, astart, asize, acount = struct.unpack_from("<IIHHH", b, ext)
            attrs = {}
            for i in range(acount):
                a = ext + astart + i * asize
                _ans, aname, raw, _size, _res0, dtype, data = struct.unpack_from("<IIIHBBI", b, a)
                if aname < len(resmap) and resmap[aname]:
                    key = resmap[aname]
                else:
                    key = strings[aname] if aname < len(strings) else aname
                if dtype == 0x03 and data < len(strings):
                    text = strings[data]
                else:
                    text = strings[raw] if raw < len(strings) else None
                attrs[key] = (dtype, data, text)
            yield (strings[name] if name < len(strings) else ""), attrs
        off += csize


class ResourceTable(object):
    """resources.arsc: resource id -> [(density, language, type, data)]."""

    def __init__(self, b):
        self.values = {}
        self.strings = []
        off = _u16(b, 2)
        while off + 8 <= len(b):
            ctype, hsize, csize = struct.unpack_from("<HHI", b, off)
            if csize < 8:
                break
            if ctype == 0x0001:
                self.strings = _string_pool(b, off)
            elif ctype == 0x0200:
                self._package(b, off, hsize, csize)
            off += csize

    def _package(self, b, off, hsize, size):
        pkg_id = _u32(b, off + 8)
        p = off + hsize
        while p + 8 <= off + size:
            ctype, h, csize = struct.unpack_from("<HHI", b, p)
            if csize < 8:
                break
            if ctype == 0x0201:
                self._type(b, p, h, pkg_id)
            p += csize

    def _type(self, b, off, hsize, pkg_id):
        type_id, flags, _res, count, entries_start = struct.unpack_from("<BBHII", b, off + 8)
        cfg = off + 20
        cfg_size = _u32(b, cfg)
        density = _u16(b, cfg + 14) if cfg_size >= 16 else 0
        language = b[cfg + 8:cfg + 10].strip(b"\0").decode("latin-1") if cfg_size >= 12 else ""
        idx = off + hsize
        if flags & 0x01:    # sparse: (entry index, offset / 4) pairs
            pairs = [(_u16(b, idx + 4 * i), _u16(b, idx + 4 * i + 2) * 4) for i in range(count)]
        elif flags & 0x02:  # 16-bit offsets / 4
            pairs = [(i, _u16(b, idx + 2 * i)) for i in range(count)]
            pairs = [(i, o * 4) for i, o in pairs if o != 0xFFFF]
        else:
            pairs = [(i, _u32(b, idx + 4 * i)) for i in range(count)]
            pairs = [(i, o) for i, o in pairs if o != 0xFFFFFFFF]
        for entry, eo in pairs:
            e = off + entries_start + eo
            try:
                esize, eflags = struct.unpack_from("<HH", b, e)
                if eflags & 0x0008:     # compact entry
                    dtype, data = eflags >> 8, _u32(b, e + 4)
                elif eflags & 0x0001:   # map entry (styles etc.)
                    continue
                else:
                    _vsize, _res0, dtype, data = struct.unpack_from("<HBBI", b, e + esize)
            except struct.error:
                continue
            resid = (pkg_id << 24) | (type_id << 16) | entry
            self.values.setdefault(resid, []).append((density, language, dtype, data))

    def resolve(self, resid, depth=0):
        """[(density, language, "string" or "color", value)], references followed."""
        out = []
        for density, language, dtype, data in self.values.get(resid, []):
            if dtype == 0x01 and depth < 8:
                for d, l, kind, value in self.resolve(data, depth + 1):
                    out.append((density or d, language or l, kind, value))
            elif dtype == 0x03 and data < len(self.strings):
                out.append((density, language, "string", self.strings[data]))
            elif 0x1C <= dtype <= 0x1F:
                out.append((density, language, "color", data))
        return out


def open_image(data):
    """A PIL image (RGBA) from file bytes. Needs Pillow."""
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    return im.convert("RGBA")


class ApkInfo(object):
    """Name, version and launcher icon of an APK."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self.names = set(self.zip.namelist())
        self.table = None
        self.manifest = {}
        self.app = {}
        try:
            self.table = ResourceTable(self.zip.read("resources.arsc"))
        except Exception:
            pass
        try:
            for tag, attrs in axml_elements(self.zip.read("AndroidManifest.xml")):
                if tag == "manifest" and not self.manifest:
                    self.manifest = attrs
                elif tag == "application" and not self.app:
                    self.app = attrs
        except Exception:
            pass

    def read(self, path):
        return self.zip.read(path) if path in self.names else None

    def _text(self, attr):
        """A text attribute: a literal, or a string resource (default language first)."""
        if not attr:
            return None
        dtype, data, text = attr
        if dtype == 0x03:
            return text or None
        if dtype == 0x01 and self.table is not None:
            values = sorted(self.table.resolve(data), key=lambda v: v[1] != "")
            for _density, _language, kind, value in values:
                if kind == "string" and value.strip():
                    return value.strip()
        return None

    def label(self):
        return self._text(self.app.get(ATTR_LABEL) or self.app.get("label"))

    def version_name(self):
        return self._text(self.manifest.get(ATTR_VERSION_NAME) or self.manifest.get("versionName"))

    def _bitmap(self, path):
        try:
            return open_image(self.read(path))
        except Exception:
            return None

    def _largest(self, paths):
        best = None
        for path in paths:
            if path.lower().endswith(BITMAP_EXTS):
                im = self._bitmap(path)
                if im is not None and (best is None or im.size[0] > best.size[0]):
                    best = im
        return best

    def _layer(self, dtype, data, depth=0):
        """An adaptive icon layer: a PIL image, an RGB color (int), or None."""
        if 0x1C <= dtype <= 0x1F:
            return data
        if dtype != 0x01 or self.table is None or depth > 4:
            return None
        values = self.table.resolve(data)
        for _density, _language, kind, value in values:
            if kind == "color":
                return value
        paths = [v for _d, _l, kind, v in values if kind == "string"]
        best = self._largest(paths)
        if best is not None:
            return best
        # <bitmap android:src=...> or <inset android:drawable=...> wrappers.
        # (Vector drawables can't be drawn here.)
        for path in paths:
            if path.lower().endswith(".xml"):
                for _tag, attrs in axml_elements(self.read(path) or b""):
                    ref = attrs.get(ATTR_SRC) or attrs.get(ATTR_DRAWABLE)
                    if ref:
                        return self._layer(ref[0], ref[1], depth + 1)
        return None

    def _adaptive(self, path, need_background):
        from PIL import Image
        layers = {}
        current = None
        for tag, attrs in axml_elements(self.read(path) or b""):
            if tag in ("background", "foreground", "monochrome"):
                current = tag
            if current in ("background", "foreground") and current not in layers:
                ref = attrs.get(ATTR_DRAWABLE)
                if ref:
                    layers[current] = self._layer(ref[0], ref[1])
        fg = layers.get("foreground")
        bg = layers.get("background")
        if fg is None or isinstance(fg, int) or (bg is None and need_background):
            return None
        size = max(432, fg.size[0], bg.size[0] if (bg is not None and not isinstance(bg, int)) else 0)
        if isinstance(bg, int):
            canvas = Image.new("RGBA", (size, size), ((bg >> 16) & 255, (bg >> 8) & 255, bg & 255, 255))
        elif bg is not None:
            canvas = bg.resize((size, size), Image.LANCZOS)
        else:
            canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        canvas.alpha_composite(fg.resize((size, size), Image.LANCZOS))
        # Launchers show the middle 72dp of the 108dp layers.
        m = size // 6
        return canvas.crop((m, m, size - m, size - m))

    def _guess(self):
        """Old or unusual APKs: the largest launcher-like picture in res/."""
        paths = []
        for n in self.names:
            base = os.path.basename(n.lower())
            if not n.startswith("res/") or not base.endswith(BITMAP_EXTS):
                continue
            if not any(k in base for k in ("icon", "ic_launcher", "app_icon")):
                continue
            if any(k in base for k in ("foreground", "background", "round", "monochrome")):
                continue
            paths.append(n)
        return self._largest(paths)

    def icon(self):
        """The launcher icon as a PIL image, or None. Needs Pillow."""
        ref = self.app.get(ATTR_ICON) or self.app.get("icon")
        if ref and ref[0] == 0x01 and self.table is not None:
            paths = [v for _d, _l, kind, v in self.table.resolve(ref[1]) if kind == "string"]
            legacy = self._largest(paths)
            for path in paths:
                if path.lower().endswith(".xml"):
                    try:
                        im = self._adaptive(path, need_background=legacy is not None)
                    except Exception:
                        im = None
                    if im is not None:
                        return im
            if legacy is not None:
                return legacy
        return self._guess()


# ---------------------------------------------------------------------------
# Icon for the Homebrew Menu and the HOME menu: a 256x256 JPEG.

def find_game_icon(game_dir):
    """The game's window icon (gui/window_icon.png in Ren'Py's template), or None."""
    names = ("gui/window_icon.png", "window_icon.png")
    for n in names:
        p = os.path.join(game_dir, *n.split("/"))
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
    for fn in sorted(os.listdir(game_dir)):
        if not fn.lower().endswith(".rpa"):
            continue
        path = os.path.join(game_dir, fn)
        try:
            index = read_rpa_index(path)
            for n in names:
                if n in index:
                    return read_rpa_file(path, index[n][0])
        except Exception:
            continue
    return None


def jpeg_size(data):
    """(width, height) of a JPEG, or None."""
    p = 2
    while p + 9 < len(data) and data[p] == 0xFF:
        marker = data[p + 1]
        length = struct.unpack_from(">H", data, p + 2)[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack_from(">HH", data, p + 5)
            return w, h
        p += 2 + length
    return None


def icon_jpeg(im):
    """256x256 baseline JPEG without metadata, as the Switch wants it."""
    from PIL import Image
    im = im.convert("RGBA")
    bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
    bg.alpha_composite(im)
    w, h = bg.size
    side = min(w, h)
    bg = bg.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))
    bg = bg.convert("RGB").resize((256, 256), Image.LANCZOS)
    for quality in (92, 85, 75, 60):
        out = io.BytesIO()
        bg.save(out, "JPEG", quality=quality)
        if out.tell() < 0x20000:
            break
    return out.getvalue()


def choose_icon(args, apk, game_dir):
    """(256x256 JPEG bytes, where it came from) or (None, why not)."""
    try:
        import PIL  # noqa: F401
        have_pil = True
    except ImportError:
        have_pil = False

    if args.icon:
        with open(args.icon, "rb") as f:
            data = f.read()
        if not have_pil:
            if data[:2] == b"\xff\xd8" and jpeg_size(data) == (256, 256):
                return data, args.icon
            return None, "Pillow is needed to convert {} (pip install pillow)".format(args.icon)
        return icon_jpeg(open_image(data)), args.icon

    if not have_pil:
        return None, "Pillow is not installed (pip install pillow)"

    if apk is not None:
        try:
            im = apk.icon()
        except Exception:
            im = None
        if im is not None:
            return icon_jpeg(im), "the APK"

    if game_dir and os.path.isdir(game_dir):
        data = find_game_icon(game_dir)
        if data:
            try:
                return icon_jpeg(open_image(data)), "the game's window icon"
            except Exception:
                pass

    return None, "no icon found in the APK or the game; use --icon picture.png"


# ---------------------------------------------------------------------------
# The launcher .nro: metadata, RomFS and code.

NACP_SIZE = 0x4000
NACP_LANGUAGES = (
    "AmericanEnglish", "BritishEnglish", "Japanese", "French", "German",
    "LatinAmericanSpanish", "Spanish", "Italian", "Dutch", "CanadianFrench",
    "Portuguese", "Russian", "Korean", "TraditionalChinese", "SimplifiedChinese",
    "BrazilianPortuguese")


def nacp_put(nacp, offset, size, text):
    data = text.encode("utf-8")
    if len(data) > size - 1:
        data = data[:size - 1].decode("utf-8", "ignore").encode("utf-8")
    nacp[offset:offset + size] = data + b"\0" * (size - len(data))


def make_nacp(base, title, author, version, installable=False):
    nacp = bytearray(base)
    for lang in range(16):
        nacp_put(nacp, lang * 0x300, 0x200, title)
        nacp_put(nacp, lang * 0x300 + 0x200, 0x100, author)
    if version:
        nacp_put(nacp, 0x3060, 0x10, version)
    if installable:
        nacp[0x3025] = 0                                # no user selection at start
        struct.pack_into("<I", nacp, 0x302C, 0xFFFF)    # all languages (an icon each)
        nacp[0x3036] = 0                                # no data loss warning
        struct.pack_into("<QQ", nacp, 0x3080, 0, 0)     # no system save data:
        #                                                 saves are on the SD card
    return bytes(nacp)


def romfs_list(f, base):
    """[(path, offset in f, size)] of the RomFS image at `base` in f."""
    f.seek(base)
    (_hsize, _dh_off, _dh_size, dm_off, dm_size, _fh_off, _fh_size,
     fm_off, fm_size, data_off) = struct.unpack("<10Q", f.read(0x50))
    f.seek(base + dm_off)
    dirs = f.read(dm_size)
    f.seek(base + fm_off)
    files = f.read(fm_size)
    out = []
    todo = [(0, "")]
    while todo:
        doff, prefix = todo.pop()
        _parent, _sibling, child_dir, child_file, _hash, _nlen = struct.unpack_from("<6I", dirs, doff)
        fo = child_file
        while fo != 0xFFFFFFFF:
            _p, sibling, foff, fsize, _h, nlen = struct.unpack_from("<IIQQII", files, fo)
            name = files[fo + 0x20:fo + 0x20 + nlen].decode("utf-8")
            out.append((prefix + name, base + data_off + foff, fsize))
            fo = sibling
        co = child_dir
        while co != 0xFFFFFFFF:
            _p, sibling, _cd, _cf, _h, nlen = struct.unpack_from("<6I", dirs, co)
            name = dirs[co + 0x18:co + 0x18 + nlen].decode("utf-8")
            todo.append((co, prefix + name + "/"))
            co = sibling
    return out


def nro_to_nso(code):
    """
    An (uncompressed) NSO with the code of an .nro: the same program elf2nso
    makes from the ELF, so the engine can run as an installed title.
    """
    if code[0x10:0x14] != b"NRO0":
        raise ValueError("not an .nro")
    segments = [struct.unpack_from("<II", code, 0x20 + 8 * i) for i in range(3)]
    bss = _u32(code, 0x38)
    build_id = code[0x40:0x60]

    datas = []
    for i, (mem, size) in enumerate(segments):
        data = bytearray(code[mem:mem + size])
        if len(data) != size:
            raise ValueError("truncated .nro")
        if i == 0:
            data[0x10:0x80] = bytes(0x70)   # the NRO header (zeros in the ELF)
        datas.append(bytes(data))

    hdr = bytearray(0x100)
    hdr[0:4] = b"NSO0"
    struct.pack_into("<I", hdr, 0x0C, 0x38)     # check segment hashes, no compression
    file_off = 0x100
    for i, (mem, size) in enumerate(segments):
        struct.pack_into("<IIII", hdr, 0x10 + 0x10 * i, file_off, mem, size, bss if i == 2 else 1)
        struct.pack_into("<I", hdr, 0x60 + 4 * i, size)
        hdr[0xA0 + 0x20 * i:0xC0 + 0x20 * i] = hashlib.sha256(datas[i]).digest()
        file_off += size
    hdr[0x40:0x60] = build_id
    return bytes(hdr) + b"".join(datas)


class Nro(object):
    """The renpy-switch.nro from the CI build."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            head = f.read(0x80)
            if len(head) < 0x80 or head[0x10:0x14] != b"NRO0":
                raise SystemExit("ERROR: {} is not an .nro file.".format(path))
            self.size = _u32(head, 0x18)
            f.seek(self.size)
            aset = f.read(0x38)
            if aset[0:4] != b"ASET":
                raise SystemExit("ERROR: {} has no asset section.".format(path))
            (_, _, icon_off, icon_size, nacp_off, nacp_size,
             self.romfs_off, self.romfs_size) = struct.unpack("<4sIQQQQQQ", aset)
            f.seek(self.size + icon_off)
            self.icon = f.read(icon_size)
            f.seek(self.size + nacp_off)
            self.nacp = f.read(nacp_size)
        if len(self.nacp) != NACP_SIZE:
            raise SystemExit("ERROR: unexpected NACP size in {}.".format(path))

    def romfs_files(self):
        if not self.romfs_size:
            return []
        with open(self.path, "rb") as f:
            return romfs_list(f, self.size + self.romfs_off)

    def read_romfs_file(self, name):
        for path, off, size in self.romfs_files():
            if path == name:
                with open(self.path, "rb") as f:
                    f.seek(off)
                    return f.read(size)
        return None

    def nso(self):
        with open(self.path, "rb") as f:
            return nro_to_nso(f.read(self.size))

    def write(self, dst, nacp, icon):
        """Copies the .nro with another NACP and icon (the code and RomFS unchanged)."""

        def align(x):
            return (x + 0xF) & ~0xF

        icon = icon or self.icon
        icon_off = align(0x38)
        nacp_off = align(icon_off + len(icon))
        romfs_off = align(nacp_off + len(nacp))

        with open(self.path, "rb") as f, open(dst, "wb") as out:
            remaining = self.size
            while remaining:
                chunk = f.read(min(remaining, 1 << 20))
                out.write(chunk)
                remaining -= len(chunk)

            out.write(struct.pack("<4sIQQQQQQ", b"ASET", 0, icon_off, len(icon), nacp_off, len(nacp),
                                  romfs_off if self.romfs_size else 0, self.romfs_size))

            def pad_to(rel):
                out.write(b"\0" * (rel - (out.tell() - self.size)))

            pad_to(icon_off)
            out.write(icon)
            pad_to(nacp_off)
            out.write(nacp)

            if self.romfs_size:
                pad_to(romfs_off)
                f.seek(self.size + self.romfs_off)
                remaining = self.romfs_size
                while remaining:
                    chunk = f.read(min(remaining, 1 << 20))
                    out.write(chunk)
                    remaining -= len(chunk)


def make_nro(nro, dst, title, author, version, icon_jpeg):
    nro.write(dst, make_nacp(nro.nacp, title, author, version), icon_jpeg)
    say("Created {}".format(dst))


# ---------------------------------------------------------------------------
# Installable NSP: the engine and the game in one title for the HOME menu.
# The NCAs are made by hacbrewpack with the user's own keys.

HERE = os.path.dirname(os.path.abspath(__file__))


def default_title_id(name):
    """A stable title id per game: 01f + 40 bits of a hash + 000."""
    h = hashlib.sha1(("renpy-switch:" + name.lower()).encode("utf-8")).hexdigest()
    return int("01f" + h[:10] + "000", 16)


def find_file(explicit, candidates, what):
    if explicit:
        if not os.path.isfile(explicit):
            raise SystemExit("ERROR: {} not found: {}".format(what, explicit))
        return os.path.abspath(explicit)
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def find_hacbrewpack(explicit):
    exe = "hacbrewpack.exe" if os.name == "nt" else "hacbrewpack"
    found = find_file(explicit, [os.path.join(HERE, exe), os.path.join(os.getcwd(), exe),
                                 shutil.which("hacbrewpack")], "hacbrewpack")
    if not found:
        raise SystemExit(
            "ERROR: {} not found.\n"
            "Download the hacbrewpack artifact from the same CI run as renpy-switch.nro\n"
            "and put {} next to apk2switch.py (or use --hacbrewpack PATH).".format(exe, exe))
    return found


def find_keys(explicit):
    home = os.path.expanduser("~")
    found = find_file(explicit, [os.path.join(HERE, "prod.keys"), os.path.join(HERE, "keys.txt"),
                                 os.path.join(HERE, "keys.dat"), os.path.join(os.getcwd(), "prod.keys"),
                                 os.path.join(home, ".switch", "prod.keys")], "keys file")
    if not found:
        raise SystemExit(
            "ERROR: prod.keys not found.\n"
            "Dump the keys of your own console (Lockpick_RCM) and put prod.keys next to\n"
            "apk2switch.py, into %USERPROFILE%\\.switch\\, or use --keys PATH.")
    return found


def link_or_copy(src, dst, state):
    if state.get("link", True):
        try:
            os.link(src, dst)
            return
        except OSError:
            state["link"] = False
    shutil.copyfile(src, dst)


def make_nsp(args, nro, game_dir, name, title, author, version, icon, icon_why, out_dir):
    hbp = find_hacbrewpack(args.hacbrewpack)
    keys = find_keys(args.keys)

    if icon is None:
        say("WARNING: no icon ({}); the NSP gets the engine's default icon.".format(icon_why))
        icon = nro.icon

    if args.title_id:
        tid = int(args.title_id, 16)
        if not (0x0100000000000000 <= tid <= 0x0FFFFFFFFFFFFFFF) or tid & 0xFFF:
            raise SystemExit("ERROR: the title id must be 0100000000000000..0fffffffffffffff and end in 000.")
    else:
        tid = default_title_id(name)

    npdm = nro.read_romfs_file("nsp/main.npdm")
    if npdm is None:
        raise SystemExit("ERROR: this renpy-switch.nro is too old to make an NSP; download a newer build.")

    game_files = []
    game_size = 0
    for root, _dirs, files in os.walk(game_dir):
        for fn in files:
            full = os.path.join(root, fn)
            game_files.append((full, os.path.relpath(full, game_dir)))
            game_size += os.path.getsize(full)

    tmp_parent = os.path.abspath(args.temp or out_dir)
    os.makedirs(tmp_parent, exist_ok=True)
    need = 2 * game_size + 512 * 1024 * 1024
    free = shutil.disk_usage(tmp_parent).free
    if free < need:
        raise SystemExit("ERROR: not enough free space in {} for the NSP: need about {}, have {}.\n"
                         "Free some space or use --temp D:\\somewhere.".format(tmp_parent, human(need), human(free)))

    work = os.path.join(tmp_parent, "nsp-tmp")
    if os.path.exists(work):
        shutil.rmtree(work)
    for d in ("exefs", "romfs", "control"):
        os.makedirs(os.path.join(work, d))

    say("")
    say("Making the NSP (title id {:016x}) ...".format(tid))

    # ExeFS: the engine's code and permissions.
    with open(os.path.join(work, "exefs", "main"), "wb") as f:
        f.write(nro.nso())
    with open(os.path.join(work, "exefs", "main.npdm"), "wb") as f:
        f.write(npdm)

    # RomFS: the engine's runtime files, then the game.
    romfs = os.path.join(work, "romfs")
    with open(nro.path, "rb") as f:
        for path, off, size in nro.romfs_files():
            if path.startswith(("nsp/", "Contents/game/")):
                continue
            dst = os.path.join(romfs, *path.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            f.seek(off)
            with open(dst, "wb") as out:
                remaining = size
                while remaining:
                    chunk = f.read(min(remaining, 1 << 20))
                    out.write(chunk)
                    remaining -= len(chunk)

    say("Adding the game ({}) ...".format(human(game_size)))
    state = {}
    game_root = os.path.join(romfs, "Contents", "game")
    for full, rel in game_files:
        dst = os.path.join(game_root, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        link_or_copy(full, dst, state)

    # Saves: the same folder as the .nro version of the game.
    with open(os.path.join(romfs, "Contents", "switch.cfg"), "w", encoding="utf-8", newline="\n") as f:
        f.write("save_dir=sdmc:/switch/{}/save/\n".format(name))

    # Control: title, version and an icon for every language.
    with open(os.path.join(work, "control", "control.nacp"), "wb") as f:
        f.write(make_nacp(nro.nacp, title, author, version, installable=True))
    for lang in NACP_LANGUAGES:
        with open(os.path.join(work, "control", "icon_{}.dat".format(lang)), "wb") as f:
            f.write(icon)

    if os.name == "nt":
        longest = max(len(os.path.join(root, fn)) for root, _d, files in os.walk(work) for fn in files)
        if longest > 250:
            raise SystemExit("ERROR: file paths get too long for hacbrewpack ({} characters).\n"
                             "Use a short temporary folder, e.g. --temp C:\\nsp".format(longest))

    # hacbrewpack gets short relative paths only (it can't take every
    # character on a Windows command line); the keys are copied in for the run.
    shutil.copyfile(keys, os.path.join(work, "keys.dat"))
    cmd = [hbp, "-k", "keys.dat", "--exefsdir", "exefs", "--romfsdir", "romfs",
           "--controldir", "control", "--nologo", "--titleid", "{:016x}".format(tid),
           "--nspdir", "nsp", "--ncadir", "nca", "--tempdir", "temp", "--backupdir", "backup"]
    say("Running hacbrewpack (this takes a while for a big game) ...")
    try:
        rc = subprocess.call(cmd, cwd=work)
    finally:
        try:
            os.remove(os.path.join(work, "keys.dat"))
        except OSError:
            pass

    built = os.path.join(work, "nsp", "{:016x}.nsp".format(tid))
    if rc != 0 or not os.path.isfile(built):
        raise SystemExit("ERROR: hacbrewpack failed (exit code {}). The files are in {}".format(rc, work))

    dst = os.path.join(out_dir, name + ".nsp")
    if os.path.exists(dst):
        os.remove(dst)
    shutil.move(built, dst)

    if not args.keep_temp:
        shutil.rmtree(work, ignore_errors=True)

    say("Created {} ({})".format(dst, human(os.path.getsize(dst))))
    return dst, tid


# ---------------------------------------------------------------------------


def add_controls(out_game):
    controls = CONTROLS_RPY
    for candidate in (os.path.join(HERE, "zz_switch_controls.rpy"),
                      os.path.join(HERE, "..", "switch", "game_addons", "zz_switch_controls.rpy")):
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as f:
                controls = f.read()
            break
    with open(os.path.join(out_game, "zz_switch_controls.rpy"), "w", encoding="utf-8", newline="\n") as f:
        f.write(controls)
    say("Added zz_switch_controls.rpy.")


def convert_apk(args, apk, apk_path, out_game):
    os.makedirs(out_game, exist_ok=True)

    names = extract_game(apk.zip, out_game)

    # Presplash.
    if not any(os.path.exists(os.path.join(out_game, "presplash" + e)) for e in (".png", ".jpg")):
        data = apk.read("assets/android-presplash.jpg") or apk.read("assets/android-presplash.png")
        if data:
            ext = ".png" if data[:4] == b"\x89PNG" else ".jpg"
            with open(os.path.join(out_game, "presplash" + ext), "wb") as f:
                f.write(data)
            say("Added presplash{} from the APK.".format(ext))

    if not args.no_controls:
        add_controls(out_game)

    report = check_game(out_game, names)
    print_report(report)

    if args.convert_avif and any(n.lower().endswith(".avif") for n in names):
        convert_avif(out_game, names)

    if args.pack:
        pack_game(out_game, args.pack_size * 1024 * 1024)


def main():
    ap = argparse.ArgumentParser(description="Convert a Ren'Py Android APK for the Switch build of Ren'Py.")
    ap.add_argument("apk", help="the Ren'Py .apk, or an already converted \"game\" folder")
    ap.add_argument("-o", "--out", default=None, help="output folder (default: <apk name>-switch)")
    ap.add_argument("--name", default=None, help="folder name on the SD card, e.g. subahibi")
    ap.add_argument("--nro", default=None, help="renpy-switch.nro from the CI build (the engine)")
    ap.add_argument("--nsp", action="store_true",
                    help="also make an installable .nsp with the game inside (needs --nro, hacbrewpack, prod.keys)")
    ap.add_argument("--title", default=None, help="title in the menus (default: the APK's name)")
    ap.add_argument("--author", default="Ren'Py", help="author / publisher in the menus")
    ap.add_argument("--version", default=None, help="version in the menus (default: the APK's version)")
    ap.add_argument("--icon", default=None, help="icon picture (any format, cropped to a square; default: from the APK)")
    ap.add_argument("--keys", default=None, help="prod.keys for --nsp (default: next to this script or ~/.switch/)")
    ap.add_argument("--hacbrewpack", default=None, help="hacbrewpack for --nsp (default: next to this script)")
    ap.add_argument("--title-id", default=None, help="title id for --nsp (default: made from --name)")
    ap.add_argument("--temp", default=None, help="folder for the temporary files of --nsp (default: the output folder)")
    ap.add_argument("--keep-temp", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-controls", action="store_true", help="do not add zz_switch_controls.rpy")
    ap.add_argument("--convert-avif", action="store_true", help="convert AVIF images (needs Pillow >= 11.2)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing game folder")
    ap.add_argument("--pack", action="store_true",
                    help="pack images/audio/video into a few .rpa archives (fast to copy)")
    ap.add_argument("--pack-size", type=int, default=1900,
                    help="maximum size of one archive in MB (default 1900; FAT32 limit is 4096)")
    args = ap.parse_args()

    if args.nsp and not args.nro:
        raise SystemExit("ERROR: --nsp needs --nro renpy-switch.nro (the engine from the CI build).")

    apk = None
    converted = False

    if os.path.isdir(args.apk):
        # An already converted game folder (sdmc layout: .../switch/<name>/game).
        game_dir = os.path.abspath(args.apk)
        if os.path.isdir(os.path.join(game_dir, "game")):
            game_dir = os.path.join(game_dir, "game")
        if not (args.pack or args.nro):
            raise SystemExit("ERROR: {} is a folder; add --pack and/or --nro (--nsp).".format(game_dir))
        sd_dir = os.path.dirname(game_dir)
        name = args.name or os.path.basename(sd_dir) or "renpygame"
        parent = os.path.dirname(sd_dir)
        out = os.path.dirname(parent) if os.path.basename(parent).lower() == "switch" else sd_dir
        if args.pack:
            pack_game(game_dir, args.pack_size * 1024 * 1024)
            if not args.nro:
                say("")
                say("Done. Copy {} to the SD card (replace the old game folder).".format(game_dir))
                return
    else:
        apk_path = os.path.abspath(args.apk)
        base = os.path.splitext(os.path.basename(apk_path))[0]
        name = args.name or base.split("-")[0].lower() or "renpygame"
        out = os.path.abspath(args.out or (os.path.splitext(apk_path)[0] + "-switch"))
        sd_dir = os.path.join(out, "switch", name)
        game_dir = os.path.join(sd_dir, "game")
        apk = ApkInfo(apk_path)

        if os.path.exists(game_dir) and not args.force:
            if not args.nro:
                raise SystemExit("ERROR: {} already exists (use --force to replace it).".format(game_dir))
            say("The game folder already exists, keeping it as is:")
            say("  {}".format(game_dir))
            say("(use --force to convert the game again)")
        else:
            if os.path.exists(game_dir):
                shutil.rmtree(game_dir)
            convert_apk(args, apk, apk_path, game_dir)
            converted = True

    nsp = None
    if args.nro:
        title = args.title or (apk.label() if apk else None) or name
        version = args.version or (apk.version_name() if apk else None)
        icon, why = choose_icon(args, apk, game_dir)
        say("")
        say("Title: {}   version: {}".format(title, version or "-"))
        if icon:
            say("Icon: from {}.".format(why))
        else:
            say("WARNING: no icon: {}.".format(why))

        nro = Nro(args.nro)
        os.makedirs(sd_dir, exist_ok=True)
        make_nro(nro, os.path.join(sd_dir, name + ".nro"), title, args.author, version, icon)
        if args.nsp:
            nsp = make_nsp(args, nro, game_dir, name, title, args.author, version, icon, why, out)

    say("")
    if converted:
        say("Done. Copy the folder")
        say("  {}".format(os.path.join(out, "switch")))
        say("to the root of the SD card, so that you get")
        say("  sdmc:/switch/{0}/{0}.nro".format(name))
        say("  sdmc:/switch/{}/game/".format(name))
        if not args.nro:
            say("and put renpy-switch.nro into sdmc:/switch/{0}/ (any name works).".format(name))
        say("Start it from the Homebrew Menu opened by holding R while launching a game")
        say("(full memory mode). Saves go to sdmc:/switch/{}/save/.".format(name))
    elif args.nro:
        say("Done. Copy {}.nro to sdmc:/switch/{}/ (replace the old one).".format(name, name))

    if nsp:
        say("")
        say("NSP: {}".format(nsp[0]))
        say("  Install it with DBI, Tinfoil, Goldleaf or another installer (your console")
        say("  needs sigpatches) and start it from the HOME menu. The game is inside the")
        say("  NSP, so it needs no game folder on the SD card. Saves go to")
        say("  sdmc:/switch/{}/save/, the same as the .nro version.".format(name))

    if apk is not None:
        apk.zip.close()


if __name__ == "__main__":
    main()
