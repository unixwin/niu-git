#!/usr/bin/env python3
"""niu-git build: native Windows git without MSYS (MSVC + CMake + vcpkg).

Repo layout rules:
- Relative paths only; no hardcoded developer-absolute runner paths.
- Upstream source is pulled as a tarball and extracted into src/ (gitignored).
- Build output lands in build/, distributable layout in dist/.
"""
import argparse
import os
import re
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
VCPKG_ROOT = Path(os.environ.get("NIUGIT_VCPKG_ROOT", "D:/vcpkg"))
VCPKG_INSTALLED = VCPKG_ROOT / "installed" / "x64-windows"
# Static triplet searched first: ZLIB (zs.lib) and Iconv (iconv.lib) resolve
# here, while curl/EXPAT exist only in x64-windows and stay dynamic. Both
# triplets are /MD so the CRT linkage matches. This drops zlib1.dll and
# iconv-2.dll from git.exe's import table, which is what lets wpm place the
# bare git.exe in the shared <root>/usr/bin with no DLL search-path setup.
VCPKG_STATIC_INSTALLED = VCPKG_ROOT / "installed" / "x64-windows-static-md"
ARCH_FLAG = []


def use_arch(arch):
    """Retarget build/dist/vcpkg triplet directories for x64 or arm64."""
    global BUILD, DIST, VCPKG_INSTALLED, VCPKG_STATIC_INSTALLED, ARCH_FLAG
    if arch == "x64":
        return
    BUILD = ROOT / f"build-{arch}"
    DIST = ROOT / f"dist-{arch}"
    VCPKG_INSTALLED = VCPKG_ROOT / "installed" / f"{arch}-windows"
    VCPKG_STATIC_INSTALLED = VCPKG_ROOT / "installed" / f"{arch}-windows-static-md"
    ARCH_FLAG = ["-A", arch.upper()]


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


def apply_patches():
    """Apply patches/*.patch to src/ (idempotent: skips applied patches)."""
    patches = sorted((ROOT / "patches").glob("*.patch"))
    if not patches:
        return
    sh = shutil.which("sh")
    git = shutil.which("git")
    rel_src = os.path.relpath(SRC, ROOT)
    for p in patches:
        print(f"applying {p.name}")
        # git apply is the primary applier: winuxcmd's patch(1) 1.1.8 does
        # not strip the a/ b/ prefixes under -p1, so it only works for
        # already-extracted flat paths. Detect the already-applied case via
        # a reverse check so re-runs stay idempotent.
        if git:
            proc = subprocess.run(
                [git, "apply", "--check", "-p1", f"--directory={rel_src}", str(p)],
                capture_output=True, text=True, cwd=ROOT)
            if proc.returncode == 0:
                run([git, "apply", "-p1", f"--directory={rel_src}", str(p)],
                    cwd=ROOT)
                continue
            rev = subprocess.run(
                [git, "apply", "--reverse", "--check", "-p1",
                 f"--directory={rel_src}", str(p)],
                capture_output=True, text=True, cwd=ROOT)
            if rev.returncode == 0:
                print("  already applied, skipping")
                continue
            output = proc.stdout + proc.stderr + rev.stdout + rev.stderr
        else:
            # `patch -N` exits 1 when the patch is already applied (skip);
            # treat that as success, anything else is a real failure.
            proc = subprocess.run(
                [sh, "-c",
                 f"patch -d '{SRC.as_posix()}' -p1 -N -r - < '{p.as_posix()}'"],
                capture_output=True, text=True)
            output = proc.stdout + proc.stderr
            if proc.returncode == 0 or "Skipping patch" in output:
                continue
        print(output, file=sys.stderr)
        raise SystemExit(f"patch failed: {p.name}")


