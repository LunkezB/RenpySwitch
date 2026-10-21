/*
 * Ren'Py 8.5 launcher for Nintendo Switch homebrew (NXPython / CPython 3.14).
 *
 * Layout inside the RomFS:
 *   romfs:/Contents/renpy.py          Ren'Py launcher script
 *   romfs:/Contents/renpy/            Ren'Py Python package (+ renpy/common)
 *   romfs:/Contents/game/             the game (optional, see below)
 *   romfs:/python/lib/python3.14/     CPython standard library
 *   romfs:/python/site/               pure-Python deps (six, ecdsa)
 *
 * The game can either be embedded in the RomFS (romfs:/Contents/game) or be a
 * loose "game" folder next to the .nro on the SD card:
 *   sdmc:/switch/<name>/<name>.nro
 *   sdmc:/switch/<name>/game/
 *
 * Saves, logs, persistent data and regenerated caches always go to the SD
 * card (never to another title's save data):
 *   NRO:  <folder of the .nro>/save/
 *   NSP:  sdmc:/switch/renpy-saves/<program id>/
 *
 * The choice is passed to renpy.py in RENPY_SWITCH_GAME / RENPY_SWITCH_SAVE.
 */

#include <switch.h>
#include <Python.h>

#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <pthread.h>
#include <unistd.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include "renpy_inittab.h"

#ifndef RENPY_SWITCH_STACK_SIZE
#define RENPY_SWITCH_STACK_SIZE (32 * 1024 * 1024)
#endif

/*
 * Early boot log, written before anything else can go wrong, so a crash
 * leaves a trace even when the save folder was never created.
 */
#define BOOT_LOG "sdmc:/switch/renpy-switch-boot.txt"

static void boot_log(const char *fmt, ...)
{
    FILE *f = fopen(BOOT_LOG, "a");
    if (!f) {
        return;
    }
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static char g_game_dir[FS_MAX_PATH];
static char g_save_dir[FS_MAX_PATH];
static char g_message[0x800];

/* ------------------------------------------------------------------------ */
/* Error reporting                                                          */

static void show_error(const char *message)
{
    char first_line[0x100];
    const char *end = strchr(message, '\n');
    size_t len = end ? (size_t)(end - message) : strlen(message);

    if (len >= sizeof(first_line)) {
        len = sizeof(first_line) - 1;
    }

    memcpy(first_line, message, len);
    first_line[len] = '\0';

    ErrorSystemConfig c;
    errorSystemCreate(&c, first_line, message);
    errorSystemShow(&c);
}

static void fatal(const char *message)
{
    show_error(message);
    exit(1);
}

/* ------------------------------------------------------------------------ */
/* Paths                                                                    */

static int is_dir(const char *path)
{
    struct stat st;
    return stat(path, &st) == 0 && S_ISDIR(st.st_mode);
}

static void mkdirs(const char *path)
{
    char tmp[FS_MAX_PATH];
    snprintf(tmp, sizeof(tmp), "%s", path);

    /* Skip the device prefix ("sdmc:/"). */
    char *p = strchr(tmp, ':');
    p = p ? p + 2 : tmp + 1;

    for (; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            mkdir(tmp, 0777);
            *p = '/';
        }
    }

    mkdir(tmp, 0777);
}

/*
 * Reads "key=value" from romfs:/Contents/switch.cfg (written by apk2switch
 * into NSP builds) into out. Returns 1 if the key was found.
 */
static int read_cfg(const char *key, char *out, size_t out_size)
{
    FILE *f = fopen("romfs:/Contents/switch.cfg", "r");
    if (!f) {
        return 0;
    }

    char line[FS_MAX_PATH + 64];
    size_t key_len = strlen(key);
    int found = 0;

    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (strncmp(line, key, key_len) == 0 && line[key_len] == '=') {
            snprintf(out, out_size, "%s", line + key_len + 1);
            found = 1;
            break;
        }
    }

    fclose(f);
    return found;
}

