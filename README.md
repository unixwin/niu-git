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
