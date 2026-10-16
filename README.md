# niu-git

Native Windows git without MSYS — built with MSVC, no bash/perl/MSYS2 bundled.
Shell-script parts (hooks, `git-mergetool`, `git-bisect`, ...) are provided by
the [niubash](https://github.com/unixwin/niubash) `sh` shim found on `PATH`.

See `docs/planning/niubash-git.md` in the niubash repo for the full plan.

## Status

Phase 0 PoC **done**: MSVC + CMake + vcpkg build of git-for-windows v2.55.0.windows.2.

- `python scripts/build.py` -> `dist/` (MinGit-style layout: `mingw64/bin`, `etc/`)
- Verified: init/add/commit/log/status, https clone via schannel, sh hooks executed
  via `sh` found on PATH (niubash shim handoff mechanism confirmed)
- Bundle: 29 MB / zip 33 MB (MinGit zip: 37 MB) — zlib/iconv linked
  statically (no `zlib1.dll`/`iconv-2.dll` in `git.exe`), no MSYS2, no
  openssl, no perl
- Patch #1: tolerate stale `http.sslBackend` from other Git distributions on
  single-backend (schannel) builds — warn + fall back instead of dying
- Patch #2: accept POSIX-shaped path arguments (`/d/repo/...`) via
  exists-checked fallback conversion; message payloads (`-m`/`--message`
  values) are never rewritten — messages are literal bytes
- Patch #3: self-locate the system prefix — `system_prefix()` validates its
  strip result against `<prefix>/<GIT_EXEC_PATH>` and, when that fails,
  probes upward for the payload markers (`opt/niugit/mingw64`, `mingw64`),
  so the bare `usr/bin/git.exe` of wpm's `layout: flat` finds its
  exec-path/templates/system config with no wrapper env at all
- Dispatcher retired (v2.55.0.5): the root entry launcher is no longer
  shipped — with patch #3 the real `mingw64/bin/git.exe` maps straight to
  `<root>/usr/bin/git.exe`, removing the launcher+dispatcher hop. Measured on
  a real `wpm` flat install: `git --version` 13.7 ms avg / 11.5 ms min
  (shim chain: 48.5 ms avg / 43.9 ms min), `git status` 22.6 ms avg.
  `scripts/entry/launch-git.c`
  stays buildable (`build.py --entry-only`) for the differential suite
  below; it forwarded argv byte-exactly using standard Windows quoting
  rules (quotes/trailing backslashes round-trip, `commit -m` payloads,
  `D:\dir\` pathspecs, CJK and multiline messages survive verbatim)

## Speed

Startup cost of the `git` on your PATH, measured on Windows x64 with a warm
cache (Python `subprocess` timing, 15 iterations, small repository, 2026-10):

| `git` on PATH | `git --version` | `git status --porcelain` |
|---|---|---|
| **niu-git (flat layout)** | **13.7 ms avg / 11.5 ms min** | **22.6 ms avg / 17.4 ms min** |
| Git for Windows `mingw64\bin\git.exe` | 51.3 ms avg | 30.6 ms avg |
| Git for Windows `cmd\git.exe` (the usual PATH entry) | 45.3 ms avg | 50.1 ms avg |
| `time git …` inside a MINGW64 Git Bash prompt | 73-103 ms real | — |

The difference is structural, not tuning. Git Bash runs git inside the MSYS2
runtime (and from cmd/PowerShell it is additionally reached through a
`cmd\git.exe` launcher), so every invocation pays for a POSIX runtime
initialisation — and, in a Git Bash prompt, a fork. In niu-git
`usr\bin\git.exe` *is* the git binary — one `CreateProcess`, no MSYS DLLs, no
bash and no perl in the process tree; patch #3 is what lets that bare binary
find its own payload, so there is no wrapper hop either.

Git Bash also bundles perl, which its `git send-email`, `git svn`,
`git cvsimport`, `git cvsexportcommit` and `git archimport` need (and its
test suite and hook samples use). niu-git has no perl dependency: those
commands are absent instead of failing at runtime, and the shell-script
parts of git that do matter — hooks, `git bisect`, `git mergetool`,
`git filter-branch`, `git submodule`, `git request-pull` — run on the `sh`
that [niubash](https://github.com/unixwin/niubash) provides.

## Compared with Git Bash

|  | Git Bash (Git for Windows) | niu-git |
|---|---|---|
| git binary | MSYS2 runtime + `cmd\git.exe` launcher | real git C source (MSVC), no POSIX layer |
| `git --version` | 45-103 ms | **11-14 ms** |
| POSIX tools / shell | bundled (bash, coreutils, openssl, perl) | none bundled — `sh` + coreutils come from [niubash](https://github.com/unixwin/niubash) and [winuxcmd](https://github.com/unixwin/winuxcmd) |
| TLS | OpenSSL (bundled) or schannel | schannel only, no OpenSSL |
| perl commands (`send-email`, `svn`, `cvs*`) | bundled with perl | not available |
| `git grep -P` (PCRE2) | yes | no — use winuxcmd's `grep -P` instead |
| git-gui / gitk (Tcl/Tk) | bundled | not shipped |
| credential helper | Git Credential Manager in the system gitconfig | not configured by default (see below) |
| ARM64 | yes | x64 only (so far) |

`git --version` carries both the upstream suffix and our marker
(`2.56.0.windows.2.niugit`), so a bug report identifies the distribution as
well as the base — a bare `2.55.0` was what upstream's fallback produced
because the tarball ships no `version` file.

Credentials: a helper you configured yourself works unchanged — niu-git
reads the same user config, so a host-scoped `credential.<url>.helper`, Git
Credential Manager, or `gh auth git-credential` keeps answering exactly as
it did. Only a helper that lived in Git Bash's *system* gitconfig
(`credential.helper=manager` there) is invisible to niu-git; for that case
the bundle ships git's own tiny `wincred` helper (172 KB, built from
`contrib/credential/wincred`), which reads the
`git:<protocol>://<host>` entries already in the Windows Credential Manager
(the names Git Credential Manager writes):

```
git config --global credential.helper wincred                    # the bundled helper
git config --global credential.helper manager                    # or Git Credential Manager, if installed
git config --global credential.helper '!gh auth git-credential' # or GitHub CLI tokens
```

niu-git deliberately sets no credential helper in its system gitconfig: git
consults helpers in system -> global -> local order, so a system default
would run *before* a helper you chose yourself.

## Argv fidelity test

`python scripts/test-launcher.py` runs a differential suite: the same command
goes through the launcher and the real `mingw64/bin/git.exe` in twin repos,
byte-comparing stdout/stderr/exit code plus HEAD message bytes, reflog and
status. Message payloads are additionally asserted against their intended
literal bytes (catches a bug shared by both stacks). Since v2.55.0.5 the
launcher is not shipped, so build it first with `python scripts/build.py
--entry-only` (it lands in `dist/git.exe`) or point `NIUGIT_TEST_WRAPPER`
at an existing copy. Target selection: `NIUGIT_TEST_WRAPPER` /
`NIUGIT_TEST_REAL` (exe paths), `NIUGIT_TEST_ROOT` (dir with `git.exe` +
`mingw64/`), then `./dist` after a build.

## Test

Run upstream `t/` suite against the CMake build (subset: `t0*`, `t1[0-4]*`).
Must use a real Git Bash — the niubash/WorkBuddy host sets `MSYS_NO_PATHCONV=1`,
which breaks absolute-path args to the native git.exe (on hosts where the
Niubash shim replaced `C:\Program Files\Git`, point at any real Git Bash,
e.g. `D:\Git\bin\bash.exe`):

```
env -u MSYS_NO_PATHCONV -u MSYS2_ARG_CONV_EXCL -u MSYS \
  "/c/Program Files/Git/bin/bash.exe" scripts/test.sh "t0*"
```

The first argument accepts bare test names (`scripts/test-globs.txt`, one
per line when omitted) or wildcards (`"t0*"`). Perl comes from the
`t-perl-shim` auto-detect: Strawberry if installed, else the Git Bash
MSYS perl — neither is required for the non-perl tests.

Logs land in `testlog/`; the script prints an ok/notok aggregate. Baseline:
curated subset 2861 ok / 14 `# TODO known breakage` / 0 hard failures;
t0001-init: 102/103 (only `includeIf.onbranch` re-init edge case fails).

## WPM package

`wpm/niugit.json` is the ready-to-merge official-index entry (schema 1,
`layout: flat`): the payload lands under `opt/niugit/` and
`mingw64/bin/git.exe` is mapped onto the shared `usr/bin/git.exe` — no root
launcher, no shim dispatch (patch #3 makes the bare git.exe self-locate).
The `sha256` and `size` match the local build
`niu-git-2.55.0.windows.2-x64.zip` (regenerate + rehash on every release;
keep the `niu-git-` file-name prefix — v2.55.0.4 was uploaded as
`niugit-...zip` and its index URL has 404'd ever since). The existing `git`
entry (MinGit) stays untouched until niu-git is promoted to be the default.
Installed users migrate with:

```
wpm update winuxcmd          # wpm >= 1.1.9 is required, see below
wpm install niugit --force   # replaces the usr/bin/git.exe shim with the real binary
```

The flat layout needs **WinuxCmd/wpm 1.1.9 or newer**: older wpm broke the
`usr\bin` command hardlink with a plain `DeleteFileW`, which self-locks
against the running `wpm.exe` image and failed with `failed to break
WinuxCmd hardlink ... Access denied`. The upgrade overwrites the payload
under `opt/niugit/` in place, so the retired shim launcher
`opt/niugit/git.exe` (and stale files inside the payload directories) can
survive; the launcher is unreferenced and safe to delete. Running
`wpm uninstall niugit -y` before the install rebuilds the payload
directories from scratch instead.

## Build

```
python scripts/build.py            # x64: configure + build + collect into dist/
python scripts/build.py --prepare  # re-fetch/extract upstream tarball
python scripts/build.py --arch arm64   # needs the "MSVC ARM64 build tools"
                                       # VS component (not installed here yet)
```

Source code is NEVER committed here: CI pulls the upstream tarball and applies
`patches/`. License: GPLv2 (upstream git); keep this repo patch-only.