static void choose_paths(int argc, char *argv[])
{
    /* Directory of the .nro (NRO launches get argv[0] = sdmc:/switch/x/x.nro). */
    char nro_dir[FS_MAX_PATH] = "";

    if (!envIsNso() && argc > 0 && argv[0] && strncmp(argv[0], "sdmc:/", 6) == 0) {
        snprintf(nro_dir, sizeof(nro_dir), "%s", argv[0]);
        char *slash = strrchr(nro_dir, '/');
        if (slash) {
            *slash = '\0';
        }
    }

    /* Game directory. */
    if (is_dir("romfs:/Contents/game")) {
        snprintf(g_game_dir, sizeof(g_game_dir), "romfs:/Contents/game");
    } else if (nro_dir[0]) {
        snprintf(g_game_dir, sizeof(g_game_dir), "%s/game", nro_dir);
    } else {
        snprintf(g_game_dir, sizeof(g_game_dir), "sdmc:/switch/renpy/game");
    }

    /* Save directory. An NSP build names its own (the same folder as the
     * game's .nro, so both share saves); otherwise one per program id. */
    char cfg_save[FS_MAX_PATH];

    if (nro_dir[0]) {
        snprintf(g_save_dir, sizeof(g_save_dir), "%s/save/", nro_dir);
    } else if (read_cfg("save_dir", cfg_save, sizeof(cfg_save)) &&
               strncmp(cfg_save, "sdmc:/", 6) == 0 && strlen(cfg_save) < FS_MAX_PATH - 2) {
        size_t n = strlen(cfg_save);
        snprintf(g_save_dir, sizeof(g_save_dir), "%s%s", cfg_save, cfg_save[n - 1] == '/' ? "" : "/");
    } else {
        u64 program_id = 0;
        svcGetInfo(&program_id, InfoType_ProgramId, CUR_PROCESS_HANDLE, 0);
        snprintf(g_save_dir, sizeof(g_save_dir), "sdmc:/switch/renpy-saves/%016lX/", program_id);
    }

    mkdirs(g_save_dir);

    setenv("RENPY_SWITCH_GAME", g_game_dir, 1);
    setenv("RENPY_SWITCH_SAVE", g_save_dir, 1);

    if (!is_dir(g_game_dir)) {
        snprintf(g_message, sizeof(g_message),
            "Game folder not found.\n\n"
            "Expected the Ren'Py \"game\" folder at:\n%s\n\n"
            "Copy the game folder next to the .nro, e.g.\n"
            "sdmc:/switch/mygame/mygame.nro\n"
            "sdmc:/switch/mygame/game/",
            g_game_dir);
        fatal(g_message);
    }
}

/* ------------------------------------------------------------------------ */
/* _otrhlibnx: small native helpers used by the Ren'Py patch.               */

static PyObject *commitsave(PyObject *self, PyObject *args)
{
    /* Saves live on the SD card; nothing to commit. Kept for compatibility. */
    Py_RETURN_NONE;
}

static PyObject *startboost(PyObject *self, PyObject *args)
{
    appletSetCpuBoostMode(ApmCpuBoostMode_FastLoad);
    Py_RETURN_NONE;
}

static PyObject *disableboost(PyObject *self, PyObject *args)
{
    appletSetCpuBoostMode(ApmCpuBoostMode_Normal);
    Py_RETURN_NONE;
}

static PyObject *restartprogram(PyObject *self, PyObject *args)
{
    if (envIsNso()) {
        appletRestartProgram(NULL, 0);
    }
    Py_RETURN_NONE;
}

static PyObject *isdocked(PyObject *self, PyObject *args)
{
    return PyBool_FromLong(appletGetOperationMode() == AppletOperationMode_Console);
}

static PyObject *saveroot(PyObject *self, PyObject *args)
{
    return PyUnicode_FromString(g_save_dir);
}

