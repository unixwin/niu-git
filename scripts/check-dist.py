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
The dumpbin-based import checks stay in CI because they need the VS toolchain.
"""
import re
import sys
from pathlib import Path

REQUIRED = (
    "mingw64/bin/git.exe",
    "mingw64/libexec/git-core/git-upload-pack.exe",
    "mingw64/libexec/git-core/git-receive-pack.exe",
    "mingw64/libexec/git-core/git-upload-archive.exe",
    "mingw64/libexec/git-core/git-credential-wincred.exe",
    "etc/gitconfig",
)
TRIMMED_FROM_BIN = ("git-upload-pack.exe", "git-receive-pack.exe",
                    "git-upload-archive.exe")
# Absolute-path-looking strings, then filtered to the ones that can only come
# from a build tree (our own name, or the CI/dev workspace roots).
ABSOLUTE = re.compile(rb"[A-Za-z]:[\\/][ -~]{4,}")
BUILD_PATH_HINTS = ("niu-git", "niugit", "\\repo\\", "/repo/")


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
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

    if failures:
        for line in failures:
            print(f"check-dist: {line}", file=sys.stderr)
        return 1
    print(f"check-dist: {dist} OK "
          f"({len(REQUIRED)} required files, no build-path leakage)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
