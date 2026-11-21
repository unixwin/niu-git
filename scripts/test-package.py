#!/usr/bin/env python3
"""Tests for scripts/package.py (niu-git issue #23).

Builds a synthetic dist tree with the same duplication shape as the real
one (git.exe x5, git-remote-http x4, git-shell x2, perl dashed/.perl pairs,
a tiny identical pair, unique files), packages it, and checks:

  1. the deduped zip is smaller and carries links.json, not the copies;
  2. the manifest picks mingw64/bin/git.exe as the git.exe canonical;
  3. --verify passes (materialised tree byte-identical to the compat zip,
     links really are hardlinks);
  4. --verify fails on a tampered manifest and on a tampered compat tree.

Stdlib only; runs on Linux and Windows: `python scripts/test-package.py`.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "scripts" / "package.py"


def run(*argv, expect=0):
    proc = subprocess.run([sys.executable, str(PACKAGE), *argv],
                          capture_output=True, text=True)
    if proc.returncode != expect:
        raise SystemExit(f"package.py {' '.join(argv)} exited "
                         f"{proc.returncode}, want {expect}:\n"
                         f"{proc.stdout}{proc.stderr}")
    return proc


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fixture(dist):
    git = os.urandom(60000) + b"git-binary-marker"
    remote = os.urandom(30000) + b"remote-marker"
    shell = os.urandom(20000) + b"shell-marker"
    perl_send = b"#!/usr/bin/perl\n# git-send-email\n" + os.urandom(5000)
    perl_svn = b"#!/usr/bin/perl\n# git-svn\n" + os.urandom(4000)
    tiny = b"tiny-mergetool\n"
    # git.exe group x5 (bin/ is the canonical).
    write(dist / "mingw64" / "bin" / "git.exe", git)
    for name in ("git-receive-pack.exe", "git-upload-archive.exe",
                 "git-upload-pack.exe", "git.exe"):
        write(dist / "mingw64" / "libexec" / "git-core" / name, git)
    # git-remote-http group x4 (all libexec; lexicographically first wins).
    for name in ("git-remote-ftp.exe", "git-remote-ftps.exe",
                 "git-remote-http.exe", "git-remote-https.exe"):
        write(dist / "mingw64" / "libexec" / "git-core" / name, remote)
    # git-shell x2.
    write(dist / "mingw64" / "bin" / "git-shell.exe", shell)
    write(dist / "mingw64" / "libexec" / "git-core" / "git-shell.exe", shell)
    # perl dashed/.perl pairs: the dashed name is canonical.
    for script, body in (("git-send-email", perl_send), ("git-svn", perl_svn)):
        write(dist / "mingw64" / "libexec" / "git-core" / script, body)
        write(dist / "mingw64" / "libexec" / "git-core" / f"{script}.perl",
              body)
    # tiny identical pair.
    write(dist / "mingw64" / "libexec" / "git-core" / "mergetools" / "gvimdiff",
          tiny)
    write(dist / "mingw64" / "libexec" / "git-core" / "mergetools" / "nvimdiff",
          tiny)
    # unique files.
    write(dist / "mingw64" / "bin" / "libcurl.dll", os.urandom(40000))
    write(dist / "mingw64" / "libexec" / "git-core" / "git-credential-wincred.exe",
          os.urandom(15000))
    write(dist / "etc" / "gitconfig",
          b"[http]\n\tsslBackend = schannel\n"
          b"[credential]\n\thelper = wincred\n")
    write(dist / "mingw64" / "share" / "git-core" / "templates" / "info" / "exclude",
          b"# exclude\n")
    return {"git": git, "remote": remote, "shell": shell}


def zip_entries(path):
    with zipfile.ZipFile(path) as zf:
        return [i.filename for i in zf.infolist() if not i.is_dir()]


def main():
    with tempfile.TemporaryDirectory(prefix="niugit-test-package-") as tmp:
        tmp = Path(tmp)
        dist = tmp / "dist"
        fixture(dist)
        out = tmp / "out"
        proc = run(str(dist), "--version", "9.9.9.test", "--arch", "x64",
                   "--out-dir", str(out))
        deduped = out / "niu-git-9.9.9.test-x64.zip"
        compat = out / "niu-git-9.9.9.test-x64-compat.zip"
        assert deduped.is_file() and compat.is_file(), "both zips produced"
        assert deduped.stat().st_size < compat.stat().st_size, \
            "deduped zip smaller than compat zip"
        with zipfile.ZipFile(deduped) as zf:
            manifest = json.loads(zf.read("links.json").decode("utf-8"))
            names = {i.filename for i in zf.infolist()}
        assert manifest["format"] == "niugit-links/1", manifest
        assert manifest["compat_archive"] == compat.name, manifest
        links = {l["target"]: l["source"] for l in manifest["links"]}
        # 4 + 3 + 1 + 2 + 1 = 11 linked copies.
        assert len(links) == 11, links
        assert links["mingw64/libexec/git-core/git-receive-pack.exe"] == \
            "mingw64/bin/git.exe", links
        assert links["mingw64/libexec/git-core/git.exe"] == \
            "mingw64/bin/git.exe", links
        assert links["mingw64/libexec/git-core/git-remote-https.exe"] == \
            "mingw64/libexec/git-core/git-remote-ftp.exe", links
        assert links["mingw64/libexec/git-core/git-shell.exe"] == \
            "mingw64/bin/git-shell.exe", links
        assert links["mingw64/libexec/git-core/git-send-email.perl"] == \
            "mingw64/libexec/git-core/git-send-email", links
        assert "mingw64/libexec/git-core/git-receive-pack.exe" not in names
        assert "mingw64/bin/git.exe" in names
        assert "mingw64/libexec/git-core/git-send-email" in names
        assert "links.json" in names
        # Compat zip keeps the old layout: every dist file, no manifest.
        compat_names = zip_entries(compat)
        assert "links.json" not in compat_names, compat_names
        assert len(compat_names) == 21, compat_names
        assert len(zip_entries(deduped)) == 21 - 11 + 1, zip_entries(deduped)
        # --verify passes on the good pair.
        run("--verify", str(deduped), str(compat))
        # --materialize really creates hardlinks (same inode as the source).
        expanded = tmp / "expanded"
        with zipfile.ZipFile(deduped) as zf:
            zf.extractall(expanded)
        run("--materialize", str(expanded))
        target = expanded / "mingw64/libexec/git-core/git-upload-pack.exe"
        source = expanded / "mingw64/bin/git.exe"
        assert target.stat().st_ino == source.stat().st_ino, \
            "materialised target is not a hardlink of its source"
        assert target.read_bytes() == source.read_bytes()
        # --verify fails on a tampered manifest (source that is not stored).
        bad = tmp / "bad.zip"
        with zipfile.ZipFile(deduped) as src, \
                zipfile.ZipFile(bad, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "links.json":
                    payload = json.loads(data)
                    payload["links"][0]["source"] = "mingw64/bin/nope.exe"
                    data = (json.dumps(payload, indent=2) + "\n").encode()
                dst.writestr(item, data)
        run("--verify", str(bad), str(compat), expect=1)
        # --verify fails when the compat tree is tampered.
        bad_compat = tmp / "bad-compat.zip"
        with zipfile.ZipFile(compat) as src, \
                zipfile.ZipFile(bad_compat, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "etc/gitconfig":
                    data = b"[http]\n\tsslBackend = openssl\n"
                dst.writestr(item, data)
        run("--verify", str(deduped), str(bad_compat), expect=1)
    print("test-package: OK (11 links, canonical picks, verify + "
          "tamper cases pass)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
