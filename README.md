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
- Bundle: 20 MB / zip 20 MB (MinGit zip: 37 MB) — no MSYS2, no openssl, no perl
- Patch #1: tolerate stale `http.sslBackend` from other Git distributions on
  single-backend (schannel) builds — warn + fall back instead of dying

## Test

Run upstream `t/` suite against the CMake build (subset: `t0*`, `t1[0-4]*`).
Must use real Git Bash — the niubash/WorkBuddy host sets `MSYS_NO_PATHCONV=1`,
which breaks absolute-path args to the native git.exe:

```
env -u MSYS_NO_PATHCONV -u MSYS2_ARG_CONV_EXCL -u MSYS \
  "/c/Program Files/Git/bin/bash.exe" scripts/test.sh "t0*"
```

Logs land in `testlog/`; the script prints an ok/notok aggregate. Baseline
t0001-init: 102/103 (only `includeIf.onbranch` re-init edge case fails).

## WPM package

`wpm/niugit.json` is the ready-to-merge official-index entry (schema 1,
`layout: shim`). It points at the future GitHub Release URL; the `sha256` and
`size` match the local build `niu-git-2.55.0.windows.2-x64.zip` (regenerate +
rehash on every release). The existing `git` entry (MinGit) stays untouched
until niu-git is promoted to be the default.

## Build

```
python scripts/build.py            # x64: configure + build + collect into dist/
python scripts/build.py --prepare  # re-fetch/extract upstream tarball
python scripts/build.py --arch arm64   # needs the "MSVC ARM64 build tools"
                                       # VS component (not installed here yet)
```

Source code is NEVER committed here: CI pulls the upstream tarball and applies
`patches/`. License: GPLv2 (upstream git); keep this repo patch-only.
