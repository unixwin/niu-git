# niu-git agent guide

Repository rules for anything (human or agent) that builds, patches, tests or
releases niu-git. Everything here was learned by breaking it in CI or on a
real machine; each rule names the incident so a future agent can judge whether
it still applies.

## What this repo is

A patch-only distribution of git-for-windows: the upstream tarball is fetched,
`patches/*.patch` are applied, and the result is built with MSVC + CMake +
vcpkg into a MinGit-style `dist/`. `src/` is fetched, never committed.

## Build

- Always build with `python scripts/build.py` (add `--arch arm64` for arm64);
  never hand-edit `dist/`. For a from-scratch fetch use `--prepare`.
- `NIUGIT_VCPKG_ROOT` selects the vcpkg root (default `D:/vcpkg`). The build
  needs two triplets: `<arch>-windows` (curl[schannel]/expat dynamic) and
  `<arch>-windows-static-md` (zlib/iconv static).
- Local builds are for quick checks only. **CI is the builder of record**:
  `.github/workflows/build-niugit.yml` cross-builds x64 + arm64 on
  `windows-2025`, runs the upstream test subset, and smoke-tests the arm64
  binary on a native `windows-11-arm` runner (an x64 machine cannot execute
  arm64 binaries, so arm64 correctness is only provable there).

### Build invariants (enforced by `scripts/check-dist.py`, run it locally too)

- `git.exe` imports **no** `zlib1.dll`/`iconv-2.dll` (static zlib/iconv). The
  runtime DLL closure is derived from the import tables, never hardcoded: a
  hardcoded pair shipped a release without `libexpat.dll` and `git http-push`
  could not start on any machine.
- Binaries carry **no build-directory paths**: `configure()` passes
  `/experimental:deterministic /pathmap:<src root>=src /Brepro` so `__FILE__`
  becomes `src\...` and the PE timestamp is a content checksum. Without it the
  binary leaked 2 600+ absolute paths, differed between machines, and a
  pinned sha256 stopped meaning "the bytes I built".
- `bin/` does not carry the dashed server forms (`git-upload-pack.exe` …):
  they live in `libexec/git-core`, which is what `setup_path` prepends.
  Shipping both duplicated ~15 MB in the zip because a zip explodes
  hardlinks.
- `libexec/git-core` contains `git-credential-wincred.exe` (built from
  `contrib/credential/wincred`), the perl commands under their **dashed**
  names (the `.perl` name alone is not what git looks up), and
  `etc/gitconfig` with `[http] sslBackend = schannel`.
- Deliberately **no** `credential.helper` in `etc/gitconfig`: git consults
  helpers system → global → local, so a system default would answer before a
  helper the user configured (`gh auth git-credential`, GCM, …). The helper
  binary ships; the recipe is documented in the README.

## Patches

- Patches are consumed by `git apply -p1 --directory=src` (see
  `apply_patches()`), so paths inside a patch are upstream-root relative
  (`a/http.c`, `a/contrib/buildsystems/CMakeLists.txt`).
- **Keep patches LF-only.** `.gitattributes` pins `*.patch text eol=lf`
  because a checkout with `core.autocrlf=true` (the GitHub Windows runners)
  rewrote them as CRLF; the context then failed against the LF sources and CI
  reported a baffling `patch does not apply` far from the cause.
  `apply_patches()` also normalizes a CRLF copy defensively.
- Every patch needs a header (`From:`/`Subject:`) plus a justification, per
  `patches/README.md`. Current set: 0001 sslBackend tolerance, 0002
  POSIX-shaped path args, 0003 `system_prefix` self-location (required by
  wpm's flat layout), 0004 CMake `GIT-BUILD-OPTIONS` (see below).
- **0004 exists because a clean `-DBUILD_TESTING=OFF` build cannot work
  upstream**: the block that writes `$CMAKE_BINARY_DIR/GIT-BUILD-OPTIONS` sits
  inside `if(BUILD_TESTING)` while `tools/generate-{script,perl,python}.sh`
  (whose custom commands are outside the guard) source that file. A reused
  build directory hides it; a fresh CI checkout fails in three generators.
- Use `git diff --no-index` to generate a patch on Windows: MSYS `diff -u`
  emits corrupt hunk headers, and winuxcmd's `patch(1)` does not strip `-p1`.

## Upstream baseline

- `GIT_TAG` is the single source of truth (the release asset name is derived
  from it: `niu-git-<GIT_TAG without v>-<arch>.zip`).
- `GIT_TARBALL_SIZE`/`GIT_TARBALL_SHA256` pin the codeload archive; `prepare()`
  verifies and retries. A truncated tarball still extracts a plausible tree
  and used to surface as a patch-context mismatch.
- Bumping the baseline: change `GIT_TAG`, refresh the pinned size/sha256,
  confirm every patch still applies (a five-minute local `--prepare` +
  `apply_patches()` check), then let CI build both architectures.

## Release

- Merge the CI branch to `master`, then tag `v<upstream version>.<counter>`
  (e.g. `v2.56.0.1` on the `2.56.0.windows.2` baseline) **on the merged
  master commit** and push the tag. The tag run builds both assets, publishes
  the GitHub release, and (only if `WPM_SOURCE_TOKEN` is configured) updates
  the wpm index.
- **`WPM_SOURCE_TOKEN` is not configured for niu-git**, so the index step
  warns and skips: after a release, update `unixwin/wpm-source` by hand —
  version, per-arch sha256/size (read them from the CI packaging step's log
  line, then cross-check against the downloaded asset), `urls`, the `files`
  mapping from `wpm/niugit.json`, and `layout: flat`. Confirm the published
  index before telling anyone to install.
- Migration for users is `wpm update winuxcmd` (needs wpm ≥ 1.1.9) then
  `wpm install niugit --force`; document prerequisites in the README, not only
  in the release notes.

## Machine traps that look like product bugs

- **fsmonitor daemons hold the payload**: `git fsmonitor--daemon run --detach`
  runs from `opt/niugit/mingw64/libexec/git-core/git.exe`, so a later
  `wpm install niugit --force` can fail with "being used by another process"
  on that file. List holders with PowerShell (`Get-Process | Where-Object {
  $_.Modules.FileName -ieq <path> }`), kill them, install immediately.
- The local wpm index cache can lag the published one (mirror/CDN ordering):
  if an install uses a stale entry, `wpm index update` and retry.
- niu-git has no CI-only knowledge: anything you verify locally must be
  reproducible from the workflow file plus `scripts/`.
