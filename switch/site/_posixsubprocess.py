# Nintendo Switch: there are no child processes. CPython's subprocess module
# imports _posixsubprocess at import time on POSIX platforms, so this stand-in
# lets "import subprocess" succeed (Ren'Py imports it at startup); actually
# starting a process raises OSError.

import errno


def fork_exec(*args, **kwargs):
    raise OSError(errno.ENOSYS, "Starting processes is not supported on the Nintendo Switch")
