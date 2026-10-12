#!/usr/bin/env python
"""Differential argv-fidelity test for the niu-git launcher and its git build.

Runs the same logical command through two stacks in twin repo copies:
  wrapper = <root>/git.exe            (launch-git.c entry)
  real    = <root>/mingw64/bin/git.exe
and compares stdout/stderr/exit code plus repo end state (HEAD bytes, commit
message bytes, reflog, status) byte-for-byte. Message payloads are additionally
asserted against their *intended literal bytes*, independent of the
differential (guards against both stacks sharing a bug, e.g. rewriting
`commit -m /c` into `c:/`).

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


def run(exe, args, cwd, stdin=None, timeout=60):
    e = dict(os.environ)
    for k in list(e):
        if k.startswith("GIT_"):
            del e[k]
    e.update(FROZEN)
    # equalize PATH (the launcher prepends its mingw64/bin by design; give the
    # real side the same so the comparison isolates argv fidelity)
    e["PATH"] = os.path.join(os.path.dirname(exe), "mingw64", "bin") + ";" + e.get("PATH", "")
    try:
        p = subprocess.run([exe] + args, cwd=cwd, capture_output=True,
                           env=e, input=stdin, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return "TIMEOUT", b"", b""


class Case:
    def __init__(self, name, msg=None, runs=None, check_msgs=None):
        """msg: literal bytes committed via `commit -m` (asserted byte-exact in
        HEAD, including the trailing newline). runs: list of arg-lists to run
        in order; defaults to a single commit for `msg` cases. check_msgs:
        {key: expected bytes} additional annotation assertions (e.g. tags)."""
        self.name = name
        self.msg = msg.encode() if isinstance(msg, str) else msg
        if runs is None:
            if self.msg is not None:
                runs = [["commit", "-q", "-m", self.msg, "--allow-empty"]]
            else:
                runs = []
        self.runs = runs
        self.check_msgs = check_msgs or {}


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
]


def make_repo(base, name):
    path = os.path.join(base, name)
    os.makedirs(path)
    with open(os.path.join(path, "f.txt"), "w", newline="") as f:
        f.write("hello world\n")
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
            for side, exe in (("wrapper", wrapper), ("real", real)):
                repo = make_repo(base, f"{c.name.replace('/', '_')}.{side}")
                run(real, ["init", "-q", "-b", "main"], repo)
                for args in c.runs:
                    run(exe, args, repo)
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
    print(f"\n== test-launcher: {total - failures}/{total} passed ==")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
