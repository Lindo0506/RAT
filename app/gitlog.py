"""Git plumbing for the RAT.

Central output contract (verified against git 2.43):

    git log --no-merges --numstat -z -M50% \
        --format=%x01%H%x00%ct%x00%an%x00%ae%x00%s <ref>

  * header fields are NUL separated; the header itself is NUL terminated
  * a bare "\\n" separates the header from the entries (only if entries exist)
  * entry        : b"<add>\\t<del>\\t<path>\\0"
  * rename entry : b"<add>\\t<del>\\t\\0<oldpath>\\0<newpath>\\0"
  * binary entry : b"-\\t-\\t..."          -> not measured (git's binary detection)
  * empty commits emit no entries at all

Line counts are relative to the commit's (single) parent; the initial commit
diffs against the empty tree.  Renames are detected at the required 50%
threshold and their deltas are attributed to the NEW path, so a pure rename
(0/0) never changes any metric.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

LOG_FORMAT = "--format=%x01%H%x00%ct%x00%an%x00%ae%x00%s"
LOG_FLAGS = ["--no-merges", "--numstat", "-z", "-M50%"]
ENTRY_RE = re.compile(rb"^(\d+|-)\t(\d+|-)\t")
SHA_RE = re.compile(rb"^[0-9a-f]{40}([0-9a-f]{24})?$")


class GitError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# low level helpers
# ---------------------------------------------------------------------------

def _cmd(git_dir: str, args: list[str]) -> list[str]:
    return ["git", f"--git-dir={git_dir}"] + args


def run(git_dir: str, args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(_cmd(git_dir, args), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out") from exc


def run_text(git_dir: str, args: list[str], timeout: float | None = None) -> str:
    proc = run(git_dir, args, timeout=timeout)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {args[0]} failed: {err[:500]}")
    return proc.stdout.decode("utf-8", "replace")


def is_repo(git_dir: str) -> bool:
    proc = run(git_dir, ["rev-parse", "--git-dir"])
    return proc.returncode == 0


def resolve_ref(git_dir: str, ref: str) -> str | None:
    """Peel a rev (branch/tag/sha/HEAD) to a commit sha."""
    proc = run(git_dir, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if proc.returncode != 0:
        return None
    return proc.stdout.decode().strip()


def rev_list_no_merges(git_dir: str, ref: str) -> list[str]:
    """H-bar: non-merge commits reachable from `ref` (newest first)."""
    out = run_text(git_dir, ["rev-list", "--no-merges", ref])
    return out.split()


def _for_each_ref(git_dir: str, namespace: str) -> list[dict]:
    out = run_text(git_dir, [
        "for-each-ref",
        "--format=%(refname:short)%00%(objectname)%00%(*objectname)",
        namespace,
    ])
    refs = []
    for line in out.splitlines():
        parts = line.split("\x00")
        if len(parts) != 3 or not parts[0]:
            continue
        name, sha, peeled = parts
        refs.append({"name": name, "sha": (peeled or sha)[:12]})
    return refs


def list_refs(git_dir: str) -> dict:
    """Branches/tags/HEAD for the reference picker."""
    head = run(git_dir, ["symbolic-ref", "--short", "-q", "HEAD"])
    head_name = head.stdout.decode().strip() if head.returncode == 0 else "(detached)"
    head_sha = resolve_ref(git_dir, "HEAD")
    return {
        "head": head_name,
        "head_sha": (head_sha or "")[:12],
        "branches": _for_each_ref(git_dir, "refs/heads"),
        "tags": _for_each_ref(git_dir, "refs/tags"),
    }


def has_file_at(git_dir: str, ref: str, path: str) -> bool:
    proc = run(git_dir, ["cat-file", "-e", f"{ref}:{path}"])
    return proc.returncode == 0


def ls_tree_files(git_dir: str, ref: str, cap: int = 200_000) -> list[str]:
    """Every blob path in the tree at `ref` (used for the file browser)."""
    out = run_text(git_dir, ["ls-tree", "-r", "--name-only", "-z", ref])
    files = [p for p in out.split("\x00") if p]
    return files[:cap]


def check_mailmap(git_dir: str, ref: str, identities: list[tuple[str, str]],
                  chunk: int = 200) -> dict[tuple[str, str], tuple[str, str]]:
    """Canonicalise raw (name, email) identities with git's mailmap machinery.

    Uses `.mailmap` from the tree at `ref` (works for bare mirrors too).
    Unmapped identities are returned unchanged.
    """
    ident_re = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^>]*)>\s*$")
    mapping: dict[tuple[str, str], tuple[str, str]] = {}
    use_blob = has_file_at(git_dir, ref, ".mailmap")
    for start in range(0, len(identities), chunk):
        batch = identities[start:start + chunk]
        args: list[str] = []
        if use_blob:
            args += ["-c", f"mailmap.blob={ref}:.mailmap"]
        args += ["check-mailmap"] + [f"{name} <{email}>" for name, email in batch]
        lines: list[str] = []
        try:
            lines = run_text(git_dir, args).splitlines()
        except GitError:
            lines = []
        for identity, line in zip(batch, lines):
            match = ident_re.match(line.strip())
            if match:
                mapping[identity] = (match.group("name") or identity[0],
                                     match.group("email") or identity[1])
            else:
                mapping[identity] = identity
        for identity in batch:  # any lines we could not match / missing
            mapping.setdefault(identity, identity)
    return mapping


def clone(url: str, dest: Path, progress=None) -> None:
    """Full (deep) mirror clone with progress callbacks."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["git", "clone", "--mirror", "--progress", "--", url, str(dest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    pct = re.compile(r":\s+(\d+)%")
    assert proc.stderr is not None
    for raw in proc.stderr:
        if progress:
            m = pct.search(raw.decode("utf-8", "replace"))
            if m:
                progress(int(m.group(1)) / 100.0)
    proc.wait()
    if proc.returncode != 0:
        raise GitError(f"clone failed for {url}")