static PyMethodDef otrh_methods[] = {
    { "commitsave", commitsave, METH_NOARGS, "No-op (saves are on the SD card)." },
    { "startboost", startboost, METH_NOARGS, "Boost the CPU while loading." },
    { "disableboost", disableboost, METH_NOARGS, "Return the CPU to normal clocks." },
    { "restartprogram", restartprogram, METH_NOARGS, "Restart the title (NSP only)." },
    { "isdocked", isdocked, METH_NOARGS, "True when the console is docked." },
    { "saveroot", saveroot, METH_NOARGS, "Writable data directory on the SD card." },
    { NULL, NULL, 0, NULL }
};

static struct PyModuleDef otrh_module = {
    PyModuleDef_HEAD_INIT, "_otrhlibnx", NULL, -1, otrh_methods,
};

static PyObject *PyInit__otrhlibnx(void)
{
    return PyModule_Create(&otrh_module);
}

static struct _inittab g_builtins[] = {
    { "_otrhlibnx", PyInit__otrhlibnx },
    RENPY_INITTAB_ENTRIES
    { NULL, NULL }
};

/* ------------------------------------------------------------------------ */
/* libnx hooks                                                              */

/*
 * No __libnx_initheap override: libnx's default uses all available memory in
 * an installed title and the heap prepared by the homebrew loader for an
 * .nro. (Calling svcSetHeapSize under hbloader crashes before main.)
 */

static int g_nxlink = 0;

void userAppInit(void)
{
    /* Emulators start with an empty SD card (no "switch" folder). */
    mkdir("sdmc:/switch", 0777);
    FILE *f = fopen(BOOT_LOG, "w");
    if (f) {
        fputs("renpy-switch boot log\n", f);
        fclose(f);
    }

    Result rc = romfsInit();
    boot_log("romfsInit: 0x%x", rc);

    rc = socketInitializeDefault();
    boot_log("socketInitializeDefault: 0x%x", rc);

    /* Started with nxlink (hbmenu netloader)? Then stdout/stderr go back to
     * the PC: "nxlink -s game.nro" shows Ren'Py's output live. */
    if (R_SUCCEEDED(rc) && __nxlink_host.s_addr != 0 && nxlinkStdio() >= 0) {
        g_nxlink = 1;
        boot_log("nxlink stdio enabled");
    }
}

void userAppExit(void)
{
    socketExit();
    romfsExit();
}

/* ------------------------------------------------------------------------ */
/* Crash reporter                                                           */

/*
 * On a CPU exception (data abort etc.) libnx calls this on a separate stack
 * before the process dies. It appends the registers, a frame-pointer
 * backtrace and the Python stacks of all threads to the boot log. Addresses
 * are also printed relative to main() so they can be symbolized with the
 * symbol table the CI publishes (symbols.txt.gz on the ci-logs branch).
 * Only write()-level calls here: the heap may be corrupted.
 */
alignas(16) u8 __nx_exception_stack[0x40000];
u64 __nx_exception_stack_size = sizeof(__nx_exception_stack);
u32 __nx_exception_ignoredebug = 1;

extern const char *_Py_DumpTracebackThreads(int fd, PyInterpreterState *interp, PyThreadState *current_tstate);
int main(int argc, char *argv[]);

static void crash_write(int fd, const char *fmt, ...)
{
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n > 0) {
        write(fd, buf, n < (int)sizeof(buf) ? n : (int)sizeof(buf) - 1);
    }
}

