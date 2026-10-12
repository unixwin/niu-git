// niu-git root entry launcher.
//
// wpm's shim layout forwards the package command to opt\<pkg>\<cmd>.exe —
// the package root. A bare copy of mingw64/bin/git.exe there breaks
// git-for-windows RUNTIME_PREFIX layout discovery (exec-path, template dir
// and PATH-based DLL lookup all resolve relative to mingw64/bin), so https
// remote helpers and init templates die. This tiny launcher keeps the real
// exe in mingw64/bin and just pins the environment before forwarding.
//
// argv fidelity: arguments are forwarded byte-exactly. The child command
// line is built with standard Windows quoting rules (quote only when the
// argument is empty or contains space/tab/quote; inside quotes, double the
// backslashes preceding a quote, emit an embedded quote as \", and double
// trailing backslashes before the closing quote). Re-quoting with
// unconditional "..." wrappers corrupts any argument that itself contains a
// quote or ends in a backslash (commit -m payloads, pathspecs like D:\dir\),
// so that must never be done here.

#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

static void join(WCHAR *dst, size_t cap, const WCHAR *dir, const WCHAR *rest)
{
    _snwprintf(dst, cap, L"%s\\%s", dir, rest);
    dst[cap - 1] = 0;
}

/* Longest possible quoted form of arg: every char doubled (backslash runs)
 * plus two quotes plus one separator. */
static size_t quoted_cap(const WCHAR *arg)
{
    return 2 * wcslen(arg) + 3;
}

/* Append arg to d using standard Windows command-line quoting; returns the
 * advanced position. See "Everyone quotes command line arguments the wrong
 * way" — the rules implemented by CommandLineToArgvW and the MSVCRT. */
static WCHAR *append_quoted(WCHAR *d, const WCHAR *arg)
{
    const WCHAR *p;
    size_t bs;
    int special = (arg[0] == L'\0') || wcspbrk(arg, L" \t\"") != NULL;

    if (!special) {
        size_t n = wcslen(arg);
        wmemcpy(d, arg, n);
        d += n;
        *d++ = L' ';
        return d;
    }

    *d++ = L'"';
    for (p = arg; ; ) {
        bs = 0;
        while (*p == L'\\') { bs++; p++; }
        if (*p == L'"') {
            /* backslashes before a quote double, quote becomes \" */
            while (bs--) { *d++ = L'\\'; *d++ = L'\\'; }
            *d++ = L'\\';
            *d++ = L'"';
            p++;
        } else if (*p == L'\0') {
            /* backslashes before the closing quote double */
            while (bs--) { *d++ = L'\\'; *d++ = L'\\'; }
            break;
        } else {
            while (bs--) *d++ = L'\\';
            *d++ = *p++;
        }
    }
    *d++ = L'"';
    *d++ = L' ';
    return d;
}

int wmain(int argc, WCHAR **argv)
{
    WCHAR dir[MAX_PATH];
    DWORD n = GetModuleFileNameW(NULL, dir, MAX_PATH);
    if (n == 0 || n >= MAX_PATH)
        return 127;
    WCHAR *slash = wcsrchr(dir, L'\\');
    if (!slash)
        return 127;
    *slash = 0;

    WCHAR real[MAX_PATH];
    join(real, MAX_PATH, dir, L"mingw64\\bin\\git.exe");
    if (GetFileAttributesW(real) == INVALID_FILE_ATTRIBUTES) {
        fprintf(stderr, "git: launcher cannot find %ls\n", real);
        return 127;
    }

    WCHAR exec_path[MAX_PATH];
    join(exec_path, MAX_PATH, dir, L"mingw64\\libexec\\git-core");
    SetEnvironmentVariableW(L"GIT_EXEC_PATH", exec_path);

    WCHAR templates[MAX_PATH];
    join(templates, MAX_PATH, dir, L"mingw64\\share\\git-core\\templates");
    SetEnvironmentVariableW(L"GIT_TEMPLATE_DIR", templates);

    // mingw64/bin first so subprocess git-remote-https finds its DLLs.
    WCHAR bin[MAX_PATH], old_path[32768], new_path[32768];
    join(bin, MAX_PATH, dir, L"mingw64\\bin");
    DWORD got = GetEnvironmentVariableW(L"PATH", old_path, 32768);
    if (got == 0 || got > 32768)
        old_path[0] = 0;
    _snwprintf(new_path, 32768, L"%s;%s", bin, old_path);
    new_path[32767] = 0;
    SetEnvironmentVariableW(L"PATH", new_path);

    /* Build the child command line: "git.exe" followed by the arguments,
     * quoted per standard rules so each argument round-trips byte-exactly. */
    size_t cap = 16; /* "git.exe" + space + NUL */
    for (int i = 1; i < argc; i++)
        cap += quoted_cap(argv[i]);
    WCHAR *cmdline = malloc(cap * sizeof(WCHAR));
    if (!cmdline) {
        fprintf(stderr, "git: launcher out of memory\n");
        return 127;
    }
    WCHAR *d = cmdline;
    *d++ = L'"'; *d++ = L'g'; *d++ = L'i'; *d++ = L't';
    *d++ = L'.'; *d++ = L'e'; *d++ = L'x'; *d++ = L'e'; *d++ = L'"'; *d++ = L' ';
    for (int i = 1; i < argc; i++)
        d = append_quoted(d, argv[i]);
    *d = L'\0';

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    if (!CreateProcessW(real, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        fprintf(stderr, "git: launcher CreateProcess failed (%lu)\n", GetLastError());
        free(cmdline);
        return 127;
    }
    free(cmdline);
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
