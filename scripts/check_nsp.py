#!/usr/bin/env python3
"""
Checks for the installable (NSP) output of tools/apk2switch.py, run in CI:

  check_nsp.py nso NRO NSO
      The code apk2switch takes from the .nro (nro_to_nso) must be exactly
      what elf2nso made from the ELF (segments compared after decompressing).

  check_nsp.py pack WORKDIR
      apk2switch --pack on a small game: the archives must read back.

  check_nsp.py nsp NRO HACBREWPACK HACTOOL WORKDIR
      Builds an NSP for a tiny game with throwaway keys, unpacks it again
      with hactool and checks what is inside.
"""

import hashlib
import os
import shutil
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))

import apk2switch  # noqa: E402


def fail(msg):
    print("FAIL: " + msg)
    sys.exit(1)


def lz4_block(src, size):
    dst = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    if len(dst) != size:
        fail("LZ4 size {} != {}".format(len(dst), size))
    return bytes(dst)


def nso_segments(nso):
    """[(mem offset, data)] of an NSO (compressed or not), hashes checked."""
    flags = struct.unpack_from("<I", nso, 0x0C)[0]
    out = []
    for i in range(3):
        file_off, mem, size, _extra = struct.unpack_from("<IIII", nso, 0x10 + 0x10 * i)
        comp = struct.unpack_from("<I", nso, 0x60 + 4 * i)[0]
        raw = nso[file_off:file_off + comp]
        data = lz4_block(raw, size) if flags & (1 << i) else raw
        if flags & (8 << i) and hashlib.sha256(data).digest() != nso[0xA0 + 0x20 * i:0xC0 + 0x20 * i]:
            fail("segment {} hash mismatch".format(i))
        out.append((mem, data))
    return out


def check_nso(nro_path, nso_path):
    with open(nso_path, "rb") as f:
        ref = f.read()
    mine = apk2switch.Nro(nro_path).nso()

    a = nso_segments(ref)
    b = nso_segments(mine)
    for i, ((ma, da), (mb, db)) in enumerate(zip(a, b)):
        if ma != mb:
            fail("segment {} address {:#x} != {:#x}".format(i, mb, ma))
        if len(db) < len(da) or db[:len(da)] != da or db[len(da):].strip(b"\0"):
            fail("segment {} differs from elf2nso's".format(i))
    if ref[0x40:0x60] != mine[0x40:0x60]:
        fail("build id differs")
    # Memory size (data end + bss) must cover what elf2nso asks for.
    end_ref = a[2][0] + len(a[2][1]) + struct.unpack_from("<I", ref, 0x3C)[0]
    end_mine = b[2][0] + len(b[2][1]) + struct.unpack_from("<I", mine, 0x3C)[0]
    if end_mine < end_ref or end_mine > ((end_ref + 0xFFF) & ~0xFFF):
        fail("memory size {:#x} vs elf2nso {:#x}".format(end_mine, end_ref))
    print("OK: NSO from the .nro matches elf2nso ({} bytes of code)".format(sum(len(d) for _, d in b)))


def run(cmd, **kw):
    print("+ " + " ".join(cmd))
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, **kw)
    if r.returncode != 0:
        print(r.stdout)
        fail("{} exited with {}".format(cmd[0], r.returncode))
    return r.stdout