void __libnx_exception_handler(ThreadExceptionDump *ctx)
{
    int fd = open(BOOT_LOG, O_WRONLY | O_CREAT | O_APPEND, 0666);
    if (fd < 0) {
        return;
    }

    u64 base = (u64)(uintptr_t)&main;

    crash_write(fd, "\n=== CRASH: error_desc 0x%x ===\n", ctx->error_desc);
    crash_write(fd, "main @ 0x%lx (offsets below are relative to main)\n", base);
    crash_write(fd, "pc  0x%lx  main%+ld\n", ctx->pc.x, (long)(ctx->pc.x - base));
    crash_write(fd, "lr  0x%lx  main%+ld\n", ctx->lr.x, (long)(ctx->lr.x - base));
    crash_write(fd, "sp  0x%lx  fp 0x%lx  far 0x%lx  esr 0x%x\n", ctx->sp.x, ctx->fp.x, ctx->far.x, ctx->esr);
    for (int i = 0; i < 29; i++) {
        crash_write(fd, "x%-2d 0x%016lx%s", i, ctx->cpu_gprs[i].x, (i % 3 == 2) ? "\n" : "  ");
    }
    crash_write(fd, "\nbacktrace:\n");

    u64 fp = ctx->fp.x;
    u64 sp = ctx->sp.x;
    for (int i = 0; i < 48 && fp && !(fp & 0xF) && fp >= sp && fp - sp < 0x4000000; i++) {
        u64 next = ((u64 *)fp)[0];
        u64 ret = ((u64 *)fp)[1];
        crash_write(fd, "  #%02d 0x%lx  main%+ld\n", i, ret, (long)(ret - base));
        if (next <= fp) {
            break;
        }
        fp = next;
    }

    crash_write(fd, "\npython:\n");
    _Py_DumpTracebackThreads(fd, NULL, NULL);
    crash_write(fd, "=== end ===\n");
    close(fd);
}

static AppletHookCookie g_applet_hook_cookie;

static void on_applet_hook(AppletHookType hook, void *param)
{
    if (hook == AppletHookType_OnExitRequest) {
        /* Give Ren'Py a moment to finish writing files, then allow exit. */
        svcSleepThread(500000000ULL);
        appletUnlockExit();
    }
}

/* ------------------------------------------------------------------------ */

static void check_status(PyStatus status, const char *what)
{
    if (PyStatus_Exception(status)) {
        snprintf(g_message, sizeof(g_message), "Python initialization failed (%s).\n\n%s",
            what, status.err_msg ? status.err_msg : "unknown error");
        fatal(g_message);
    }
}

static void append_path(PyConfig *config, const char *path)
{
    wchar_t *wide = Py_DecodeLocale(path, NULL);
    if (!wide) {
        fatal("Out of memory while configuring Python.");
    }
    check_status(PyWideStringList_Append(&config->module_search_paths, wide), "module_search_paths");
    PyMem_RawFree(wide);
}

static int g_argc;
static char **g_argv;
static int g_python_rv = 1;

static void *python_thread(void *arg)
{
    (void)arg;

    if (PyImport_ExtendInittab(g_builtins) < 0) {
        fatal("Could not register Ren'Py's built-in modules.");
    }

    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);

    config.site_import = 0;
    config.install_signal_handlers = 0;
    config.write_bytecode = 0;
    config.buffered_stdio = 0;
    config.optimization_level = 0;  /* matches the precompiled .pyc */
    config.use_hash_seed = 1;
    config.hash_seed = 0;

    check_status(PyConfig_SetBytesString(&config, &config.program_name, "renpy"), "program_name");
    check_status(PyConfig_SetBytesString(&config, &config.home, "romfs:/python"), "home");

    char *py_argv[] = { "romfs:/Contents/renpy.py" };
    check_status(PyConfig_SetBytesArgv(&config, 1, py_argv), "argv");

    config.module_search_paths_set = 1;
    append_path(&config, "romfs:/Contents");
    append_path(&config, "romfs:/python/lib/python3.14");
    append_path(&config, "romfs:/python/site");

    boot_log("Py_InitializeFromConfig...");
    PyStatus status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    check_status(status, "Py_InitializeFromConfig");
    boot_log("Python initialized");

    /* Stand-ins for POSIX functions NXPython lacks (switch/site). */
    if (PyRun_SimpleString("import _switch_compat") != 0) {
        boot_log("import _switch_compat failed");
    }

    /* stdout/stderr: to nxlink when connected, otherwise to a log file. */
    if (!g_nxlink) {
        PyRun_SimpleString(
            "import os, sys\n"
            "try:\n"
            "    _d = os.environ['RENPY_SWITCH_SAVE'] + 'logs'\n"
            "    os.makedirs(_d, exist_ok=True)\n"
            "    sys.stdout = sys.stderr = open(_d + '/stdout.txt', 'w', buffering=1, encoding='utf-8', errors='replace')\n"
            "except Exception:\n"
            "    pass\n");
    }

    PyRun_SimpleString(
        "import threading\n"
        "try:\n"
        "    threading.stack_size(4 * 1024 * 1024)\n"
        "except Exception:\n"
        "    pass\n");

    FILE *renpy_file = fopen("romfs:/Contents/renpy.py", "rb");
    if (!renpy_file) {
        fatal("Could not open romfs:/Contents/renpy.py.");
    }

    boot_log("running renpy.py");

    /* PyRun_SimpleFileEx closes the file. */
    int rv = PyRun_SimpleFileEx(renpy_file, "romfs:/Contents/renpy.py", 1);
    boot_log("renpy.py returned %d", rv);

    if (rv != 0) {
        snprintf(g_message, sizeof(g_message),
            "Ren'Py stopped with an error.\n\n"
            "See the logs in:\n%slogs/\n(traceback.txt, log.txt, stdout.txt)",
            g_save_dir);
        show_error(g_message);
    }

    Py_FinalizeEx();
    g_python_rv = rv == 0 ? 0 : 1;
    return NULL;
}