def configure():
    BUILD.mkdir(exist_ok=True)
    # Static zlib/iconv first in the search path (see VCPKG_STATIC_INSTALLED);
    # curl/EXPAT resolve from the dynamic triplet.
    prefix_path = os.pathsep.join(
        [VCPKG_STATIC_INSTALLED.as_posix(), VCPKG_INSTALLED.as_posix()])
    cmd = [
        # NOTE: upstream docs mention -DNO_VCPKG=TRUE but the code never reads
        # it (docs bug, PR candidate); the real switch is USE_VCPKG.
        "cmake", "-S", SRC / "contrib" / "buildsystems", "-B", BUILD,
        *ARCH_FLAG,
        # Drop cached find results: switching triplets must re-resolve
        # zlib/iconv, otherwise a stale x64-windows entry wins over the
        # static-md one and the import table keeps zlib1.dll/iconv-2.dll.
        "-UZLIB*", "-UIconv*",
        "-DUSE_VCPKG=OFF",
        "-DSKIP_DASHED_BUILT_INS=ON",
        "-DBUILD_TESTING=OFF",
        "-DZLIB_USE_STATIC_LIBS=ON",
        f"-DCMAKE_PREFIX_PATH={prefix_path}",
        "-DCMAKE_INSTALL_PREFIX=" + (DIST / "mingw64").as_posix(),  # exe at <root>/mingw64/bin -> system config at <root>/etc (MinGit layout)
    ]
    run(cmd)


def build():
    run(["cmake", "--build", BUILD, "--config", "Release"])


# Server-side dashed forms (git-upload-pack etc.). Upstream Makefile marks
# them "special": they must exist even though they are hardlinks of git.exe,
# because remote helpers / bin-wrappers invoke them by dashed name. CMake's
# install stage creates them unconditionally (bin_links, not gated by
# SKIP_DASHED_BUILT_INS) so the product bundle is fine, but the *build tree*
# only gets them via the git-links target, which SKIP_DASHED_BUILT_INS=ON
# skips — breaking every test that clones over local/file transport
# ("build/git-upload-pack.exe: No such file or directory"). Recreate the
# three in the build tree so bin-wrappers work; the dist stays lean.
SERVER_DASHED = ["git-upload-pack", "git-receive-pack", "git-upload-archive"]


def link_server_dashed():
    git_exe = BUILD / "git.exe"
    for name in SERVER_DASHED:
        target = BUILD / f"{name}.exe"
        if not target.exists():
            os.link(git_exe, target)


def install_perl_shim():
    # CMake hardcodes PERL_PATH=/usr/bin/perl (MSYS perl), which does not
    # exist on niu-git build machines. Ship scripts/t-perl-shim/perl (maps
    # MSYS paths for Strawberry perl) into build/ and point
    # GIT-BUILD-OPTIONS at it, or every PERL_TEST_HELPERS test dies.
    opts = BUILD / "GIT-BUILD-OPTIONS"
    if not opts.exists():
        return
    text = opts.read_text()
    if "t-perl-shim" in text:
        return
    shim_dst = BUILD / "t-perl-shim" / "perl"
    shim_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "scripts" / "t-perl-shim" / "perl", shim_dst)
    opts.write_text(re.sub(r"PERL_PATH=.*",
                           f"PERL_PATH='{shim_dst.as_posix()}'", text))
    print("installed t-perl-shim, PERL_PATH ->", shim_dst)


# perl-gen custom commands under msbuild silently fail (cwd/env mismatch inside
# generate-perl.sh); regenerate with absolute paths before install. These
# scripts get dropped from the final bundle anyway (NO_PERL product decision).
PERL_SCRIPTS = [
    "git-archimport", "git-cvsexportcommit", "git-cvsimport",
    "git-cvsserver", "git-send-email", "git-svn",
]


def gen_perl():
    sh = shutil.which("sh")
    for name in PERL_SCRIPTS:
        run([sh, (SRC / "tools/generate-perl.sh").as_posix(),
             (BUILD / "GIT-BUILD-OPTIONS").as_posix(),
             (BUILD / "GIT-VERSION-FILE").as_posix(),
             (BUILD / "GIT-PERL-HEADER").as_posix(),
             (SRC / f"{name}.perl").as_posix(),
             (BUILD / f"{name}.perl").as_posix()])


