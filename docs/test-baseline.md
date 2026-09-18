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
- **2026-09-18 triage (3 rounds)**: still invalid as a *pass-rate* baseline
  (machine anomaly active), but produced three real fixes and a candidate
  list (below).

## Fixes landed 2026-09-18

1. **t-perl-shim eval-quoting** (7ed2701): the shim rebuilt the command
   line as a quoted string + eval, so perl one-liners containing
   backslashes (hex2oct's `printf "\\%03o"`) were re-parsed by the shell
   and corrupted → t1007 32-34 false failures. Rewritten to round-trip
   args through positional parameters; shipped via
   `build.py install_perl_shim()`. Verified: t1007 44/44.
2. **POSIX-form TEST_DIRECTORY/GIT_BUILD_DIR** (3a34b92): Windows-form
   paths in PATH are invisible to MSYS lookup — every bare `test-tool`
   invocation died "command not found" (stderr swallowed into the helper
   `perf` file). t1419 3/13 → 13/13, t1450 96 ok / 0 fail.
3. **BASH_ENV scrub** (7ed2701): the host shell-runtime shim re-defines
   `rm` in every non-interactive bash via BASH_ENV; `env -u BASH_FUNC_*`
   alone is insufficient. Runners re-exec with `-u BASH_ENV` too.

## Machine anomaly status (blocks a final clean baseline)

Still present without reboot, **now drive-agnostic**: batch
`update-ref --stdin` transactions with nested ref paths
(`refs/heads/foo/1`) + `pack-refs --all` silently lose all refs and
(loosely) objects — packed-refs ends up header-only. Reproduced 3/3 with
**official Git for Windows 2.55.0.windows.3**, on C:\ (intermittent,
worse under load) and D:\ (persistent). Single-ref updates are fine.
Downstream: `git prune` deletes reachable objects (ref enumeration sees
nothing). This poisons every test whose setup creates nested refs in a
single transaction (t1408/t1419/t1460/t1461/... in the 09-18 runs).
Verification recipe (does NOT need the niu-git build):

```
git init -q -b main d && cd d && echo x>f && git add f && git commit -qm c
bb=$(git rev-parse HEAD)
{ for n in a b c; do echo "create refs/heads/$n/x $bb"; done; } | git update-ref --stdin
git pack-refs --all && git show-ref | wc -l   # 3 = healthy, 0 = anomaly active
```

## Candidate real product issues (post-reboot triage queue)

Consistently reproduced across the 09-18 runs regardless of anomaly
phase; still need one clean confirmation each after reboot:

| Candidate | Tests | Notes |
|---|---|---|
| sparse-checkout | t1091 (10), t1092 (14 + t/o) | cone mode, worktrees, merge conflicts |
| submodule ops hang | t1013 t/o, t1006 #257 | read-tree-submodule times out on C: too |
| safe.directory normalization | t0033 (6) | checked/configured path normalization, dot, asterisk |
| --follow-symlinks prep | t1006 #225-244 | root cause = test 225 prep failure |
| path-utils | t0060 (3) | real-path-on-symlinks ×2, MSYSTEM/PATH adjustment |
| reffiles fsck | t0602 (3) | ref name check, symlink symref content |
| @{-1} in check-ref-format --branch | t1402 (2) | #82, #84 |
| promisor.quiet in submodule | t0410 #38 | |
| delayed checkout submodule collision | t0021 #33 | |
| rev-parse --push / at-combinations | t1514 (6), t1508 (2) | |
| repo structure / subdirectory symlink | t1901 (1), t1020 #15 | |

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
