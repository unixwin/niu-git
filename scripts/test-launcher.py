#!/usr/bin/env python
"""Differential argv-fidelity test for the niu-git launcher and its git build.

Runs the same logical command through two stacks in twin repo copies:
  wrapper = <root>/git.exe            (launch-git.c entry)
  real    = <root>/mingw64/bin/git.exe
and compares stdout/stderr/exit code plus repo end state (HEAD bytes, commit
message bytes, reflog, status) byte-for-byte. Message payloads are additionally
asserted against their *intended literal bytes*, independent of the
differential (guards against both stacks sharing a bug, e.g. rewriting
`commit -m /c` into `c:/`). Behavior-only cases (missing-shell regressions,
issues #14/#15) instead assert the exit code and message shape on both stacks.

Target selection (first match wins):
  NIUGIT_TEST_WRAPPER / NIUGIT_TEST_REAL   explicit exe paths
  NIUGIT_TEST_ROOT                         dir holding git.exe + mingw64/
  ./dist                                   build.py output layout
  ../Niubash/winuxcmd/opt/niugit           installed product (smoke fallback)

Usage: python scripts/test-launcher.py [case-substring]
Exit code 0 = all pass.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

OID_RE = re.compile(rb"\b[0-9a-f]{7,64}\b")

FROZEN = {
    "GIT_AUTHOR_NAME": "Audit", "GIT_AUTHOR_EMAIL": "audit@example.com",
    "GIT_COMMITTER_NAME": "Audit", "GIT_COMMITTER_EMAIL": "audit@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000", "GIT_COMMITTER_DATE": "1700000000 +0000",
    # deterministic runs: no background daemon holding handles, no eol rewrites
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "false",
    "GIT_CONFIG_KEY_1": "core.autocrlf", "GIT_CONFIG_VALUE_1": "false",
}


def locate_targets():
    w = os.environ.get("NIUGIT_TEST_WRAPPER")
    r = os.environ.get("NIUGIT_TEST_REAL")
    if w and r:
        return w, r
    cands = []
    root = os.environ.get("NIUGIT_TEST_ROOT")
    if root:
        cands.append(root)
    cands.append(os.path.join(REPO, "dist"))
    cands.append(r"C:\Users\cmx\AppData\Local\Programs\Niubash\winuxcmd\opt\niugit")
    for c in cands:
        w2 = os.path.join(c, "git.exe")
        r2 = os.path.join(c, "mingw64", "bin", "git.exe")
        if os.path.exists(w2) and os.path.exists(r2):
            return os.environ.get("NIUGIT_TEST_WRAPPER", w2), os.environ.get("NIUGIT_TEST_REAL", r2)
    sys.exit("test-launcher: no target tree found (set NIUGIT_TEST_ROOT or "
             "NIUGIT_TEST_WRAPPER/NIUGIT_TEST_REAL)")


def norm(b):
    return OID_RE.sub(b"<OID>", b) if isinstance(b, bytes) else b


def run(exe, args, cwd, stdin=None, timeout=60, env=None):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith("GIT_"):
            del e[k]
    e.update(FROZEN)
    # equalize PATH (the launcher prepends its mingw64/bin by design; give the
    # real side the same so the comparison isolates argv fidelity)
    e["PATH"] = os.path.join(os.path.dirname(exe), "mingw64", "bin") + ";" + e.get("PATH", "")
    e.update(env or {})
    try:
        p = subprocess.run([exe] + args, cwd=cwd, capture_output=True,
                           env=e, input=stdin, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return "TIMEOUT", b"", b""


def dialect_forms(path, stem):
    """POSIX dialect spellings of a native Windows path (issue #18):
    {STEM} is the path itself, plus its forward-slash form and the
    /<drive>, /mnt/<drive> and /cygdrive/<drive> spellings of it."""
    drive = path[0]
    rest = path[2:].replace("\\", "/")
    return {
        "{" + stem + "}": path,
        "{" + stem + "_FWD}": path.replace("\\", "/"),
        "{" + stem + "_POSIX}": "/" + drive + rest,
        "{" + stem + "_MNT}": "/mnt/" + drive + rest,
        "{" + stem + "_CYG}": "/cygdrive/" + drive + rest,
    }


def substitute(args, forms):
    out = []
    for a in args:
        for tok, val in forms.items():
            # Message payloads travel as bytes (their exact bytes are the
            # assertion); replace in the arg's own type so a bytes arg
            # never meets a str token. The tokens are pure ASCII, so the
            # encode is exact and non-token bytes pass through untouched.
            if isinstance(a, bytes):
                a = a.replace(tok.encode(), val.encode())
            else:
                a = a.replace(tok, val)
        out.append(a)
    return out


_alt_cwd_dirs = []


def alt_drive_dir(repo):
    """A scratch dir on a different drive than the repo, for the
    issue #18 /tmp drift pin (the landing must not follow the cwd
    drive). None when no second writable drive exists; the pin then
    runs from the repo itself and still discriminates."""
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        if letter == repo[0].upper():
            continue
        try:
            d = tempfile.mkdtemp(prefix="niugit-altcwd-",
                                 dir=letter + ":\\")
        except OSError:
            continue
        _alt_cwd_dirs.append(d)
        return d
    return None


class Case:
    def __init__(self, name, msg=None, runs=None, check_msgs=None,
                 setup_files=None, env=None, checks=None,
                 cwd_alt_drive=False):
        """msg: literal bytes committed via `commit -m` (asserted byte-exact in
        HEAD, including the trailing newline). runs: list of arg-lists to run
        in order; defaults to a single commit for `msg` cases. Run args may
        embed dialect tokens ({REPO}, {REPO_MNT}, {BASE_CYG}, {ROOT_POSIX},
        {REPO_TMP}, ...; see dialect_forms), substituted per side before
        running. check_msgs: {key: expected bytes} additional annotation
        assertions (e.g. tags). setup_files: {relpath: bytes} written into
        each repo before running. env: {var: value} merged over the per-run
        environment (e.g. a scrubbed PATH). checks: assertions on the last
        run's result, applied to both stacks: "rc:<int>", "err~:<substr>",
        "err!~:<substr>" (must not contain), "out~:<substr>".
        cwd_alt_drive: run from a scratch dir on another drive."""
        self.name = name
        self.msg = msg.encode() if isinstance(msg, str) else msg
        if runs is None:
            if self.msg is not None:
                runs = [["commit", "-q", "-m", self.msg, "--allow-empty"]]
            else:
                runs = []
        self.runs = runs
        self.check_msgs = check_msgs or {}
        self.setup_files = setup_files or {}
        self.env = env or {}
        self.checks = checks or []
        self.cwd_alt_drive = cwd_alt_drive


CASES = [
    # ---- issue #3 family: message payloads are literal bytes ----
    Case("m/backticks", "docs: note `uname -r` and `pwd` handling"),
    Case("m/dollar-brace", "use ${HOME} and ${UNDEFINED_VAR} here"),
    Case("m/cmd-subst", "out is $(uname -s) and $(pwd) ok"),
    Case("m/zsh-kv", "zsh text ${(kv)MAP} verbatim"),
    Case("m/slash-path-text", "see /c/Windows and /d/repo/docs for details"),
    # ---- issue #5: message-shaped path tokens must NOT be rewritten ----
    Case("m/lone-posix-drive-c", "/c"),
    Case("m/lone-posix-drive-d", "/d"),
    Case("m/posix-existing", "/d/repo"),
    Case("m/drive-slash-form", "/D:/repo"),
    # ---- issue #4: quoting must round-trip ----
    Case("m/embedded-quotes", 'she said "hi" and left'),
    Case("m/trailing-backslash", "ends with backslash \\"),
    Case("m/shell-snippet", 'fix: guard PATH="a;b" in run.sh'),
    Case("m/embedded-quote-eof", 'loud "'),
    # ---- breadth: messages still work in every shape ----
    Case("m/cjk", "提交信息：修复了路径转换的问题"),
    Case("m/emoji", "fix: emoji \U0001f389 and mixed 中文 ok"),
    Case("m/multiline", "line1\n\nbody line2\nbody line3\n"),
    Case("m/long-8k", ("release notes: " + "lorem ipsum `code` ${VAR} /path/token " * 150)[:8192]),
    Case("m/leading-dash", "-not-a-flag message"),
    Case("m/empty", runs=[["commit", "-q", "-m", "", "--allow-empty"]]),
    Case("m-amend/backticks",
         runs=[["commit", "-q", "-m", "first", "--allow-empty"],
               ["commit", "--amend", "--allow-empty", "-m", "amended `uname -r` msg"]],
         check_msgs={"amended `uname -r` msg": b"amended `uname -r` msg\n"}),
    Case("tag/annotation-posix",
         runs=[["commit", "-q", "-m", "base", "--allow-empty"],
               ["tag", "-a", "-m", "/c", "v1"]],
         check_msgs={"tag:v1": b"/c\n"}),
    # ---- issue #14: use_shell must not AV when no sh is on PATH ----
    # PATH scrubbed to a dir without sh.exe (System32 carries none); the
    # wrapper prepends its own mingw64/bin, which ships no shell either, so
    # both stacks must resolve the same failing lookup. A clean fatal exit
    # (128) replaces the access violation (3221225477).
    Case("sh-missing/alias",
         runs=[["-c", "alias.p=!echo hi", "p"]],
         env={"PATH": r"C:\Windows\System32"},
         checks=["rc:128", "err~:no shell found on PATH"]),
    # ---- issue #15: a failing hook spawn must name the missing interpreter ----
    # The hook exists and is executable; what is missing is the sh its
    # shebang resolves to. ENOENT ("No such file or directory") describes
    # neither and was previously printed against the hook itself.
    Case("sh-missing/hook-shebang",
         setup_files={"hooks/pre-commit": b"#!/bin/sh\nexit 1\n"},
         runs=[["add", "f.txt"],
               ["-c", "core.hooksPath=hooks", "commit", "-q", "-m", "hook blocks the commit"]],
         env={"PATH": r"C:\Windows\System32"},
         checks=["rc:1", "err~:cannot spawn hooks/pre-commit",
                 "err~:no 'sh' found on PATH",
                 "err!~:No such file or directory"]),
    # ---- issue #18: the -C dialect set matches the family's ----
    # git -C must accept every spelling the family accepts for the same
    # directory: the twin repos live under %TEMP%, so {REPO_TMP} is the
    # issue's discriminating probe (a repo under %TEMP% addressed via
    # /tmp/...). "not a git repository" (rc 128) proves the chdir
    # landed and only repo discovery failed; "cannot change to" is the
    # dialect miss. Missing targets must keep the original literal in
    # the diagnostic (the conversion's both-spellings-fail invariant).
    Case("cd/win-backslash",
         runs=[["-C", "{REPO}", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"]),
    Case("cd/win-fwdslash",
         runs=[["-C", "{REPO_FWD}", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"]),
    Case("cd/posix-drive",
         runs=[["-C", "{REPO_POSIX}", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"]),
    Case("cd/mnt-drive",
         runs=[["-C", "{REPO_MNT}", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"]),
    Case("cd/cygdrive",
         runs=[["-C", "{REPO_CYG}", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"]),
    Case("cd/tmp-dialect",
         runs=[["-C", "{REPO_TMP}", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"]),
    Case("cd/tmp-dialect-alt-cwd",
         runs=[["-C", "{REPO_TMP}", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"],
         cwd_alt_drive=True),
    Case("cd/relative",
         runs=[["-C", ".", "status", "--porcelain"]],
         checks=["rc:0", "out~:?? f.txt"]),
    Case("cd/gitdir-mnt",
         runs=[["-C", "{REPO_MNT}/.git", "status"]],
         checks=["rc:128", "err~:must be run in a work tree"]),
    Case("cd/root-posix",
         runs=[["-C", "{ROOT_POSIX}", "status"]],
         checks=["rc:128", "err~:not a git repository",
                 "err!~:cannot change to"]),
    Case("cd/root-mnt",
         runs=[["-C", "{ROOT_MNT}", "status"]],
         checks=["rc:128", "err~:not a git repository",
                 "err!~:cannot change to"]),
    Case("cd/root-cygdrive",
         runs=[["-C", "{ROOT_CYG}", "status"]],
         checks=["rc:128", "err~:not a git repository",
                 "err!~:cannot change to"]),
    Case("cd/bare-tmp",
         runs=[["-C", "/tmp", "status"]],
         checks=["rc:128", "err~:not a git repository",
                 "err!~:cannot change to"]),
    Case("cd/nonrepo-posix",
         runs=[["-C", "{BASE_POSIX}", "status"]],
         checks=["rc:128", "err~:not a git repository",
                 "err!~:cannot change to"]),
    Case("cd/nonrepo-mnt",
         runs=[["-C", "{BASE_MNT}", "status"]],
         checks=["rc:128", "err~:not a git repository",
                 "err!~:cannot change to"]),
    Case("cd/nonrepo-cygdrive",
         runs=[["-C", "{BASE_CYG}", "status"]],
         checks=["rc:128", "err~:not a git repository",
                 "err!~:cannot change to"]),
    Case("cd/missing-mnt",
         runs=[["-C", "/mnt/d/niugit18-no-such-dir", "status"]],
         checks=["rc:128",
                 "err~:cannot change to '/mnt/d/niugit18-no-such-dir'"]),
    Case("cd/missing-cygdrive",
         runs=[["-C", "/cygdrive/d/niugit18-no-such-dir", "status"]],
         checks=["rc:128",
                 "err~:cannot change to '/cygdrive/d/niugit18-no-such-dir'"]),
    Case("cd/missing-tmp",
         runs=[["-C", "/tmp/niugit18-no-such-dir", "status"]],
         checks=["rc:128",
                 "err~:cannot change to '/tmp/niugit18-no-such-dir'"]),
]


def make_repo(base, name, setup_files=None):
    path = os.path.join(base, name)
    os.makedirs(path)
    with open(os.path.join(path, "f.txt"), "w", newline="") as f:
        f.write("hello world\n")
    if setup_files:
        for rel, data in setup_files.items():
            dst = os.path.join(path, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            mode = "wb" if isinstance(data, bytes) else "w"
            with open(dst, mode) as f:
                f.write(data)
    return path


def commit_msgs(repo, real):
    out = {}
    rc, o, e = run(real, ["log", "--format=%H %s", "-z", "--all"], repo)
    if rc != 0:
        return {"<no-log>": e}
    for entry in o.split(b"\0"):
        if not entry:
            continue
        oid, subj = entry.split(b" ", 1)
        rc2, o2, _ = run(real, ["cat-file", "commit", oid.decode()], repo)
        body = o2[o2.find(b"\n\n") + 2:]
        out[subj.decode("utf-8", "replace")] = body
    rc, o, e = run(real, ["tag", "-l"], repo)
    for tag in o.split():
        rc2, o2, _ = run(real, ["cat-file", "tag", tag.decode()], repo)
        if rc2 == 0 and b"\n\n" in o2:
            out["tag:" + tag.decode()] = o2[o2.find(b"\n\n") + 2:]
    return out


def repo_state(repo, real):
    rc, o, _ = run(real, ["rev-parse", "HEAD"], repo)
    head = o.strip() if rc == 0 else b"no-head"
    head_msg = b""
    if head != b"no-head":
        rc2, o2, _ = run(real, ["cat-file", "commit", head.decode()], repo)
        head_msg = o2[o2.find(b"\n\n") + 2:] if rc2 == 0 else b""
    rc, o, _ = run(real, ["reflog", "--format=%gs"], repo)
    reflog = norm(o)
    rc, o, _ = run(real, ["status", "--porcelain"], repo)
    status = o
    msgs = commit_msgs(repo, real)
    return {"HEAD": head, "head_msg": head_msg, "reflog": reflog, "status": status, "msgs": msgs}


def main(only=None):
    wrapper, real = locate_targets()
    print(f"wrapper: {wrapper}\nreal:    {real}")
    base = tempfile.mkdtemp(prefix="niugit-launcher-test-")
    failures = 0
    total = 0
    try:
        for c in CASES:
            if only and only not in c.name:
                continue
            total += 1
            diffs = []
            states = {}
            results = {}
            for side, exe in (("wrapper", wrapper), ("real", real)):
                repo = make_repo(base, f"{c.name.replace('/', '_')}.{side}",
                                 setup_files=c.setup_files)
                run(real, ["init", "-q", "-b", "main"], repo)
                forms = dialect_forms(repo, "REPO")
                forms.update(dialect_forms(base, "BASE"))
                forms["{ROOT_POSIX}"] = "/" + repo[0]
                forms["{ROOT_MNT}"] = "/mnt/" + repo[0]
                forms["{ROOT_CYG}"] = "/cygdrive/" + repo[0]
                tail = os.path.relpath(repo, tempfile.gettempdir())
                if not tail.startswith(".."):
                    forms["{REPO_TMP}"] = "/tmp/" + tail.replace("\\", "/")
                cwd = repo
                if c.cwd_alt_drive:
                    cwd = alt_drive_dir(repo) or repo
                for args in c.runs:
                    results[side] = run(exe, substitute(args, forms),
                                        cwd, env=c.env)
                states[side] = repo_state(repo, real)
            # 1. absolute message-byte assertions (catches shared-stack bugs).
            # git's default commit-message cleanup strips buffer-trailing
            # whitespace and ensures one trailing newline — mirror that.
            expected = dict(c.check_msgs)
            if c.msg is not None:
                m = c.msg
                expected["<commit>"] = m if m.endswith(b"\n") else m.rstrip() + b"\n"
            have = dict(states["wrapper"]["msgs"])
            have["<commit>"] = states["wrapper"]["head_msg"]
            for label, want in expected.items():
                got = have.get(label, b"<missing>")
                if got != want:
                    diffs.append(f"message {label!r}: want {want!r} got {got!r}")
            # 2. differential wrapper vs real
            fw, fg = states["wrapper"], states["real"]
            for k in ("HEAD", "reflog", "status"):
                if fw[k] != fg[k]:
                    diffs.append(f"{k}: wrapper={fw[k]!r} real={fg[k]!r}")
            # 3. per-run behavior assertions (missing-shell cases etc.)
            if c.checks:
                rc, out, err = results["real"]
                text = err.decode("utf-8", "replace") + "\n" + out.decode("utf-8", "replace")
                for spec in c.checks:
                    kind, _, val = spec.partition(":")
                    okval = False
                    if kind == "rc":
                        okval = rc == int(val)
                        label = f"rc={rc}"
                    elif kind == "err~":
                        okval = val in err.decode("utf-8", "replace")
                        label = f"stderr: {text.strip()!r}"
                    elif kind == "err!~":
                        okval = val not in err.decode("utf-8", "replace")
                        label = f"stderr: {err.decode('utf-8', 'replace').strip()!r}"
                    elif kind == "out~":
                        okval = val in out.decode("utf-8", "replace")
                        label = f"stdout: {out.decode('utf-8', 'replace')!r}"
                    else:
                        diffs.append(f"check {spec!r}: unknown kind")
                        continue
                    if not okval:
                        diffs.append(f"check {spec!r} failed ({label})")
            if diffs:
                failures += 1
                print(f"[FAIL] {c.name}")
                for d in diffs:
                    print("       " + d)
            else:
                print(f"[ok]   {c.name}")
    finally:
        time.sleep(0.2)
        shutil.rmtree(base, ignore_errors=True)
        for d in _alt_cwd_dirs:
            shutil.rmtree(d, ignore_errors=True)
    print(f"\n== test-launcher: {total - failures}/{total} passed ==")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