def check_nsp(nro_path, hbp, hactool, work):
    if os.path.exists(work):
        shutil.rmtree(work)
    game = os.path.join(work, "out", "switch", "testgame", "game")
    os.makedirs(os.path.join(game, "images"))
    with open(os.path.join(game, "script.rpy"), "w") as f:
        f.write('label start:\n    "Hello from an NSP."\n    return\n')
    with open(os.path.join(game, "images", "bg.bin"), "wb") as f:
        f.write(os.urandom(3 * 1024 * 1024 + 7))

    from PIL import Image
    icon_png = os.path.join(work, "icon.png")
    Image.new("RGB", (300, 200), (200, 40, 90)).save(icon_png)

    # Throwaway keys: enough for hacbrewpack to encrypt and hactool to decrypt.
    keys = os.path.join(work, "test.keys")
    with open(keys, "w") as f:
        f.write("header_key = {}\n".format(hashlib.sha256(b"h").hexdigest()))
        f.write("key_area_key_application_00 = {}\n".format(hashlib.md5(b"k").hexdigest()))

    title = "Тест — テスト"
    run([sys.executable, os.path.join(HERE, "..", "tools", "apk2switch.py"), game,
         "--nro", nro_path, "--nsp", "--title", title, "--version", "1.2",
         "--icon", icon_png, "--keys", keys, "--hacbrewpack", hbp])

    nsp = os.path.join(work, "out", "testgame.nsp")
    nro_out = os.path.join(work, "out", "switch", "testgame", "testgame.nro")
    for p in (nsp, nro_out):
        if not os.path.isfile(p):
            fail("missing " + p)
    if os.path.exists(os.path.join(work, "out", "nsp-tmp")):
        fail("temporary folder left behind")

    # The .nro for the Homebrew Menu: same title and icon, code and RomFS unchanged.
    nro = apk2switch.Nro(nro_out)
    if apk2switch.jpeg_size(nro.icon) != (256, 256):
        fail(".nro icon is not a 256x256 JPEG")
    if nro.nacp[:0x200].split(b"\0")[0].decode("utf-8") != title:
        fail(".nro title")
    if nro.nso() != apk2switch.Nro(nro_path).nso():
        fail(".nro code changed")
    if [(p, s) for p, _o, s in nro.romfs_files()] != [(p, s) for p, _o, s in apk2switch.Nro(nro_path).romfs_files()]:
        fail(".nro RomFS changed")

    tid = apk2switch.default_title_id("testgame")
    # Bit 12 set would make it an add-on id (emulators crash on it).
    for name in ("testgame", "subahibi", "mygame", "a", "b", "c", "d", "e", "f"):
        if apk2switch.default_title_id(name) & 0x1FFF:
            fail("title id for {!r} is not an application id".format(name))

    # Unpack the NSP and its NCAs.
    unpack = os.path.join(work, "unpack")
    run([hactool, "-k", keys, "-t", "pfs0", "--outdir=" + unpack, nsp])
    ncas = sorted(f for f in os.listdir(unpack) if f.endswith(".nca"))
    if len(ncas) != 3:
        fail("expected 3 NCAs (program, control, meta), got {}".format(ncas))

    found = {}
    for nca in ncas:
        d = os.path.join(work, "nca-" + nca[:8])
        os.makedirs(d)      # hactool makes only the last folder of a path
        info = run([hactool, "-k", keys, "--exefsdir=" + d + "/exefs", "--romfsdir=" + d + "/romfs",
                    "--section0dir=" + d + "/s0", os.path.join(unpack, nca)])
        if "{:016x}".format(tid) not in info.lower():
            fail("{} does not have title id {:016x}".format(nca, tid))
        for kind in ("Program", "Control", "Meta"):
            if "Content Type:                       " + kind in info:
                found[kind] = d
    if sorted(found) != ["Control", "Meta", "Program"]:
        fail("content types: {}".format(sorted(found)))

    prog = found["Program"]
    with open(os.path.join(prog, "exefs", "main"), "rb") as f:
        if f.read() != apk2switch.Nro(nro_path).nso():
            fail("ExeFS main differs from the engine code")
    with open(os.path.join(prog, "exefs", "main.npdm"), "rb") as f:
        npdm = f.read()
    aci0 = struct.unpack_from("<I", npdm, 0x70)[0]
    if struct.unpack_from("<Q", npdm, aci0 + 0x10)[0] != tid:
        fail("main.npdm ACI0 title id not patched")

    romfs = os.path.join(prog, "romfs")
    for rel in ("Contents/renpy.py", "Contents/renpy/bootstrap.py", "python/lib/python3.14/os.py",
                "Contents/game/script.rpy", "Contents/game/images/bg.bin", "Contents/switch.cfg"):
        if not os.path.isfile(os.path.join(romfs, *rel.split("/"))):
            fail("RomFS lacks " + rel)
    if os.path.exists(os.path.join(romfs, "nsp")):
        fail("RomFS still has nsp/")
    with open(os.path.join(romfs, "Contents", "game", "images", "bg.bin"), "rb") as a, \
            open(os.path.join(game, "images", "bg.bin"), "rb") as b:
        if a.read() != b.read():
            fail("game file changed in the RomFS")
    with open(os.path.join(romfs, "Contents", "switch.cfg")) as f:
        if f.read().strip() != "save_dir=sdmc:/switch/testgame/save/":
            fail("switch.cfg")

    control = os.path.join(found["Control"], "romfs")
    with open(os.path.join(control, "control.nacp"), "rb") as f:
        nacp = f.read()
    for lang in range(16):
        name = nacp[lang * 0x300:lang * 0x300 + 0x200].split(b"\0")[0].decode("utf-8")
        if name != title:
            fail("NACP title {} = {!r}".format(lang, name))
    if nacp[0x3060:0x3070].split(b"\0")[0] != b"1.2":
        fail("NACP version")
    if nacp[0x3025] != 0 or struct.unpack_from("<Q", nacp, 0x3078)[0] != tid:
        fail("NACP user account / save data owner")
    for lang in apk2switch.NACP_LANGUAGES:
        p = os.path.join(control, "icon_{}.dat".format(lang))
        if not os.path.isfile(p):
            fail("missing icon " + lang)
        with open(p, "rb") as f:
            data = f.read()
        if apk2switch.jpeg_size(data) != (256, 256) or b"Exif" in data[:64]:
            fail("icon {} is not a plain 256x256 JPEG".format(lang))

    print("OK: NSP {:016x} has the engine, the game, the title, the icons ({})".format(
        tid, apk2switch.human(os.path.getsize(nsp))))


