# Upstream test suite baseline (niu-git build)

- Upstream: git-for-windows v2.55.0.windows.2, CMake/MSVC build, out-of-tree
- Scope: `t[01]*` (150 files), real Git Bash, JOBS=4
- **Valid clean baseline (testrun6, 2026-09-17 ~13:50):**
  **ok=9540 / notok=355, of which 162 are upstream `# TODO known breakage`
  (test_expect_failure accounting) → 193 real failures ≈ 98.0% real pass rate.**
- Product bundle: 20.2 MB zip, MinGit layout, schannel backend.

## Confirmed product fix from this baseline effort

**Server dashed forms** (`git-upload-pack`, `git-receive-pack`,
`git-upload-archive`): upstream Makefile marks them "special" — they must
exist as standalone names (remote helpers invoke them by dashed name).
CMake's install stage creates them unconditionally (bin_links, not gated by
SKIP_DASHED_BUILT_INS) so the product bundle was always fine; but the
*build tree* only gets them via the git-links target, which
`SKIP_DASHED_BUILT_INS=ON` skips — breaking every local/file clone in the
test harness (bin-wrappers point at the build tree).
`scripts/build.py` now recreates the three hardlinks after build (264fd90).
Verified: t1507 20 notok → 29/29 standalone; t1013 58 notok → 6.

## Known-good clean exemplars (testrun6)

t0006-date 149/149, t0012-help 182/182, t1006-cat-file 269/269,
t0014-alias 23/23, t0013-sha1dc 1/1, t0005-signals 5/5, t0007-git-var 27/27,
t0027-auto-crlf 260/261 (the 1 was a host-harness artifact, see below).

## Real failure families that survived the clean run (triage list)

| Family | Files | ~notok | Notes |
|---|---|---|---|
| partial-clone promisor | t0410 t0411 t1022 | ~22 | network-dependent, needs local http remote triage |
| sparse-checkout | t1090 t1091 t1092 | ~24 | needs dedicated triage |
| submodule recursion | t1013 | 58→6 after upload-pack fix; residual 6 need review |
| delayed checkout process filter | t0021 | 4 | long-running filter protocol |
| hook stdio redirection | t1800 | 6 | client/server hook stdout/stderr semantics |
| cat-file --batch-all-objects | t1006 t1007 | ~12 | needs review |
| scattered | t0033 t0050 t0060 t1450 t1500 ... | ~40 | per-test triage |

## Invalid runs (do not cite)

- **testrun1** (49%): host safe-delete shim (BASH_FUNC_rm%% env-exports)
  hijacked every `rm`; ~2200 phantom eol failures in t0027 alone.
- **testrun3**: `unset -f` doesn't survive xargs child shells (functions
  re-import from BASH_FUNC_* env); all 150 files bailed at setup.
- **testrun7** (5h) + **testrun8** (subset): poisoned by a machine-level
  MSYS2/filesystem anomaly starting 17:55:46 — six git.exe processes froze
  at that second holding trash-directory handles (delete-pending → all
  later create/remove on those paths fail), and `ln -s dir-target` began
  reporting failure (rc=1) even though the symlink is created and usable
  (verified with both coreutils 8.32 copies). Suspected filter-driver /
  Defender update at that moment. Fix attempt: reboot, then rerun
  `scripts/test.sh` (36-file globs subset committed in scripts/test-globs.txt).

## How to reproduce

```
"/c/Program Files/Git/bin/bash.exe" scripts/test.sh            # full t[01]*
JOBS=2 "/c/Program Files/Git/bin/bash.exe" scripts/test.sh     # low parallelism
# subset: edit scripts/test-globs.txt (bare names, one per line)
```

Notes:
- Launchers must pass NO glob argument under the WorkBuddy/CodeBuddy host:
  its wrapper eval mangles quoted globs. The script self-scrubs
  MSYS_NO_PATHCONV/MSYS2_ARG_CONV_EXCL and BASH_FUNC_* safe-delete shims.
- `GIT_BUILD_DIR`/`TEST_DIRECTORY` are derived automatically (Windows-form
  paths; `build/t-perl-shim/perl` bridges Strawberry perl — re-apply
  `PERL_PATH` in `build/GIT-BUILD-OPTIONS` after any reconfigure).
- Before running: no orphan git.exe/bash.exe from previous runs may hold
  trash-directory handles (they cause delete-pending Permission-denied
  bail-outs). Check with `Get-Process -Name git,bash,sh`.