# ---------------------------------------------------------------------------
# log parsing
# ---------------------------------------------------------------------------

def _nul_tokens(stream) -> Iterator[bytes]:
    """Split a byte stream on NUL, yielding tokens (empty ones included)."""
    carry = b""
    while True:
        chunk = stream.read(1 << 20)
        if not chunk:
            break
        carry += chunk
        parts = carry.split(b"\x00")
        carry = parts.pop()
        yield from parts
    if carry:
        yield carry


def parse_log_stream(stream) -> Iterator[tuple]:
    """Yield (sha, committer_ts, name, email, subject, changes).

    `changes` is a list of (path, added, deleted) for every measured
    (non-binary, churn > 0) entry, rename deltas attributed to the new path.
    """
    tokens = _nul_tokens(stream)
    cur: list | None = None
    while True:
        try:
            raw = next(tokens)
        except StopIteration:
            break
        tok = raw.lstrip(b"\n")
        if not tok:
            continue
        if tok[:1] == b"\x01" and SHA_RE.match(tok[1:]):
            if cur is not None:
                yield tuple(cur)
            try:
                ts = int(next(tokens))
            except (StopIteration, ValueError):
                ts = 0
            name = next(tokens, b"").decode("utf-8", "replace")
            email = next(tokens, b"").decode("utf-8", "replace")
            subject = next(tokens, b"").decode("utf-8", "replace")
            cur = [tok[1:].decode(), ts, name, email, subject, []]
            continue
        if cur is None:
            continue
        match = ENTRY_RE.match(tok)
        if not match:
            continue
        add_raw, del_raw = match.group(1), match.group(2)
        rest = tok[match.end():]
        if rest == b"":
            # rename / binary-rename: old path token then new path token
            next(tokens, b"")                    # old path (dropped on purpose)
            path = next(tokens, b"").decode("utf-8", "replace")
        else:
            path = rest.decode("utf-8", "replace")
        if add_raw == b"-" or del_raw == b"-":
            continue                             # binary files are not measured
        added, deleted = int(add_raw), int(del_raw)
        if added + deleted > 0:
            cur[5].append((path, added, deleted))
    if cur is not None:
        yield tuple(cur)


def iter_commits(git_dir: str, ref: str | None = None,
                 shas: list[str] | None = None, chunk: int = 400) -> Iterator[tuple]:
    """Stream parsed commits either from a ref (full history) or an explicit
    sha list (incremental, `--no-walk`)."""
    if ref is not None:
        batches = [[ref]]
    else:
        shas = shas or []
        batches = [["--no-walk=unsorted"] + shas[i:i + chunk]
                   for i in range(0, len(shas), chunk)]
    for extra in batches:
        with tempfile.TemporaryFile() as errf:
            proc = subprocess.Popen(
                _cmd(git_dir, ["log"] + LOG_FLAGS + [LOG_FORMAT] + extra),
                stdout=subprocess.PIPE, stderr=errf,
            )
            assert proc.stdout is not None
            try:
                yield from parse_log_stream(proc.stdout)
            finally:
                proc.stdout.close()
                code = proc.wait()
                if code != 0:
                    errf.seek(0)
                    detail = errf.read().decode("utf-8", "replace").strip()[:500]
                    raise GitError(f"git log failed: {detail}")
