#!/usr/bin/env python3
"""niu-git release packaging: deduped archive + links manifest + compat archive.

Why this exists (issue #23): the release zip is half duplicate bytes. The
dist tree carries hardlinks (upstream libexec is hardlinked: git.exe appears
as mingw64/bin/git.exe plus the dashed server forms and libexec/git-core/
git.exe; git-remote-http is one binary under four names; git-shell twice;
the perl commands exist as both `git-send-email` and `git-send-email.perl`),
and a zip explodes every hardlink into a physical copy. Measured on the
2.56.0.windows.4 asset (sha256 fc700ae9..., 34.8 MB, 111 files): 10 groups
of byte-identical files, 16.9 MB of compressed duplicate payload.

What this script produces, from a dist/ tree (see scripts/build.py):

  niu-git-<version>-<arch>.zip          deduped archive: one physical entry
                                        per unique content, plus links.json
                                        at the zip root listing the removed
                                        copies as target -> source pairs.
  niu-git-<version>-<arch>-compat.zip   fully expanded archive: the exact
                                        layout the old Compress-Archive step
                                        produced, for consumers that cannot
                                        consume links.json yet.

The deduped archive is the release asset of record; the compat archive is
the fallback until the consumer side lands (wpm links support,
unixwin/WinuxCmd#1835). Until then the wpm-source index must keep pointing
at the -compat.zip. A consumer that plain-unzips the deduped archive still
gets a working git: the canonical copy of each group is chosen so the
primary lookup paths survive (mingw64/bin/git.exe is canonical for the
git.exe group), only the alternate names are links.

Modes:
  python scripts/package.py <dist> --version V --arch A [--out-dir D]
      build both zips (default out-dir is <dist>, as the old CI step did).
  python scripts/package.py --verify <deduped.zip> <compat.zip>
      expand the deduped archive, materialise its links as real hardlinks,
      and require the resulting tree to be byte-identical to the compat
      archive. Also requires the deduped archive to carry no duplicate
      content group. Exit 1 on any mismatch.
  python scripts/package.py --materialize <dir>
      read links.json in <dir> and materialise each target as a hardlink
      of its source (falls back to a copy, with a warning, where the
      filesystem refuses a link). This is the reference behaviour for
      consumers; wpm implements the same mapping natively.

Determinism: entries are sorted, timestamps are pinned, compression is
deflate level 9, so the same dist tree produces the same bytes (the same
reproducibility premise as build.py's /Brepro). Only the standard library
is used, so the script runs on the Windows CI runners and on Linux.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

MANIFEST_NAME = "links.json"
MANIFEST_FORMAT = "niugit-links/1"
# Roots of the dist tree that go into the archive (the old CI step zipped
# exactly these two directories).
PACK_ROOTS = ("mingw64", "etc")
# Fixed timestamp so the archive is reproducible.
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_files(dist):
    """Return {rel-posix-path: absolute Path} for every file under PACK_ROOTS.

    A symlink in dist would silently become a regular file in the zip, so
    refuse instead of changing its meaning. Zip files from an earlier run
    sitting at the dist root are not under PACK_ROOTS and are ignored.
    """
    files = {}
    for root in PACK_ROOTS:
        base = dist / root
        if not base.is_dir():
            raise SystemExit(f"dist tree is missing {root}/ ({dist})")
        for path in sorted(base.rglob("*")):
            if path.is_symlink():
                raise SystemExit(f"refusing to package symlink: {path}")
            if path.is_file():
                files[path.relative_to(dist).as_posix()] = path
    return files


def group_by_content(files):
    """Group relative paths by (size, sha256); return {key: [rel paths]}."""
    groups = {}
    for rel, path in sorted(files.items()):
        key = (path.stat().st_size, sha256_of(path))
        groups.setdefault(key, []).append(rel)
    return groups


def canonical_of(members):
    """Pick the surviving copy of a duplicate group, deterministically.

    Prefer mingw64/bin/ so the primary lookup path (the one wpm maps to
    usr/bin/git.exe, and the one a plain unzip still provides) is the
    physical copy; otherwise the lexicographically first path. For the
    perl pairs this keeps the dashed name (git-send-email) and links the
    .perl form, which is what git looks up.
    """
    in_bin = [m for m in members if m.startswith("mingw64/bin/")]
    return sorted(in_bin if in_bin else members)[0]


def build_manifest(groups, compat_name):
    """Return (manifest dict, {rel path: canonical rel path} for dedup)."""
    links = []
    canonical = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        source = canonical_of(members)
        for target in sorted(members):
            if target == source:
                continue
            links.append({"target": target, "source": source})
            canonical[target] = source
    links.sort(key=lambda link: link["target"])
    manifest = {
        "format": MANIFEST_FORMAT,
        "compat_archive": compat_name,
        "links": links,
    }
    return manifest, canonical


def zip_info(name, is_dir=False):
    info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o755 if is_dir else 0o644) << 16
    if is_dir:
        info.external_attr |= 0x10
    return info


def write_zip(files, manifest, out_path):
    """Write the deduped archive: canonical copies + links.json at root."""
    canonical_targets = {link["target"] for link in manifest["links"]}
    names = sorted(rel for rel in files if rel not in canonical_targets)
    parents = set()
    for rel in names:
        parts = Path(rel).parts[:-1]
        for i in range(1, len(parts) + 1):
            parents.add("/".join(parts[:i]) + "/")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for directory in sorted(parents):
            zf.writestr(zip_info(directory, is_dir=True), b"")
        for rel in names:
            zf.write(files[rel], rel, zipfile.ZIP_DEFLATED)
        payload = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
                   ).encode("utf-8")
        zf.writestr(zip_info(MANIFEST_NAME), payload)


def write_compat_zip(files, out_path):
    """Write the fully expanded archive (old layout, no manifest)."""
    names = sorted(files)
    parents = set()
    for rel in names:
        parts = Path(rel).parts[:-1]
        for i in range(1, len(parts) + 1):
            parents.add("/".join(parts[:i]) + "/")
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for directory in sorted(parents):
            zf.writestr(zip_info(directory, is_dir=True), b"")
        for rel in names:
            zf.write(files[rel], rel, zipfile.ZIP_DEFLATED)


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() \
        else None


def describe_archive(path):
    data = path.read_bytes()
    with zipfile.ZipFile(path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
    return len(infos), len(data), hashlib.sha256(data).hexdigest()


def package(dist, version, arch, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"niu-git-{version}-{arch}"
    deduped_path = out_dir / f"{stem}.zip"
    compat_path = out_dir / f"{stem}-compat.zip"
    files = collect_files(dist)
    groups = group_by_content(files)
    manifest, _ = build_manifest(groups, compat_path.name)
    write_zip(files, manifest, deduped_path)
    write_compat_zip(files, compat_path)
    dup_files = sum(len(m) - 1 for m in groups.values() if len(m) > 1)
    dup_groups = sum(1 for m in groups.values() if len(m) > 1)
    n_d, size_d, sha_d = describe_archive(deduped_path)
    n_c, size_c, sha_c = describe_archive(compat_path)
    print(f"package: {len(files)} files in dist, "
          f"{dup_groups} duplicate groups, {dup_files} linked copies")
    print(f"package: {deduped_path.name}: {n_d} entries, {size_d} bytes, "
          f"sha256 {sha_d}")
    print(f"package: {compat_path.name}: {n_c} entries, {size_c} bytes, "
          f"sha256 {sha_c}")
    print(f"package: deduped saves {size_c - size_d} bytes "
          f"({100.0 * (size_c - size_d) / size_c:.1f}%) vs compat")
    print("package: wpm-source must point at the -compat.zip until "
          "WinuxCmd#1835 (wpm links support) lands")
    return deduped_path, compat_path


def read_manifest(deduped_zip):
    with zipfile.ZipFile(deduped_zip) as zf:
        try:
            raw = zf.read(MANIFEST_NAME)
        except KeyError:
            raise SystemExit(f"verify: {deduped_zip} has no {MANIFEST_NAME}")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SystemExit(f"verify: {MANIFEST_NAME} is not valid JSON: {exc}")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise SystemExit(
            f"verify: {MANIFEST_NAME} format is "
            f"{manifest.get('format')!r}, want {MANIFEST_FORMAT!r}")
    links = manifest.get("links")
    if not isinstance(links, list):
        raise SystemExit(f"verify: {MANIFEST_NAME} has no 'links' list")
    return manifest


def materialize(directory, strict=False):
    """Create every manifest target in directory as a hardlink of its source.

    With strict=True (the verifier) a failed link is fatal; otherwise fall
    back to a plain copy with a warning, so a filesystem that refuses links
    still yields a working tree.
    """
    directory = Path(directory)
    manifest = json.loads((directory / MANIFEST_NAME).read_text("utf-8"))
    made = []
    for link in manifest["links"]:
        target = directory / link["target"]
        source = directory / link["source"]
        if not source.is_file():
            raise SystemExit(f"materialize: link source missing: "
                             f"{link['source']}")
        if target.exists():
            raise SystemExit(f"materialize: link target already exists: "
                             f"{link['target']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError as exc:
            if strict:
                raise SystemExit(
                    f"materialize: cannot hardlink {link['target']}: {exc}")
            print(f"materialize: hardlink failed for {link['target']} "
                  f"({exc}); copying instead", file=sys.stderr)
            shutil.copyfile(source, target)
        made.append(link["target"])
    return made


def tree_snapshot(root):
    """Return {rel-posix: (size, sha256)} for every file under root."""
    snapshot = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"verify: unexpected symlink in tree: {path}")
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if rel == MANIFEST_NAME:
                continue
            snapshot[rel] = (path.stat().st_size, sha256_of(path))
    return snapshot


def verify(deduped_zip, compat_zip):
    deduped_zip = Path(deduped_zip)
    compat_zip = Path(compat_zip)
    manifest = read_manifest(deduped_zip)
    with zipfile.ZipFile(deduped_zip) as zf:
        entries = {i.filename for i in zf.infolist() if not i.is_dir()}
        # The deduped archive must carry no duplicate content group: every
        # remaining copy is unique by (size, CRC).
        seen = {}
        for info in zf.infolist():
            if info.is_dir() or info.filename == MANIFEST_NAME:
                continue
            key = (info.file_size, info.CRC)
            if key in seen:
                raise SystemExit(
                    f"verify: duplicate content survives in {deduped_zip}: "
                    f"{seen[key]} and {info.filename}")
            seen[key] = info.filename
    targets = [link["target"] for link in manifest["links"]]
    if len(targets) != len(set(targets)):
        raise SystemExit("verify: duplicate link targets in manifest")
    for link in manifest["links"]:
        if link["source"] not in entries:
            raise SystemExit(
                f"verify: link source not in archive: {link['source']}")
        if link["target"] in entries:
            raise SystemExit(
                f"verify: link target still stored in archive: {link['target']}")
        if link["target"] == link["source"]:
            raise SystemExit(f"verify: link target == source: {link['target']}")
    with tempfile.TemporaryDirectory(prefix="niugit-verify-") as tmp:
        tmp = Path(tmp)
        expanded = tmp / "deduped"
        reference = tmp / "compat"
        with zipfile.ZipFile(deduped_zip) as zf:
            zf.extractall(expanded)
        with zipfile.ZipFile(compat_zip) as zf:
            zf.extractall(reference)
        made = materialize(expanded, strict=True)
        got = tree_snapshot(expanded)
        want = tree_snapshot(reference)
        if got != want:
            only_got = sorted(set(got) - set(want))
            only_want = sorted(set(want) - set(got))
            differing = sorted(p for p in set(got) & set(want)
                               if got[p] != want[p])
            raise SystemExit(
                "verify: materialised tree differs from compat tree: "
                f"only in deduped={only_got[:5]} "
                f"only in compat={only_want[:5]} "
                f"content differs={differing[:5]}")
    n_d, size_d, _ = describe_archive(deduped_zip)
    n_c, size_c, _ = describe_archive(compat_zip)
    print(f"verify: OK - {len(made)} links materialised as hardlinks, "
          f"materialised tree byte-identical to compat "
          f"({n_d} deduped entries + {len(made)} links == {n_c} compat "
          f"entries; {size_d} vs {size_c} bytes)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="niu-git release packaging (issue #23)")
    parser.add_argument("dist", nargs="?",
                        help="dist/ tree to package (mingw64/ + etc/)")
    parser.add_argument("--version", help="release version, e.g. "
                        "2.56.0.windows.4")
    parser.add_argument("--arch", choices=["x64", "arm64"], default="x64")
    parser.add_argument("--out-dir", type=Path,
                        help="where the zips go (default: <dist>)")
    parser.add_argument("--verify", nargs=2, metavar=("DEDUPED", "COMPAT"),
                        help="verify a deduped zip against its compat zip")
    parser.add_argument("--materialize", metavar="DIR",
                        help="materialise links.json in DIR as hardlinks")
    args = parser.parse_args(argv)
    if args.verify:
        return verify(*args.verify)
    if args.materialize:
        made = materialize(args.materialize)
        print(f"materialize: {len(made)} links created in "
              f"{args.materialize}")
        return 0
    if not args.dist or not args.version:
        parser.error("packaging needs <dist> and --version")
    dist = Path(args.dist)
    if not dist.is_dir():
        parser.error(f"{dist} is not a directory")
    package(dist, args.version, args.arch, args.out_dir or dist)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
