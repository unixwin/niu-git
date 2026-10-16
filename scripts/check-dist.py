#!/usr/bin/env python3
"""Release-artifact checks that do not need the compiler toolchain.

  python scripts/check-dist.py [dist-dir]

Fails (exit 1) when:
  1. a required product file is missing from the layout;
  2. bin/ still carries the dashed server forms that the product trims
     (libexec/git-core is where git resolves them);
  3. a shipped binary embeds a build-directory path. __FILE__ (used by
     die()/BUG()) is compiled through /pathmap, so a release must not contain
     strings such as "D:\\repo\\niu-git\\src\\http.c": they leak the build
     layout to users, differ between machines and break reproducibility.
The static-link check needs dumpbin: pass --require-dumpbin in CI (a missing
dumpbin then fails the build instead of skipping the check).
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REQUIRED = (
    "mingw64/bin/git.exe",
    "mingw64/libexec/git-core/git-upload-pack.exe",
    "mingw64/libexec/git-core/git-receive-pack.exe",
    "mingw64/libexec/git-core/git-upload-archive.exe",
    "mingw64/libexec/git-core/git-credential-wincred.exe",
    # The perl commands must exist under their dashed name (the .perl file
    # alone is not what git looks up); perl itself is optional.
    "mingw64/libexec/git-core/git-send-email",
    "mingw64/libexec/git-core/git-svn",
    "etc/gitconfig",
)
TRIMMED_FROM_BIN = ("git-upload-pack.exe", "git-receive-pack.exe",
                    "git-upload-archive.exe")
# Absolute-path-looking strings, then filtered to the ones that can only come
# from a build tree (our own name, or the CI/dev workspace roots).
ABSOLUTE = re.compile(rb"[A-Za-z]:[\\/][ -~]{4,}")
BUILD_PATH_HINTS = ("niu-git", "niugit", "\\repo\\", "/repo/")
# zlib and iconv are linked statically into git.exe; a dynamic leak shows up
# as these names in its import table.
STATIC_ONLY_DLLS = ("zlib1.dll", "iconv-2.dll")


def find_dumpbin():
    override = os.environ.get("DUMPBIN")
    if override and Path(override).is_file():
        return Path(override)
    for base in (os.environ.get("ProgramFiles"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        if not base:
            continue
        hits = sorted(Path(base).glob(
            "Microsoft Visual Studio/*/*/VC/Tools/MSVC/*/bin/Hostx64/x64/dumpbin.exe"))
        if hits:
            return hits[-1]
    return None


def check_imports(dist, required, failures):
    dumpbin = find_dumpbin()
    git_exe = dist / "mingw64" / "bin" / "git.exe"
    if dumpbin is None:
        note = "dumpbin not found; static-link check skipped"
        if required:
            failures.append(note)
        else:
            print(f"check-dist: {note}")
        return
    result = subprocess.run([str(dumpbin), "/dependents", str(git_exe)],
                            capture_output=True, text=True)
    imports = (result.stdout + result.stderr).lower()
    for dll in STATIC_ONLY_DLLS:
        if dll in imports:
            failures.append(f"git.exe still imports {dll}")
    print(f"check-dist: git.exe imports no {'/'.join(STATIC_ONLY_DLLS)} "
          "(static link OK)")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    required = "--require-dumpbin" in sys.argv[1:]
    dist = Path(args[0] if args else "dist")
    if not dist.is_dir():
        print(f"check-dist: {dist} is not a directory", file=sys.stderr)
        return 1
    failures = []

    for relative in REQUIRED:
        if not (dist / relative).is_file():
            failures.append(f"missing {dist / relative}")

    for name in TRIMMED_FROM_BIN:
        if (dist / "mingw64" / "bin" / name).exists():
            failures.append(f"bin/{name} should have been trimmed")

    leaked = {}
    for binary in dist.rglob("*.exe"):
        data = binary.read_bytes()
        hits = set()
        for match in ABSOLUTE.finditer(data):
            text = match.group().decode("latin1")
            if any(hint in text.lower() for hint in BUILD_PATH_HINTS):
                hits.add(text)
        if hits:
            leaked[binary.relative_to(dist).as_posix()] = sorted(hits)

    if leaked:
        failures.append("binaries embed build-directory paths (/pathmap missing?)")
        for binary, hits in list(leaked.items())[:4]:
            for text in hits[:3]:
                failures.append(f"    {binary}: {text}")

    if dist.joinpath("mingw64", "bin", "git.exe").is_file():
        check_imports(dist, required, failures)

    if failures:
        for line in failures:
            print(f"check-dist: {line}", file=sys.stderr)
        return 1
    print(f"check-dist: {dist} OK "
          f"({len(REQUIRED)} required files, no build-path leakage)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