# Root entry launcher: wpm's shim layout forwards "git" to the package root,
# and a bare git.exe copy there breaks RUNTIME_PREFIX discovery. See
# scripts/entry/launch-git.c. Built with a dedicated CMake project so the
# entry gets a clean /MT static build (zero deps, like git itself).
def build_entry():
    entry_build = ROOT / "build-entry"
    run(["cmake", "-S", ROOT / "scripts" / "entry", "-B", entry_build])
    run(["cmake", "--build", entry_build, "--config", "Release"])
    exe = entry_build / "Release" / "git.exe"
    if not exe.exists():
        raise SystemExit(f"entry build produced nothing: {exe}")
    shutil.copy2(exe, DIST / "git.exe")
    print("entry launcher ->", DIST / "git.exe")


def link_dist_dashed():
    """git's setup_path() puts exec-path (libexec/git-core) on the PATH of
    spawned commands, and local clone/push invoke git-upload-pack and
    friends through a shell. The dashed server forms must therefore live in
    libexec/git-core itself, not only in mingw64/bin."""
    bin_git = DIST / "mingw64" / "bin" / "git.exe"
    core = DIST / "mingw64" / "libexec" / "git-core"
    for name in SERVER_DASHED:
        dst = core / f"{name}.exe"
        if dst.exists():
            dst.unlink()
        os.link(bin_git, dst)
    print("dashed server forms ->", core)


def collect():
    gen_perl()
    run(["cmake", "--install", BUILD, "--config", "Release"])
    # The wpm "flat" layout puts the real git.exe at the shared <root>/usr/bin
    # (see wpm-source niugit entry), so the old shim-layout root launcher is
    # not shipped anymore; remove any stale copy from earlier builds.
    launcher = DIST / "git.exe"
    if launcher.exists():
        launcher.unlink()
        print("removed stale root launcher", launcher)
    link_dist_dashed()
    # Drop the dashed forms from bin/ once libexec has them: in the wpm flat
    # layout mingw64/bin is not on PATH (only usr/bin/git.exe is), and git
    # always resolves them via setup_path's exec-path prepend anyway - the
    # old launcher's private PATH prepend was the only thing that ever read
    # them from bin/. They are hardlinks of the 5 MB git.exe, and a zip
    # explodes hardlinks, so shipping both copies inflates the artifact by
    # ~15 MB for no reachable code path.
    for name in SERVER_DASHED:
        p = DIST / "mingw64" / "bin" / f"{name}.exe"
        if p.exists():
            p.unlink()
    # Runtime DLLs. The vcpkg libcurl here is the schannel build: TLS goes
    # through Windows crypt32/bcrypt, no openssl shipped. zlib/iconv are
    # linked statically into the git binaries, but libcurl.dll still imports
    # zlib1.dll, so ship the remaining DLLs next to it (libexec/git-core).
    for stale in ("iconv-2.dll", "zlib1.dll", "libcurl.dll"):
        # drop copies from earlier dynamic builds (also in bin/)
        for sub in ("bin", "libexec/git-core"):
            p = DIST / "mingw64" / sub / stale
            if p.exists():
                p.unlink()
    for dll in ("zlib1.dll", "libcurl.dll"):
        src = VCPKG_INSTALLED / "bin" / dll
        shutil.copy2(src, DIST / "mingw64" / "libexec" / "git-core" / dll)
    # The bundled curl supports schannel only; make it the default so plain
    # `git clone https://...` works out of the box.
    etc = DIST / "etc"
    etc.mkdir(parents=True, exist_ok=True)
    (etc / "gitconfig").write_text(
        "[http]\n\tsslBackend = schannel\n", encoding="utf-8", newline="\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true", help="re-download/extract source")
    ap.add_argument("--skip-configure", action="store_true")
    ap.add_argument("--entry-only", action="store_true",
                    help="only build the root entry launcher into dist/")
    ap.add_argument("--arch", choices=["x64", "arm64"], default="x64")
    args = ap.parse_args()

    use_arch(args.arch)
    if args.entry_only:
        build_entry()
        print("done (entry-only).")
        return
    if args.prepare or not SRC.exists():
        prepare()
    apply_patches()
    if not args.skip_configure:
        configure()
    build()
    link_server_dashed()
    install_perl_shim()
    collect()
    print("done.")


if __name__ == "__main__":
    main()
