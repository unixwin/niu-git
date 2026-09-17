#!/bin/sh
# Run upstream git test suite (subset) against the CMake build.
#
# MUST be launched from real Git Bash with MSYS path conversion enabled
# (the WorkBuddy/CodeBuddy host sets MSYS_NO_PATHCONV=1 and
# MSYS2_ARG_CONV_EXCL=* globally, which breaks absolute-path args to the
# native git.exe). Recommended invocation:
#
#   env -u MSYS_NO_PATHCONV -u MSYS2_ARG_CONV_EXCL -u MSYS \
#     "/c/Program Files/Git/bin/bash.exe" scripts/test.sh [glob]
#
# Optional env: JOBS (parallel test files, default 4), GLOB via $1 (default t0*).
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WROOT=$(cygpath -w "$ROOT")
GLOB="${1:-t0*}"
JOBS="${JOBS:-4}"

export PATH="/d/vcpkg/installed/x64-windows/bin:$PATH"
export TEST_DIRECTORY="${WROOT}\\src\\t"
export GIT_BUILD_DIR="${WROOT}\\build"
export TESTLOG="$ROOT/testlog"
mkdir -p "$TESTLOG"

cd "$ROOT/src/t" || exit 1
ls $GLOB.sh | xargs -P "$JOBS" -I{} sh -c 'exec sh "$1" > "$TESTLOG/$1.log" 2>&1' _ {}

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
