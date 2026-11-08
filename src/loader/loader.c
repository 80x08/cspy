// cspy.dll: boots an embedded CPython inside CLIP STUDIO PAINT and runs cspy\startup.py.
//
// Two ways in:
//  - permanent: copy this DLL to PlugIn\PAINT\. CSP loads every *.dll there (recursively) at startup
//    and calls TriglavPluginCall; we report success without a module ID, so CSP silently drops us
//    (no "failed plug-in" dialog) and we pin ourselves so its FreeLibrary is a no-op.
//  - dev: python inject.py (LoadLibrary via CreateRemoteThread).
// The Python folder is found next to this DLL (dev), else at <CSP exe dir>\cspy (installed).
#include <windows.h>
#include <stdio.h>

typedef void (*Py_InitializeEx_t)(int);
typedef int (*PyRun_SimpleString_t)(const char *);
typedef void *(*PyEval_SaveThread_t)(void);

static HMODULE g_self;

static void log_msg(const char *msg) { OutputDebugStringA("[cspy] "); OutputDebugStringA(msg); OutputDebugStringA("\n"); }

static int has_python(const char *root) {
    char p[MAX_PATH];
    snprintf(p, sizeof p, "%s\\python\\python314.dll", root);
    return GetFileAttributesA(p) != INVALID_FILE_ATTRIBUTES;
}

static DWORD WINAPI boot(LPVOID unused) {
    (void)unused;
    char root[MAX_PATH], code[4 * MAX_PATH], msg[2 * MAX_PATH], pydll[MAX_PATH];
    GetModuleFileNameA(g_self, root, MAX_PATH);
    *strrchr(root, '\\') = 0;
    if (!has_python(root)) {  // installed as a plugin: use <exe dir>\cspy
        GetModuleFileNameA(NULL, root, MAX_PATH);
        strcpy(strrchr(root, '\\'), "\\cspy");
        if (!has_python(root)) {
            snprintf(msg, sizeof msg, "no python folder next to the DLL or in %s", root);
            log_msg(msg);
            return 1;
        }
    }

    // python314._pth in the bundled embeddable Python sets sys.path, so no PYTHONHOME needed.
    snprintf(pydll, sizeof pydll, "%s\\python\\python314.dll", root);
    HMODULE py = LoadLibraryExA(pydll, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!py) {
        snprintf(msg, sizeof msg, "cannot load %s (error %lu)", pydll, GetLastError());
        log_msg(msg);
        return 1;
    }
    Py_InitializeEx_t init = (Py_InitializeEx_t)GetProcAddress(py, "Py_InitializeEx");
    PyRun_SimpleString_t run = (PyRun_SimpleString_t)GetProcAddress(py, "PyRun_SimpleString");
    PyEval_SaveThread_t save = (PyEval_SaveThread_t)GetProcAddress(py, "PyEval_SaveThread");
    if (!init || !run || !save) { log_msg("missing Python exports"); return 1; }

    // Forward slashes keep the path valid inside a Python raw string.
    for (char *p = root; *p; p++) if (*p == '\\') *p = '/';
    snprintf(code, sizeof code,
             "import sys, traceback\n"
             "sys.path.insert(0, r'%s')\n"
             "try:\n"
             "    import startup\n"
             "except BaseException:\n"
             "    open(r'%s/cspy_error.log', 'w').write(traceback.format_exc())\n",
             root, root);

    init(0);  // 0 = don't install Python signal handlers in the host app
    run(code);
    save();   // release the GIL so Python threads (console server) keep running
    log_msg("python started");
    return 0;
}

// Triglav plugin entry. Success (0) for module-initialize but no module ID set => CSP ignores us quietly.
__declspec(dllexport) void TriglavPluginCall(int *result, void **data, int selector, void *server, void *reserved) {
    (void)data; (void)selector; (void)server; (void)reserved;
    if (result) *result = 0;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = inst;
        DisableThreadLibraryCalls(inst);
        // Plugin + inject (or two copies) must not boot Python twice.
        char name[64];
        snprintf(name, sizeof name, "Local\\cspy-%lu", GetCurrentProcessId());
        HANDLE once = CreateMutexA(NULL, FALSE, name);
        if (!once || GetLastError() == ERROR_ALREADY_EXISTS) return TRUE;
        // CSP unloads plugins it doesn't use; stay resident because Python threads run our code.
        HMODULE pinned;
        GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_PIN | GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS,
                           (LPCSTR)&DllMain, &pinned);
        // Python can't start under the loader lock, so boot on a fresh thread.
        HANDLE t = CreateThread(NULL, 0, boot, NULL, 0, NULL);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
