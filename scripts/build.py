#!/usr/bin/env python3
"""niu-git build: native Windows git without MSYS (MSVC + CMake + vcpkg).

Repo layout rules:
- Relative paths only; no hardcoded developer-absolute runner paths.
- Upstream source is pulled as a tarball and extracted into src/ (gitignored).
- Build output lands in build/, distributable layout in dist/.
"""
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GIT_TAG = "v2.56.0.windows.2"
# Byte size and sha256 of the codeload tarball for GIT_TAG. A truncated
# download still extracts a usable-looking tree, so the patch step used to
# fail with a baffling context mismatch ("patch does not apply") instead of
# "the download was short" - seen in CI on 2026-10-16. prepare() verifies and
# retries, and refuses to build from a mismatch.
GIT_TARBALL_SIZE = 12870087
GIT_TARBALL_SHA256 = "1b65aabab5892a2b2758aca64a2fda0a290b06e0eeab8116a9835cb712010f65"

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
ARCH_NAME = "x64"


def use_arch(arch):
    """Retarget build/dist/vcpkg triplet directories for x64 or arm64."""
    global BUILD, DIST, VCPKG_INSTALLED, VCPKG_STATIC_INSTALLED, ARCH_FLAG, ARCH_NAME
    ARCH_NAME = arch
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


def tarball_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tarball_is_complete(path):
    if not path.exists():
        return False
    if path.stat().st_size != GIT_TARBALL_SIZE:
        return False
    return tarball_digest(path) == GIT_TARBALL_SHA256


def prepare():
    attempts = 3
    for attempt in range(1, attempts + 1):
        if tarball_is_complete(TARBALL):
            break
        if TARBALL.exists():
            print(f"{TARBALL.name} is incomplete or stale; re-downloading")
            TARBALL.unlink()
        url = (
            "https://codeload.github.com/git-for-windows/git/tar.gz/"
            f"refs/tags/{GIT_TAG}"
        )
        print(f"downloading {url} (attempt {attempt}/{attempts})")
        run([sys.executable, "-c",
             f"import urllib.request; urllib.request.urlretrieve({url!r}, {str(TARBALL)!r})"])
    if not tarball_is_complete(TARBALL):
        actual = TARBALL.stat().st_size if TARBALL.exists() else 0
        raise SystemExit(
            f"{TARBALL.name} is not the expected archive "
            f"(size {actual}, want {GIT_TARBALL_SIZE}; sha256 "
            f"{tarball_digest(TARBALL) if TARBALL.exists() else 'n/a'}); "
            "re-run to retry the download")
    if SRC.exists():
        shutil.rmtree(SRC)
    SRC.mkdir(parents=True)
    print(f"extracting {TARBALL.name} -> {SRC}")
    try:
        with tarfile.open(TARBALL) as t:
            t.extractall(SRC, filter="tar")
    except (OSError, tarfile.TarError) as exc:
        # A Windows host without symlink privileges cannot materialise the
        # symlinks the upstream tree carries (RelNotes, subprojects/git-gui,
        # subprojects/gitk). None of them are build inputs, so extract what
        # we can instead of aborting a byte-verified archive.
        print(f"  note: {exc}; retrying member by member")
        if SRC.exists():
            shutil.rmtree(SRC)
        SRC.mkdir(parents=True)
        with tarfile.open(TARBALL) as t:
            for member in t:
                try:
                    t.extract(member, SRC, filter="tar")
                except (OSError, tarfile.TarError):
                    continue
    inner = next(p for p in SRC.iterdir() if p.is_dir())
    for p in inner.iterdir():
        p.rename(SRC / p.name)
    inner.rmdir()
    # Sentinels the patch series and the build depend on.
    for sentinel in ("http.c", "exec-cmd.c", "compat/mingw.c",
                     "contrib/buildsystems/CMakeLists.txt"):
        target = SRC / sentinel
        if not target.is_file():
            raise SystemExit(f"extracted tree is missing {sentinel}; download or "
                             "extraction is incomplete")



