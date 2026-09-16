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

## Build

```
python scripts/build.py            # configure + build + collect into dist/
python scripts/build.py --prepare  # re-fetch/extract upstream tarball
```

Source code is NEVER committed here: CI pulls the upstream tarball and applies
`patches/`. License: GPLv2 (upstream git); keep this repo patch-only.
