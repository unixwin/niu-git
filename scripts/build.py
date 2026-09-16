#!/usr/bin/env python3
"""niu-git build: native Windows git without MSYS (MSVC + CMake + vcpkg).

Repo layout rules:
- Relative paths only; no hardcoded developer-absolute runner paths.
- Upstream source is pulled as a tarball and extracted into src/ (gitignored).
- Build output lands in build/, distributable layout in dist/.
"""
import argparse
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_TAG = "v2.55.0.windows.2"
TARBALL = ROOT / f"git-src-{GIT_TAG}.tar.gz"
SRC = ROOT / "src"
BUILD = ROOT / "build"
DIST = ROOT / "dist"

# PoC links against the classic-mode vcpkg install (zlib/curl already built).
# Later: manifest mode with curl[schannel] + static triplets.
VCPKG_INSTALLED = Path("D:/vcpkg/installed/x64-windows")


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def prepare():
    if TARBALL.exists() and TARBALL.stat().st_size < 1_000_000:
        TARBALL.unlink()
    if not TARBALL.exists():
        url = (
            "https://codeload.github.com/git-for-windows/git/tar.gz/"
            f"refs/tags/{GIT_TAG}"
        )
        print(f"downloading {url}")
        run([sys.executable, "-c",
             f"import urllib.request; urllib.request.urlretrieve({url!r}, {str(TARBALL)!r})"])
    if SRC.exists():
        shutil.rmtree(SRC)
    SRC.mkdir(parents=True)
    print(f"extracting {TARBALL.name} -> {SRC}")
    with tarfile.open(TARBALL) as t:
        t.extractall(SRC, filter="tar")
    inner = next(p for p in SRC.iterdir() if p.is_dir())
    for p in inner.iterdir():
        p.rename(SRC / p.name)
    inner.rmdir()


def configure():
    BUILD.mkdir(exist_ok=True)
    cmd = [
        "cmake", "-S", SRC, "-B", BUILD,
        "-DNO_VCPKG=TRUE",
        f"-DCMAKE_PREFIX_PATH={VCPKG_INSTALLED.as_posix()}",
        "-DCMAKE_INSTALL_PREFIX=" + (DIST / "git").as_posix(),
    ]
    run(cmd)


def build():
    run(["cmake", "--build", BUILD, "--config", "Release"])


def collect():
    run(["cmake", "--install", BUILD, "--config", "Release"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true", help="re-download/extract source")
    ap.add_argument("--skip-configure", action="store_true")
    args = ap.parse_args()

    if args.prepare or not SRC.exists():
        prepare()
    if not args.skip_configure:
        configure()
    build()
    collect()
    print("done.")


if __name__ == "__main__":
    main()