def normalized_patch(path, scratch):
    """Return a patch path git apply can consume whatever the checkout did.

    A checkout with core.autocrlf=true (the GitHub Windows runners) rewrites
    patches/*.patch with CRLF line endings; git apply then fails to match the
    LF sources extracted from the upstream tarball, reporting a context
    mismatch far away from the real cause. .gitattributes pins eol=lf, and this
    is the belt for a mangled checkout (a developer on a foreign setup, an
    archive download, ...).
    """
    data = path.read_bytes()
    if b"\r\n" not in data:
        return path
    fixed = scratch / path.name
    fixed.write_bytes(data.replace(b"\r\n", b"\n"))
    print(f"  note: {path.name} carries CRLF line endings; using a normalized copy")
    return fixed


def apply_patches():
    """Apply patches/*.patch to src/ (idempotent: skips applied patches)."""
    patches = sorted((ROOT / "patches").glob("*.patch"))
    if not patches:
        return
    sh = shutil.which("sh")
    git = shutil.which("git")
    rel_src = os.path.relpath(SRC, ROOT)
    scratch = Path(tempfile.mkdtemp(prefix="niugit-patches-"))
    for p in patches:
        patch = normalized_patch(p, scratch)
        print(f"applying {p.name}")
        # git apply is the primary applier: winuxcmd's patch(1) 1.1.8 does
        # not strip the a/ b/ prefixes under -p1, so it only works for
        # already-extracted flat paths. Detect the already-applied case via
        # a reverse check so re-runs stay idempotent.
        if git:
            proc = subprocess.run(
                [git, "apply", "--check", "-p1", f"--directory={rel_src}", str(patch)],
                capture_output=True, text=True, cwd=ROOT)
            if proc.returncode == 0:
                run([git, "apply", "-p1", f"--directory={rel_src}", str(patch)],
                    cwd=ROOT)
                continue
            rev = subprocess.run(
                [git, "apply", "--reverse", "--check", "-p1",
                 f"--directory={rel_src}", str(patch)],
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
                 f"patch -d '{SRC.as_posix()}' -p1 -N -r - < '{patch.as_posix()}'"],
                capture_output=True, text=True)
            output = proc.stdout + proc.stderr
            if proc.returncode == 0 or "Skipping patch" in output:
                continue
        print(output, file=sys.stderr)
        raise SystemExit(f"patch failed: {p.name}")


def find_vcvars():
    """Locate vcvarsall.bat (VS ships it; needed to compile the credential
    helper outside the CMake build)."""
    override = os.environ.get("VCVARS")
    if override and Path(override).exists():
        return Path(override)
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / \
        "Microsoft Visual Studio/Installer/vswhere.exe"
    if vswhere.is_file():
        result = subprocess.run(
            [str(vswhere), "-latest", "-products", "*",
             "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
             "-find", r"VC\Auxiliary\Build\vcvarsall.bat"],
            capture_output=True, text=True)
        first = result.stdout.strip().splitlines()
        if first and Path(first[0]).is_file():
            return Path(first[0])
    for pattern in ("*/Community/VC/Auxiliary/Build/vcvarsall.bat",
                    "*/BuildTools/VC/Auxiliary/Build/vcvarsall.bat",
                    "*/Enterprise/VC/Auxiliary/Build/vcvarsall.bat",
                    "*/Professional/VC/Auxiliary/Build/vcvarsall.bat"):
        found = sorted(Path("C:/Program Files/Microsoft Visual Studio").glob(pattern))
        if found:
            return found[-1]
    return None


