"""
Nintendo Switch (NXPython): stand-ins for POSIX process functions that do
not exist there, imported by the launcher before Ren'Py starts.

There are no child processes on the Switch, so CPython's os module lacks
waitpid() and friends, and there are no symbolic links, so it lacks
readlink(). Some standard modules reference them unconditionally (subprocess
at import time, os.path.realpath() on every call), so they get harmless
stand-ins: the wait functions report that there is no child process, and
readlink() reports that the path is not a symlink. Feature checks such as hasattr(os, "fork") are left
alone, so code that looks before it leaps still sees the truth.
"""

import errno
import os
import sys


def _no_child(*args, **kwargs):
    raise ChildProcessError(errno.ECHILD, "No child processes on the Nintendo Switch")


def _readlink(path, *args, **kwargs):
    # No symbolic links on the SD card (FAT32/exFAT) or in the RomFS. Raising
    # EINVAL ("not a symlink") is what os.path.realpath() expects.
    raise OSError(errno.EINVAL, "Not a symbolic link", path)


_STUBS = {
    "readlink": _readlink,
    "waitpid": _no_child,
    "wait": _no_child,
    "wait3": _no_child,
    "wait4": _no_child,
    "waitstatus_to_exitcode": lambda status: status,
    "WIFSTOPPED": lambda status: False,
    "WSTOPSIG": lambda status: 0,
    "WIFEXITED": lambda status: True,
    "WEXITSTATUS": lambda status: status,
    "WIFSIGNALED": lambda status: False,
    "WTERMSIG": lambda status: 0,
    "WNOHANG": 1,
}

for _name, _value in _STUBS.items():
    if not hasattr(os, _name):
        setattr(os, _name, _value)

del _name, _value


# ---------------------------------------------------------------------------
# Startup trace: every imported module and every script file opened, written
# straight to <save>/logs/trace.txt (no buffering), so a native crash during
# startup still shows how far loading got. Stops after a few thousand lines.

def _start_trace():
    save = os.environ.get("RENPY_SWITCH_SAVE")
    if not save or os.environ.get("RENPY_SWITCH_TRACE", "1") == "0":
        return

    try:
        os.makedirs(save + "logs", exist_ok=True)
        fd = os.open(save + "logs/trace.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    except OSError:
        return

    state = {"left": 4000}

    def hook(event, args):
        if state["left"] <= 0:
            return
        try:
            if event == "import":
                line = "import " + str(args[0])
            elif event == "open" and isinstance(args[0], str) and args[0].endswith((".rpyc", ".rpy", ".rpa", ".rpyb")):
                line = "open " + args[0]
            else:
                return
            state["left"] -= 1
            os.write(fd, (line + "\n").encode("utf-8", "replace"))
        except Exception:
            pass

    sys.addaudithook(hook)


_start_trace()
