#!/bin/sh
# Run upstream test files ONE BY ONE (direct exec, no xargs/pipe) to
# separate product failures from concurrency/harness artifacts.
# Usage: scripts/standalone-triage.sh t1400-update-ref t1800-hook ...
# Results: /d/repo/niu-git/triage-standalone/summary.txt (+ per-file logs)

if env | grep -Eq '^BASH_ENV=|^BASH_FUNC_'; then
    exec env -u BASH_ENV -u BASH_FUNC_rm%% -u BASH_FUNC_rmdir%% \
        -u BASH_FUNC_unlink%% "$0" "$@"
fi
unset BASH_ENV
unset MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL
export MSYS=winsymlinks:nativestrict

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT/src/t" || exit 1
export TEST_DIRECTORY="${ROOT}\\src\\t"
export GIT_BUILD_DIR="${ROOT}\\build"
export PATH="/d/vcpkg/installed/x64-windows/bin:$PATH"

OUT="$ROOT/triage-standalone"
mkdir -p "$OUT"
: > "$OUT/summary.txt"

for t in "$@"; do
    log="$OUT/$t.log"
    timeout 1200 "./$t.sh" > "$log" 2>&1
    rc=$?
    ok=$(grep -c '^ok ' "$log")
    notok=$(grep '^not ok' "$log" | grep -vc 'TODO')
    bail=$(grep -c 'Bail out' "$log")
    echo "$t rc=$rc ok=$ok real_notok=$notok bail=$bail" >> "$OUT/summary.txt"
done
echo ALL_DONE >> "$OUT/summary.txt"
