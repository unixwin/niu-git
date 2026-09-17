# Upstream test suite baseline (niu-git build)

- Upstream: git-for-windows v2.55.0.windows.2, CMake/MSVC build, out-of-tree
- Scope: `t0*` + `t1[0-4]*` (150 files), real Git Bash, JOBS=4
- Result: **ok=3401 / notok=3518 (49%)** — see classification below; the raw
  number is dominated by a handful of Windows-noise families, not product bugs.

| Family | Files | ~notok | Nature |
|---|---|---|---|
| eol/crlf/conversion | t0020 t0021 t0022 t0026 **t0027** t1051 | ~2280 | t0027 alone = 2159. Needs Git-for-Windows-style test patches (their CI carries Windows eol test adjustments); product eol semantics need dedicated triage |
| path-format diffs | t0008 t0050 t0056 t0060 ... | ~450 | git prints `D:/...` where POSIX tests expect `/d/...` (check-ignore -v etc.) |
| test-lib meta | t0000 | 50 | nested test-lib output comparison, env-sensitive |
| symlink-sensitive (0-ok files) | t1092 t1013 t1460 t1423 t1001 t1004 ... | ~350 | host env forced `MSYS=winsymlinks:nativestrict` unset → Git Bash `ln -s` degrades to copy; rerun with nativestrict before blaming the product |
| reftable/reffiles backend | t0600-0614 | ~130 | backend edge cases, needs triage |
| network-dependent | t0410 t0411 | ~40 | partial-clone promisor remote |
| scattered | t1300(7) t1006(22) t1450(50) ... | ~200 | real triage list, expected mostly-small path/format diffs |

Clean exemplars: t0006-date 149/149, t0012-help 182/182, t1006-cat-file 269/269,
t0014-alias 23/23, t0013-sha1dc 1/1, t0005-signals 5/5, t0007-git-var 27/27.

## How to reproduce

```
env -u MSYS_NO_PATHCONV -u MSYS2_ARG_CONV_EXCL -u MSYS \
  "/c/Program Files/Git/bin/bash.exe" scripts/test.sh "t0*"
```

Notes:
- `GIT_BUILD_DIR`/`TEST_DIRECTORY` must be Windows-form paths (test-lib normalizes
  to MSYS form; Strawberry perl cannot read those — `build/t-perl-shim/perl`
  bridges it. Re-apply `PERL_PATH` in `build/GIT-BUILD-OPTIONS` after any reconfigure.)
- Known env trap: `MSYS_NO_PATHCONV=1` (set by niubash/WorkBuddy hosts) silently
  breaks every absolute-path argument to the native git.exe.

## Next triage order

1. Re-run with `MSYS=winsymlinks:nativestrict` (expect big recovery in 0-ok files)
2. t0027 family: port Git for Windows' eol test adjustments as `patches/t/`
3. t0008-family path-format diffs: decide patch vs. test-expectation updates
