// niu-git root entry launcher.
//
// wpm's shim layout forwards the package command to opt\<pkg>\<cmd>.exe —
// the package root. A bare copy of mingw64/bin/git.exe there breaks
// git-for-windows RUNTIME_PREFIX layout discovery (exec-path, template dir
// and PATH-based DLL lookup all resolve relative to mingw64/bin), so https
// remote helpers and init templates die. This tiny launcher keeps the real
// exe in mingw64/bin and just pins the environment before forwarding.

#include <windows.h>
#include <stdio.h>

static void join(WCHAR *dst, size_t cap, const WCHAR *dir, const WCHAR *rest)
{
    _snwprintf(dst, cap, L"%s\\%s", dir, rest);
    dst[cap - 1] = 0;
}

static void quote_append(WCHAR *dst, size_t cap, const WCHAR *arg)
{
    size_t len = wcslen(dst);
    dst += len;
    cap -= len;
    _snwprintf(dst, cap, L"\"%s\" ", arg);
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

    WCHAR cmdline[32768] = L"\"git.exe\" ";
    for (int i = 1; i < argc; i++)
        quote_append(cmdline, 32768, argv[i]);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    memset(&si, 0, sizeof(si));
    si.cb = sizeof(si);
    memset(&pi, 0, sizeof(pi));
    if (!CreateProcessW(real, cmdline, NULL, NULL, TRUE, 0, NULL, NULL, &si, &pi)) {
        fprintf(stderr, "git: launcher CreateProcess failed (%lu)\n", GetLastError());
        return 127;
    }
    WaitForSingleObject(pi.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(pi.hProcess, &code);
    CloseHandle(pi.hProcess);
    CloseHandle(pi.hThread);
    return (int)code;
}