def check_pack(work):
    """apk2switch --pack: archives written, verified, readable, loose files kept."""
    if os.path.exists(work):
        shutil.rmtree(work)
    game = os.path.join(work, "game")
    files = {
        "script.rpy": b'label start:\n    "x"\n    return\n',
        "images/bg one.png": os.urandom(5000),
        "images/sub/ü.jpg": os.urandom(70000),
        "audio/music.ogg": b"",
        "gui/window_icon.png": os.urandom(300),
    }
    for rel, data in files.items():
        p = os.path.join(game, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    run([sys.executable, os.path.join(HERE, "..", "tools", "apk2switch.py"), game, "--pack", "--pack-size", "1"])
    rpas = sorted(f for f in os.listdir(game) if f.endswith(".rpa"))
    if not rpas:
        fail("no archives written")
    seen = {}
    for fn in rpas:
        path = os.path.join(game, fn)
        for name, entries in apk2switch.read_rpa_index(path).items():
            seen[name] = apk2switch.read_rpa_file(path, entries[0])
    for rel, data in files.items():
        if rel == "script.rpy":
            if rel in seen or not os.path.isfile(os.path.join(game, rel)):
                fail("script.rpy must stay loose")
        elif seen.get(rel) != data:
            fail("{} differs in the archives".format(rel))
    print("OK: --pack wrote {} archive(s) that read back correctly".format(len(rpas)))


if __name__ == "__main__":
    if sys.argv[1:2] == ["pack"] and len(sys.argv) == 3:
        check_pack(sys.argv[2])
        sys.exit(0)
    if sys.argv[1:2] == ["nso"] and len(sys.argv) == 4:
        check_nso(sys.argv[2], sys.argv[3])
    elif sys.argv[1:2] == ["nsp"] and len(sys.argv) == 6:
        check_nsp(*sys.argv[2:])
    else:
        print(__doc__)
        sys.exit(2)