def build_wincred():
    """Ship git's own Windows Credential Manager helper.

    Git for Windows answers credentials from its *system* gitconfig
    (credential.helper=manager, and that file is invisible to us), so a user
    whose only helper lived there is prompted at the terminal after migrating.
    wincred is git's own helper - one C file, no perl, no MSYS - and it reads
    the git:<protocol>://<host> entries already in the Windows Credential
    Manager (the same target names Git Credential Manager writes), so it is a
    zero-download replacement for that case.

    It is shipped but not made the system default on purpose: git consults
    helpers system -> global -> local, so a system-level default would answer
    before a helper the user configured (host-scoped credential.<url>.helper,
    GCM, `gh auth git-credential`, ...). Users with a helper keep exactly the
    behavior they had; the documented recipe and the niu setup wizard cover
    the rest.
    """
    source = SRC / "contrib" / "credential" / "wincred" / "git-credential-wincred.c"
    if not source.is_file():
        print("wincred source not in the tree; skipping the credential helper")
        return
    vcvars = find_vcvars()
    if vcvars is None:
        print("vcvarsall.bat not found; skipping the credential helper")
        return
    target = DIST / "mingw64" / "libexec" / "git-core" / "git-credential-wincred.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    objects = BUILD / "wincred"
    objects.mkdir(parents=True, exist_ok=True)
    vcvars_arg = "x64_arm64" if ARCH_NAME == "arm64" else "x64"
    script = ROOT / f"build-wincred-{ARCH_NAME}.bat"
    script.write_text(
        "@echo off\r\n"
        f'call "{vcvars}" {vcvars_arg} >nul 2>&1\r\n'
        # An explicit .obj path: "/Fo<dir>\" ends in a backslash before the
        # closing quote, which the MSVC argument parser reads as an escaped
        # quote and then swallows the source path ("missing source file
        # name").
        f'cl /nologo /O2 /DWIN32 /Fo"{objects}\\git-credential-wincred.obj" '
        f'"{source}" advapi32.lib /Fe:"{target}"\r\n'
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8", newline="")
    try:
        run(["cmd", "/c", str(script)])
    finally:
        script.unlink(missing_ok=True)
    if not target.is_file():
        raise SystemExit("git-credential-wincred.exe was not produced")
    print("credential helper ->", target.relative_to(ROOT))


def stamp_version():
    """Give the build a version string that names this distribution.

    GIT-VERSION-GEN prefers a `version` file in the source root (what Git for
    Windows ships in its release tarballs); ours has none, so the build fell
    back to upstream's hardcoded DEF_VER and `git --version` printed a bare
    "2.55.0" - no .windows.N suffix and nothing identifying niu-git in a bug
    report. Writing the file before configure() sets the version compiled into
    git, `git version --build-options`, GIT_USER_AGENT and the docs links.
    """
    upstream = GIT_TAG[1:] if GIT_TAG.startswith("v") else GIT_TAG
    version = os.environ.get("NIUGIT_BUILD_VERSION", f"{upstream}.niugit")
    (SRC / "version").write_text(version + "\n", encoding="ascii", newline="\n")
    print(f"version stamp -> {version}")


def configure():
    BUILD.mkdir(exist_ok=True)
    # Static zlib/iconv first in the search path (see VCPKG_STATIC_INSTALLED);
    # curl/EXPAT resolve from the dynamic triplet.
    prefix_path = os.pathsep.join(
        [VCPKG_STATIC_INSTALLED.as_posix(), VCPKG_INSTALLED.as_posix()])
    # Reproducible, path-clean binaries:
    #   /pathmap  rewrites the absolute source root baked into __FILE__ by
    #             die()/BUG() (248 strings like "D:/repo/niu-git/src/http.c",
    #             which leak the build layout to users and change the binary
    #             from one build directory to the next);
    #   /Brepro   drops the build-time PE timestamp so the same inputs produce
    #             the same bytes (what makes a pinned sha256 verifiable).
    source_root = SRC.resolve().as_posix()
    # /pathmap needs /experimental:deterministic (otherwise cl warns D9007 and
    # ignores the mapping); /Brepro on the link drops the wall-clock PE
    # timestamp. Verified with a probe: __FILE__ becomes "src\probe.c" and the
    # timestamp becomes a content checksum instead of the build date.
    repro_flags = f"/experimental:deterministic /pathmap:{source_root}=src /Brepro"
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
        f"-DCMAKE_C_FLAGS={repro_flags}",
        f"-DCMAKE_EXE_LINKER_FLAGS=/Brepro",
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


def dumpbin_path():
    """Locate dumpbin (VS ships it; CI does not put it on PATH)."""
    override = os.environ.get("DUMPBIN")
    if override and Path(override).exists():
        return Path(override)
    roots = [
        Path("C:/Program Files/Microsoft Visual Studio"),
        Path("C:/Program Files (x86)/Microsoft Visual Studio"),
    ]
    patterns = [
        "*/Community/VC/Tools/MSVC/*/bin/Hostx64/x64/dumpbin.exe",
        "*/BuildTools/VC/Tools/MSVC/*/bin/Hostx64/x64/dumpbin.exe",
        "*/Enterprise/VC/Tools/MSVC/*/bin/Hostx64/x64/dumpbin.exe",
        "*/Professional/VC/Tools/MSVC/*/bin/Hostx64/x64/dumpbin.exe",
    ]
    for root in roots:
        for pattern in patterns:
            found = sorted(root.glob(pattern))
            if found:
                return found[-1]
    raise SystemExit("dumpbin.exe not found; set the DUMPBIN environment variable")


def imported_dlls(binary, dumpbin):
    result = subprocess.run([str(dumpbin), "/dependents", str(binary)],
                            capture_output=True, text=True)
    names = []
    for line in result.stdout.splitlines():
        name = line.strip()
        if name.lower().endswith(".dll") and " " not in name:
            names.append(name)
    return names


def collect_runtime_dlls():
    """Copy the non-system DLL closure of the installed binaries.

    Only DLLs that vcpkg itself provides are considered, so no system-DLL
    allow-list has to be maintained and a vcpkg port that starts (or stops)
    depending on something is followed automatically.
    """
    dumpbin = dumpbin_path()
    provided = {}
    for root in (VCPKG_INSTALLED / "bin", VCPKG_STATIC_INSTALLED / "bin"):
        if root.is_dir():
            for dll in root.glob("*.dll"):
                provided[dll.name.lower()] = dll

    bin_dirs = [DIST / "mingw64" / "bin", DIST / "mingw64" / "libexec" / "git-core"]
    target = DIST / "mingw64" / "libexec" / "git-core"
    wanted = {}
    pending = [p for d in bin_dirs for p in d.iterdir()
               if p.suffix.lower() in (".exe", ".dll")]
    visited = set()
    while pending:
        binary = pending.pop()
        if binary.name.lower() in visited:
            continue
        visited.add(binary.name.lower())
        for name in imported_dlls(binary, dumpbin):
            key = name.lower()
            source = provided.get(key)
            if source is None or key in wanted:
                continue
            wanted[key] = source
            pending.append(source)

    for directory in bin_dirs + [target]:
        for stale in directory.glob("*.dll"):
            if stale.name.lower() not in wanted:
                stale.unlink()
    for source in sorted(wanted.values(), key=lambda p: p.name.lower()):
        shutil.copy2(source, target / source.name)
    print("runtime DLLs ->", ", ".join(sorted(wanted)) or "(none)")


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
    # Runtime DLLs: ship exactly the closure of what the installed binaries
    # import and vcpkg provides (everything else is a system DLL). The previous
    # hardcoded list (zlib1.dll, libcurl.dll) coupled the artifact to one
    # vcpkg curl port revision - a revision that grows a dependency (brotli,
    # zstd, ...) would ship a broken curl, and a developer machine hides it by
    # resolving the missing DLL from its own vcpkg bin directory.
    collect_runtime_dlls()
    # git resolves the perl-script commands by their dashed, extension-less
    # name (git-send-email), while the CMake install only drops the .perl
    # form. Publishing the dashed copy makes the command exist and explain
    # itself ("perl: not found") instead of "not a git command", and it starts
    # working the moment a perl lands on PATH (an optional `wpm install perl`
    # package is the intended route; nothing perl is bundled).
    for script in ("git-send-email", "git-svn", "git-cvsimport",
                   "git-cvsexportcommit", "git-cvsserver", "git-archimport"):
        source = DIST / "mingw64" / "libexec" / "git-core" / f"{script}.perl"
        if source.is_file():
            shutil.copy2(source, source.with_suffix(""))
    # The bundled curl supports schannel only; make it the default so plain
    # `git clone https://...` works out of the box.
    #
    # Deliberately no credential.helper here. git consults helpers in
    # system -> global -> local order, so a system-level default would run
    # *before* a helper the user configured themselves (a host-scoped
    # credential.<url>.helper, Git Credential Manager, `gh auth
    # git-credential`, ...); if it happened to hold a stale entry for that
    # host it would answer first. The helper binary ships anyway
    # (build_wincred below), so `git config --global credential.helper
    # wincred` is a zero-download option and the niu setup wizard can offer it
    # once instead of us changing someone's credential behavior silently.
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
    stamp_version()
    if not args.skip_configure:
        configure()
    build()
    link_server_dashed()
    install_perl_shim()
    build_wincred()
    collect()
    print("done.")


if __name__ == "__main__":
    main()
