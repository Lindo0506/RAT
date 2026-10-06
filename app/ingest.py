"""Repository ingestion: clone / zip upload -> parse into SQLite -> authors.

Ingestion runs on background threads (max 2 concurrent) and reports progress
through the `repos` row so the dashboard can poll it.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import threading
import time
import traceback
import zipfile
from pathlib import Path

from . import authors as authors_mod
from . import gitlog
from . import metrics as metrics_mod
from .db import DATA_DIR, db, new_connection

REPOS_DIR = DATA_DIR / "repos"
UPLOADS_DIR = DATA_DIR / "uploads"
for _d in (REPOS_DIR, UPLOADS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

_INGEST_SLOTS = threading.Semaphore(2)


def _now() -> int:
    return int(time.time())


def update_repo(repo_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE repos SET {sets} WHERE id=?", (*fields.values(), repo_id))


def create_repo(name: str, source_type: str, source: str) -> int:
    cur = db.execute(
        """INSERT INTO repos(name, source_type, source, path, status, phase, progress,
                             message, created_at, updated_at)
           VALUES(?,?,?,'','pending','queued',0,'Waiting for an ingestion slot...',?,?)""",
        (name, source_type, source, _now(), _now()))
    return int(cur.lastrowid)


def _fail(repo_id: int, exc: Exception) -> None:
    msg = str(exc).strip() or exc.__class__.__name__
    traceback.print_exc()
    update_repo(repo_id, status="error", phase="error", message=msg[:500])


def _run_in_slot(work) -> None:
    def runner() -> None:
        with _INGEST_SLOTS:
            work()
    threading.Thread(target=runner, daemon=True, name="rat-ingest").start()


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def validate_clone_url(url: str) -> str:
    url = (url or "").strip()
    if not url or len(url) > 2000:
        raise ValueError("please provide a clone URL")
    if any(c in url for c in "\n\r\t ") or url.startswith("-"):
        raise ValueError("invalid characters in URL")
    scp_like = re.match(r"^[A-Za-z0-9._-]+@[^:]+:.+$", url)
    if not (re.match(r"^(https?|git|ssh|file)://", url) or scp_like or url.startswith("/")):
        raise ValueError("unsupported URL - use https://, ssh://, git://, git@host:path or a local path")
    return url


def slug(text: str, fallback: str = "repo") -> str:
    text = re.sub(r"\.git$", "", text.strip().rstrip("/"))
    text = text.rsplit("/", 1)[-1] or text
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")
    return (text or fallback)[:60]


def ingest_clone(repo_id: int, url: str) -> None:
    def work() -> None:
        dest = REPOS_DIR / f"{slug(url, 'clone')}-{repo_id}.git"
        try:
            if dest.exists():
                # Stale directory from an interrupted run - destination names are
                # unique per repo id, so it is safe to remove.
                shutil.rmtree(dest, ignore_errors=True)
            update_repo(repo_id, status="cloning", phase="clone", progress=0.0,
                        message=f"Cloning {url} ...")

            def on_progress(p: float) -> None:
                update_repo(repo_id, progress=min(p, 0.999),
                            message=f"Cloning {url} ... {int(p * 100)}%")

            gitlog.clone(url, dest, progress=on_progress)
            if not gitlog.is_repo(str(dest)):
                raise ValueError("download finished but the result is not a git repository")
            update_repo(repo_id, path=str(dest))
            _parse_and_finalize(repo_id, str(dest), "HEAD")
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            _fail(repo_id, exc)
    _run_in_slot(work)


def ingest_zip(repo_id: int, zip_path: Path) -> None:
    def work() -> None:
        root = REPOS_DIR / f"upload-{repo_id}"
        try:
            update_repo(repo_id, status="cloning", phase="unzip", progress=0.0,
                        message="Extracting archive...")
            with zipfile.ZipFile(zip_path) as zf:
                _safe_extract(zf, root)
            git_dir = locate_git_dir(root)
            if git_dir is None:
                raise ValueError("no .git directory or file found in the uploaded archive "
                                 "(the zip must contain the repository working tree or a bare repo)")
            if not gitlog.is_repo(str(git_dir)):
                raise ValueError("the .git found in the archive is not a valid git repository")
            update_repo(repo_id, path=str(git_dir))
            _parse_and_finalize(repo_id, str(git_dir), "HEAD")
        except zipfile.BadZipFile:
            _fail(repo_id, ValueError("not a valid .zip archive"))
        except Exception as exc:  # noqa: BLE001
            _fail(repo_id, exc)
        finally:
            try:
                zip_path.unlink()
            except OSError:
                pass
    _run_in_slot(work)


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> None:
    """Extract with zip-slip protection."""
    dest.mkdir(parents=True, exist_ok=True)
    base = dest.resolve()
    for info in zf.infolist():
        target = (base / info.filename).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise ValueError(f"unsafe path in archive: {info.filename}")
    zf.extractall(base)


def locate_git_dir(root: Path, max_depth: int = 3) -> Path | None:
    """Find the git directory inside an extracted archive.

    Handles: repo at the root, a single wrapping folder, `.git` files that
    point elsewhere inside the archive (worktrees), and bare repositories.
    """
    root = root.resolve()
    candidates: list[Path] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        candidates.append(directory)
        if depth < max_depth:
            try:
                kids = sorted(p for p in directory.iterdir() if p.is_dir() and p.name != ".git")
            except OSError:
                kids = []
            queue.extend((k, depth + 1) for k in kids)
    for directory in candidates:
        git_path = directory / ".git"
        if git_path.is_dir():
            return git_path
        if git_path.is_file():
            text = git_path.read_text(errors="replace").strip()
            if text.lower().startswith("gitdir:"):
                target = (directory / text.split(":", 1)[1].strip()).resolve()
                if str(target).startswith(str(root) + os.sep):
                    return target
    for directory in candidates:
        if (directory / "HEAD").is_file() and (directory / "objects").is_dir():
            return directory
    return None


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _parse_and_finalize(repo_id: int, git_dir: str, ref: str,
                        resolved: str | None = None) -> None:
    if resolved is None:
        resolved = gitlog.resolve_ref(git_dir, ref)
    if not resolved:
        raise ValueError(f"cannot resolve '{ref}' to a commit")
    update_repo(repo_id, status="parsing", phase="scanning", progress=0.0,
                message=f"Scanning commit history for {ref} ...")
    all_shas = gitlog.rev_list_no_merges(git_dir, resolved)
    if not all_shas:
        raise ValueError("this repository has no commits")
    total = len(all_shas)
    store = new_connection()
    try:
        existing = {row["sha"] for row in
                    store.execute("SELECT sha FROM commits WHERE repo_id=?", (repo_id,))}
        missing = [s for s in all_shas if s not in existing]
        if missing:
            new_raws = _parse_into(store, repo_id, git_dir, resolved, missing, total)
            if new_raws:
                update_repo(repo_id, phase="authors", progress=0.99,
                            message="Merging author identities (mailmap)...")
                authors_mod.ensure_groups(repo_id, git_dir, resolved, new_raws)
    finally:
        store.close()
    parsed = db.query_one("SELECT COUNT(*) n FROM commits WHERE repo_id=?", (repo_id,))["n"]
    update_repo(repo_id, ref=ref, resolved_sha=resolved, status="ready", phase="done",
                progress=1.0, message=f"{parsed:,} commits parsed")
    metrics_mod.invalidate(repo_id)


_INSERT_COMMIT = ("INSERT OR IGNORE INTO commits(repo_id, sha, ts, author_id, subject, added, deleted) "
                  "VALUES(?,?,?,?,?,?,?)")
_INSERT_CHANGE = ("INSERT INTO changes(repo_id, commit_id, path, parent, is_dir, added, deleted) "
                  "VALUES(?,?,?,?,?,?,?)")


def _parse_into(conn, repo_id: int, git_dir: str, resolved: str,
                missing: list[str], total: int) -> list[tuple[int, str, str]]:
    """Parse `missing` commits (full history when nothing is stored yet).

    Returns the raw author identities that were seen for the first time.
    """
    author_cache: dict[tuple[str, str], int] = {
        (r["name"], r["email"]): r["id"]
        for r in conn.execute("SELECT id, name, email FROM raw_authors WHERE repo_id=?", (repo_id,))
    }
    new_raws: list[tuple[int, str, str]] = []
    pending: list[tuple] = []
    done = 0
    total_parse = len(missing)
    full = total_parse == total
    iterator = (gitlog.iter_commits(git_dir, ref=resolved) if full
                else gitlog.iter_commits(git_dir, shas=missing))
    for sha, ts, name, email, subject, changes in iterator:
        done += 1
        if done % 400 == 0 or done == total_parse:
            try:
                update_repo(repo_id, progress=min(done / max(total_parse, 1), 0.98),
                            message=f"Parsing commits... {done:,}/{total_parse:,}")
            except sqlite3.OperationalError:
                pass  # a progress tick must never abort the ingest
        key = (name, email)
        author_id = author_cache.get(key)
        if author_id is None:
            conn.execute("INSERT OR IGNORE INTO raw_authors(repo_id, name, email) VALUES(?,?,?)",
                         (repo_id, name, email))
            row = conn.execute("SELECT id FROM raw_authors WHERE repo_id=? AND name=? AND email=?",
                               (repo_id, name, email)).fetchone()
            author_id = row["id"]
            author_cache[key] = author_id
            new_raws.append((author_id, name, email))
            # Commit eagerly: the write lock must not be held across the long
            # parse loop, or progress updates on the shared connection stall.
            conn.commit()
        add = 0
        dele = 0
        for _, a, d in changes:
            add += a
            dele += d
        pending.append((sha, ts, author_id, subject, add, dele, changes))
        if len(pending) >= 1200:
            _flush(conn, repo_id, pending)
            pending = []
    if pending:
        _flush(conn, repo_id, pending)
    return new_raws


def _flush(conn, repo_id: int, pending: list[tuple]) -> None:
    conn.executemany(_INSERT_COMMIT,
                     [(repo_id, p[0], p[1], p[2], p[3], p[4], p[5]) for p in pending])
    ids: dict[str, int] = {}
    shas = [p[0] for p in pending]
    for i in range(0, len(shas), 400):
        chunk = shas[i:i + 400]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT id, sha FROM commits WHERE repo_id=? AND sha IN ({marks})",
                                (repo_id, *chunk)):
            ids[row["sha"]] = row["id"]
    change_rows: list[tuple] = []
    for sha, _ts, _aid, _subject, _a, _d, changes in pending:
        commit_id = ids.get(sha)
        if commit_id is None:
            continue
        for path, add, dele in changes:
            parts = path.split("/")
            change_rows.append((repo_id, commit_id, path, "/".join(parts[:-1]), 0, add, dele))
            for i in range(len(parts) - 1, 0, -1):
                change_rows.append((repo_id, commit_id, "/".join(parts[:i]),
                                    "/".join(parts[:i - 1]), 1, add, dele))
            change_rows.append((repo_id, commit_id, "", "", 1, add, dele))
    if change_rows:
        conn.executemany(_INSERT_CHANGE, change_rows)
    conn.commit()


# ---------------------------------------------------------------------------
# reference management / lifecycle
# ---------------------------------------------------------------------------

def _require_repo(repo_id: int) -> dict:
    repo = db.query_one("SELECT * FROM repos WHERE id=?", (repo_id,))
    if not repo:
        raise ValueError("unknown repository")
    if repo["status"] not in ("ready", "error") or not repo["path"]:
        raise ValueError("repository is busy or unavailable")
    if not gitlog.is_repo(repo["path"]):
        raise ValueError("repository data is not available on disk anymore")
    return repo


def set_reference(repo_id: int, ref: str) -> None:
    repo = _require_repo(repo_id)
    ref = (ref or "HEAD").strip() or "HEAD"
    resolved = gitlog.resolve_ref(repo["path"], ref)
    if not resolved:
        raise ValueError(f"cannot resolve '{ref}' to a commit")

    def work() -> None:
        try:
            _parse_and_finalize(repo_id, repo["path"], ref, resolved)
        except Exception as exc:  # noqa: BLE001
            _fail(repo_id, exc)
    _run_in_slot(work)


def reanalyze(repo_id: int) -> None:
    repo = _require_repo(repo_id)
    ref = repo["ref"] or "HEAD"
    resolved = gitlog.resolve_ref(repo["path"], ref)
    if not resolved:
        raise ValueError(f"cannot resolve '{ref}' to a commit")

    def work() -> None:
        try:
            with db.transaction() as conn:
                conn.execute("DELETE FROM changes WHERE repo_id=?", (repo_id,))
                conn.execute("DELETE FROM commits WHERE repo_id=?", (repo_id,))
            _parse_and_finalize(repo_id, repo["path"], ref, resolved)
        except Exception as exc:  # noqa: BLE001
            _fail(repo_id, exc)
    _run_in_slot(work)


def delete_repo(repo_id: int) -> None:
    repo = db.query_one("SELECT id, path, status FROM repos WHERE id=?", (repo_id,))
    if not repo:
        raise ValueError("unknown repository")
    if repo["status"] in ("cloning", "parsing", "pending"):
        raise ValueError("repository is busy - try again once ingestion finishes")
    with db.transaction() as conn:
        conn.execute("DELETE FROM changes WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM group_members WHERE raw_id IN "
                     "(SELECT id FROM raw_authors WHERE repo_id=?)", (repo_id,))
        conn.execute("DELETE FROM author_groups WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM raw_authors WHERE repo_id=?", (repo_id,))
        conn.execute("DELETE FROM repos WHERE id=?", (repo_id,))
    _cleanup_files(repo["path"])
    metrics_mod.invalidate(repo_id)


def _cleanup_files(path_str: str) -> None:
    """Remove the on-disk copy - strictly limited to our data/repos folder."""
    if not path_str:
        return
    try:
        rel = Path(path_str).resolve().relative_to(REPOS_DIR.resolve())
    except (ValueError, OSError):
        return
    top = REPOS_DIR.resolve() / rel.parts[0]
    if top.is_dir() and top.resolve().is_relative_to(REPOS_DIR.resolve()):
        shutil.rmtree(top, ignore_errors=True)
