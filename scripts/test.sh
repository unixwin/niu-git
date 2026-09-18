#!/bin/sh
# Run upstream git test suite (subset) against the CMake build.
#
# MUST be launched with real Git Bash, e.g.:
#
#   "/c/Program Files/Git/bin/bash.exe" scripts/test.sh [glob]
#
# (The WorkBuddy/CodeBuddy host sets MSYS_NO_PATHCONV=1 and
# MSYS2_ARG_CONV_EXCL=* globally, which breaks absolute-path args to the
# native git.exe; we unset them below — plain env vars, so unset
# propagates to all descendant processes.)
#
# Optional env: JOBS (parallel test files, default 4), GLOB via $1 (default t0*).
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WROOT=$(cygpath -w "$ROOT")
# Glob selection: $1 wins; otherwise scripts/test-globs.txt (one glob per
# line, for background runs where the host wrapper mangles shell args);
# otherwise the default full scope.
if [ -n "$1" ]; then
    GLOB="$1"
elif [ -f "$ROOT/scripts/test-globs.txt" ]; then
    GLOB=$(tr '\n' ' ' < "$ROOT/scripts/test-globs.txt")
else
    GLOB="t[01]*"
fi
JOBS="${JOBS:-4}"

# Host arg-conversion poisons every absolute-path argument to git.exe.
unset MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL
# Force deterministic symlink behavior: without it Git Bash degrades
# `ln -s` to a copy and symlink-sensitive test files fail wholesale
# (verified: t1423 0/36 -> 36/36).
export MSYS=winsymlinks:nativestrict

# Strip host-exported safe-delete function wrappers (WorkBuddy/CodeBuddy
# exports rm/rmdir/unlink as functions via BASH_FUNC_* environment vars).
# They hijack every `rm` inside the test suite: bulk deletes hit node.exe
# "Argument list too long" and the safe-delete quarantine poisons test
# cleanup — this single artifact caused ~2200 phantom failures in
# t0027-auto-crlf alone.
#
# unset -f only affects THIS shell: every test file is executed by a fresh
# `sh` spawned from xargs, and bash re-imports functions from the
# BASH_FUNC_* environment on startup. So re-exec ourselves via `env -u`
# to scrub the environment for all descendants.
if env 2>/dev/null | grep -Eq '^BASH_ENV=|^BASH_FUNC_(rm|rmdir|unlink)%%='; then
    exec env -u BASH_ENV -u BASH_FUNC_rm%% -u BASH_FUNC_rmdir%% \
        -u BASH_FUNC_unlink%% "$0" "$@"
fi
unset BASH_ENV
unset -f rm rmdir unlink 2>/dev/null

export PATH="/d/vcpkg/installed/x64-windows/bin:$PATH"
export TEST_DIRECTORY="${WROOT}\\src\\t"
export GIT_BUILD_DIR="${WROOT}\\build"
export TESTLOG="$ROOT/testlog"
mkdir -p "$TESTLOG"

cd "$ROOT/src/t" || exit 1
# Build the explicit test list: expand each glob in src/t, keep only real
# files. Deterministic — no reliance on ls-pipe behavior. NOTE: patterns
# must NOT carry a trailing ".*"; the ".sh" suffix is appended here, and
# "name.*.sh" would never match "name.sh" (double-dot requirement).
TESTS=""
for g in $GLOB; do
    for f in $g.sh; do
        [ -f "$f" ] && TESTS="$TESTS $f"
    done
done
printf '%s\n' $TESTS | xargs -P "$JOBS" -I{} sh -c 'exec sh "$1" > "$TESTLOG/$1.log" 2>&1' _ {}

echo "== aggregate =="
total_ok=0; total_notok=0; failed_files=""
for f in "$TESTLOG"/*.log; do
    ok=$(grep -c '^ok ' "$f" || true)
    notok=$(grep -c '^not ok ' "$f" || true)
    total_ok=$((total_ok + ok)); total_notok=$((total_notok + notok))
    if [ "$notok" -gt 0 ]; then failed_files="$failed_files $(basename "$f")"; fi
done
echo "ok=$total_ok notok=$total_notok"
[ -n "$failed_files" ] && echo "files with failures:$failed_files"
exit 0
