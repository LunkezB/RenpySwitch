#!/usr/bin/env python3
"""
Runs the patched Ren'Py in "Switch mode" on a desktop Linux machine, to catch
problems before they reach the console:

  * sys.platform is "nx" (as under NXPython), so renpy.switch is True and all
    Switch code paths (paths, presplash, variants, renderer...) are exercised;
  * the CPython modules NXPython does not build are made unimportable
    (_ctypes, _posixsubprocess, _ssl, fcntl, mmap, pwd, ...);
  * switch/site (the _posixsubprocess stand-in) is on sys.path, like
    romfs:/python/site on the console;
  * the game and save folders are passed exactly like main.c does.

Usage:
  python3.14 simulate_switch.py <renpy-source-with-host-modules> <game-dir> <save-dir> [renpy args...]

The Ren'Py tree must have its Cython modules built for this host Python
(python setup.py build_ext --inplace).
"""

import importlib.abc
import os
import sys

if len(sys.argv) < 4:
    print(__doc__)
    sys.exit(2)

renpy_base = os.path.abspath(sys.argv[1])
game_dir = os.path.abspath(sys.argv[2])
save_dir = os.path.abspath(sys.argv[3]) + "/"
extra_args = sys.argv[4:]

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(os.path.dirname(HERE), "switch", "site")

# Modules NXPython marks n/a (configure.ac PY_STDLIB_MOD_SET_NA) or does not
# build at all.
BLOCKED = {
    "_ctypes", "_ctypes_test", "ctypes",
    "_curses", "_curses_panel", "_dbm", "_gdbm",
    "_multiprocessing", "_posixshmem", "_posixsubprocess",
    "_scproxy", "_sqlite3", "_ssl", "_hashlib", "_tkinter",
    "fcntl", "grp", "mmap", "nis", "pwd", "readline", "resource",
    "spwd", "syslog", "termios", "_bz2", "_uuid",
}


class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".")[0]
        if top in BLOCKED:
            # Let our pure-Python stand-ins through.
            if os.path.exists(os.path.join(SITE, top + ".py")):
                return None
            raise ModuleNotFoundError("No module named {!r} (not available on the Switch)".format(fullname), name=fullname)
        return None


for name in list(sys.modules):
    if name.split(".")[0] in BLOCKED:
        del sys.modules[name]

sys.meta_path.insert(0, Blocker())

# Like main.c: Ren'Py base, then stdlib, then romfs:/python/site.
sys.path.insert(0, SITE)
sys.path.insert(0, renpy_base)

os.environ["RENPY_SWITCH_BASE"] = renpy_base
os.environ["RENPY_SWITCH_GAME"] = game_dir
os.environ["RENPY_SWITCH_SAVE"] = save_dir
os.makedirs(save_dir, exist_ok=True)

sys.platform = "nx"

sys.argv = [os.path.join(renpy_base, "renpy.py")] + extra_args

import runpy  # noqa: E402

runpy.run_path(os.path.join(renpy_base, "renpy.py"), run_name="__main__")