int main(int argc, char *argv[])
{
    boot_log("main: argc=%d argv0=%s nso=%d", argc, argc > 0 && argv[0] ? argv[0] : "(none)", (int)envIsNso());

    setenv("MESA_NO_ERROR", "1", 1);

    AppletType applet_type = appletGetAppletType();
    boot_log("applet type: %d", (int)applet_type);

    if (applet_type != AppletType_Application && applet_type != AppletType_SystemApplication) {
        /* Started from the album: ~440 MB of memory, not enough for Ren'Py. */
        consoleInit(NULL);
        padConfigureInput(1, HidNpadStyleSet_NpadStandard);
        PadState pad;
        padInitializeDefault(&pad);
        printf("\n\n  Ren'Py needs the full memory mode.\n\n"
               "  Start the Homebrew Menu by HOLDING R while launching\n"
               "  any installed game, then start this program again.\n\n"
               "  (Opened from the Album it only gets ~440 MB.)\n\n"
               "  Press + to exit.\n");
        consoleUpdate(NULL);
        while (appletMainLoop()) {
            padUpdate(&pad);
            if (padGetButtonsDown(&pad) & HidNpadButton_Plus) {
                break;
            }
            svcSleepThread(16000000ULL);
        }
        consoleExit(NULL);
        return 0;
    }

    appletLockExit();
    appletHook(&g_applet_hook_cookie, on_applet_hook, NULL);

    if (!is_dir("romfs:/Contents/renpy") || !is_dir("romfs:/python/lib/python3.14")) {
        boot_log("RomFS incomplete");
        fatal("Ren'Py runtime not found in RomFS.\n\n"
              "This .nro/.nsp was not packaged correctly: romfs:/Contents/renpy and "
              "romfs:/python/lib/python3.14 must exist.");
    }

    g_argc = argc;
    g_argv = argv;
    choose_paths(argc, argv);
    boot_log("game: %s", g_game_dir);
    boot_log("save: %s", g_save_dir);

    /* Run the interpreter on a thread with a big stack: the main thread's
     * stack depends on how the program was started. */
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, RENPY_SWITCH_STACK_SIZE);

    pthread_t thread;
    int err = pthread_create(&thread, &attr, python_thread, NULL);
    pthread_attr_destroy(&attr);

    if (err != 0) {
        boot_log("pthread_create failed: %d", err);
        python_thread(NULL);
    } else {
        pthread_join(thread, NULL);
    }

    boot_log("exit %d", g_python_rv);
    return g_python_rv;
}
