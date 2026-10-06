"""Metric computation layer.

Definitions follow the COMS3011A brief exactly.  Because the `changes` table
stores, per commit, one row per changed file AND one row per ancestor
directory (the root being ''), every metric reduces to indexed aggregation:

    l+(H,o) = SUM(add)                     added lines
    l-(H,o) = SUM(del)                     removed lines
    delta   = SUM(add) - SUM(del)          growth
    lambda  = SUM(add) + SUM(del)          churn
    n(H,o)  = COUNT(DISTINCT commit_id)    modifications (rows exist only when
                                           the commit churn on the object > 0,
                                           matching the brief's indicator sum)
    eta     = n / |H|                      modification frequency
    rho     = lambda / |H|                 churn rate
    omega   = lambda_a / lambda            author ownership

A directory's metric is the sum over its whole subtree (the recursive
"immediate children" definition), the repository metric is the root directory.
H-bars are the non-merge commits reachable from the active reference; the
commit set H is that set filtered by time range and/or a manual selection.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import gitlog
from .db import db

_MISS = object()
_CACHE: dict = {}
_REF_CACHE: dict = {}
_last_fill = None


def invalidate(repo_id: int) -> None:
    """Drop every cached derived value for a repository."""
    global _last_fill
    _last_fill = None
    for cache in (_CACHE, _REF_CACHE):
        for key in [k for k in cache if k and k[0] == repo_id]:
            cache.pop(key, None)


def _cached(repo_id: int, prefix: str, rest: tuple, fn):
    key = (repo_id, prefix, rest)
    val = _CACHE.get(key, _MISS)
    if val is not _MISS:
        return val
    val = fn()
    if len(_CACHE) > 512:
        _CACHE.clear()
    _CACHE[key] = val
    return val


# ---------------------------------------------------------------------------
# commit-set selection
# ---------------------------------------------------------------------------

def ref_id_set(repo, ref: str | None):
    """Commit ids reachable from `ref`; None means "all stored commits"."""
    ref = (ref or repo["ref"] or "HEAD").strip()
    key = (repo["id"], repo["updated_at"], ref)
    if key in _REF_CACHE:
        return _REF_CACHE[key]
    resolved = gitlog.resolve_ref(repo["path"], ref)
    if not resolved:
        raise ValueError(f"cannot resolve reference '{ref}'")
    shas = gitlog.rev_list_no_merges(repo["path"], resolved)
    sha_to_id = {row["sha"]: row["id"] for row in
                 db.query("SELECT sha, id FROM commits WHERE repo_id=?", (repo["id"],))}
    ids = {sha_to_id[s] for s in shas if s in sha_to_id}
    result = None if ids and len(ids) >= len(sha_to_id) else frozenset(ids)
    if len(_REF_CACHE) > 64:
        _REF_CACHE.clear()
    _REF_CACHE[key] = result
    return result


def _fill_idset(conn, ids) -> None:
    global _last_fill
    # Identity check: cached ref sets are stable objects, manual sets are fresh,
    # and holding the reference prevents id() reuse from faking a match.
    if _last_fill is ids:
        return
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS cset_tmp(id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM cset_tmp")
    conn.executemany("INSERT OR IGNORE INTO cset_tmp(id) VALUES(?)", ((i,) for i in ids))
    _last_fill = ids


class Filters:
    """Dashboard filters shared by every metric query."""

    __slots__ = ("ref", "since", "until", "author_group", "commit_ids")

    def __init__(self, ref: str | None = None, since: int | None = None,
                 until: int | None = None, author_group: int | None = None,
                 commit_ids: list[int] | None = None):
        self.ref = ref
        self.since = int(since) if since is not None else None
        self.until = int(until) if until is not None else None
        self.author_group = int(author_group) if author_group is not None else None
        self.commit_ids = [int(c) for c in commit_ids] if commit_ids is not None else None

    def key(self) -> tuple:
        ids = self.commit_ids
        idkey = None if ids is None else (len(ids), hash(tuple(sorted(ids))))
        return (self.ref, self.since, self.until, self.author_group, idkey)

    def where(self, conn, repo, include_author: bool = True) -> tuple[list[str], list]:
        """SQL conditions on the `commits c` alias for this commit set H."""
        conds: list[str] = []
        params: list = []
        if self.since is not None:
            conds.append("c.ts >= ?")
            params.append(self.since)
        if self.until is not None:
            conds.append("c.ts < ?")
            params.append(self.until)
        if include_author and self.author_group is not None:
            conds.append("c.author_id IN (SELECT raw_id FROM group_members WHERE group_id = ?)")
            params.append(self.author_group)
        ids = (frozenset(self.commit_ids) if self.commit_ids is not None
               else ref_id_set(repo, self.ref))
        if ids is not None:
            _fill_idset(conn, ids)
            conds.append("c.id IN (SELECT id FROM cset_tmp)")
        return conds, params

    def hsize(self, conn, repo, include_author: bool = True) -> int:
        conds, params = self.where(conn, repo, include_author=include_author)
        sql = "SELECT COUNT(*) n FROM commits c WHERE c.repo_id = ?"
        for cond in conds:
            sql += " AND " + cond
        return conn.execute(sql, (repo["id"], *params)).fetchone()["n"]


def _frames(add: int, dele: int, mods: int, hsize: int) -> dict:
    growth = add - dele
    churn = add + dele
    return {
        "added": add, "removed": dele, "growth": growth, "churn": churn,
        "modifications": mods, "commits": hsize,
        "mod_frequency": (mods / hsize) if hsize else 0.0,
        "churn_rate": (churn / hsize) if hsize else 0.0,
    }


# ---------------------------------------------------------------------------
# object tree (for the file/directory browser)
# ---------------------------------------------------------------------------

def tree(repo) -> dict:
    """Immediate children map: parent -> {'dirs': [...], 'files': [...]}.

    Sources: every distinct path ever touched (so deleted files stay reachable)
    plus the current tree at the resolved reference (so untouched files show up
    with zero metrics).
    """
    def build() -> dict:
        files: set[str] = set()
        dirs: set[str] = set()
        for row in db.query("SELECT DISTINCT path, is_dir FROM changes WHERE repo_id=?",
                            (repo["id"],)):
            (dirs if row["is_dir"] else files).add(row["path"])
        try:
            files.update(gitlog.ls_tree_files(repo["path"], repo["resolved_sha"] or repo["ref"]))
        except (gitlog.GitError, TypeError, KeyError):
            pass
        for path in files:
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
        dirs.discard("")
        children: dict[str, dict[str, list[str]]] = {}

        def bucket(parent: str) -> dict:
            return children.setdefault(parent, {"dirs": [], "files": []})

        for path in dirs:
            bucket(path.rsplit("/", 1)[0] if "/" in path else "")["dirs"].append(path)
        for path in files:
            bucket(path.rsplit("/", 1)[0] if "/" in path else "")["files"].append(path)
        for entry in children.values():
            entry["dirs"].sort()
            entry["files"].sort()
        return {"children": children, "files": files, "dirs": dirs}

    return _cached(repo["id"], "tree", (repo["updated_at"],), build)


def object_children(repo, path: str) -> dict:
    info = tree(repo)
    return info["children"].get(path, {"dirs": [], "files": []})


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

_OBJ_COND = "ch.repo_id = ? AND ch.is_dir = ? AND ch.path = ?"


def object_metrics(repo, f: Filters, path: str, is_dir: bool,
                   include_author: bool = True) -> dict:
    """Every metric for one object (file or directory, root = '')."""

    def compute() -> dict:
        with db.session() as conn:
            conds, params = f.where(conn, repo, include_author=include_author)
            sql = (f"SELECT COALESCE(SUM(ch.added),0) added, COALESCE(SUM(ch.deleted),0) deleted, "
                   f"COUNT(DISTINCT ch.commit_id) n FROM changes ch "
                   f"JOIN commits c ON c.id = ch.commit_id "
                   f"WHERE {_OBJ_COND}")
            for cond in conds:
                sql += " AND " + cond
            row = conn.execute(sql, (repo["id"], 1 if is_dir else 0, path, *params)).fetchone()
            hsize = f.hsize(conn, repo, include_author=include_author)
        out = {"path": path, "is_dir": bool(is_dir)}
        out.update(_frames(row["added"], row["deleted"], row["n"], hsize))
        if include_author and f.author_group is not None:
            base = _object_metrics_uncached(repo, f, path, is_dir, include_author=False)
            out["ownership"] = (out["churn"] / base["churn"]) if base["churn"] else 0.0
            out["context_churn"] = base["churn"]
            out["context_added"] = base["added"]
            out["context_removed"] = base["removed"]
        return out

    return _cached(repo["id"], "om",
                   (repo["updated_at"], f.key(), path, is_dir, include_author), compute)


def _object_metrics_uncached(repo, f, path, is_dir, include_author) -> dict:
    with db.session() as conn:
        conds, params = f.where(conn, repo, include_author=include_author)
        sql = (f"SELECT COALESCE(SUM(ch.added),0) added, COALESCE(SUM(ch.deleted),0) deleted, "
               f"COUNT(DISTINCT ch.commit_id) n FROM changes ch "
               f"JOIN commits c ON c.id = ch.commit_id WHERE {_OBJ_COND}")
        for cond in conds:
            sql += " AND " + cond
        row = conn.execute(sql, (repo["id"], 1 if is_dir else 0, path, *params)).fetchone()
        hsize = f.hsize(conn, repo, include_author=include_author)
    out = {"path": path, "is_dir": bool(is_dir)}
    out.update(_frames(row["added"], row["deleted"], row["n"], hsize))
    return out


def children_metrics(repo, f: Filters, path: str) -> list[dict]:
    """Immediate children of `path` (from the tree) with their metrics."""

    def compute() -> list[dict]:
        info = object_children(repo, path)
        names = ([(d, True) for d in info["dirs"]] + [(p, False) for p in info["files"]])
        if not names:
            return []
        with db.session() as conn:
            conds, params = f.where(conn, repo)
            sql = (f"SELECT ch.is_dir d, ch.path p, COALESCE(SUM(ch.added),0) added, "
                   f"COALESCE(SUM(ch.deleted),0) deleted, COUNT(DISTINCT ch.commit_id) n "
                   f"FROM changes ch JOIN commits c ON c.id = ch.commit_id "
                   f"WHERE ch.repo_id = ? AND ch.parent = ?")
            for cond in conds:
                sql += " AND " + cond
            sql += " GROUP BY ch.is_dir, ch.path"
            rows = conn.execute(sql, (repo["id"], path, *params)).fetchall()
            hsize = f.hsize(conn, repo)
        found = {(r["p"], bool(r["d"])): r for r in rows}
        out = []
        for name, is_dir in names:
            row = found.get((name, is_dir))
            entry = {"path": name, "name": name.rsplit("/", 1)[-1], "is_dir": is_dir}
            entry.update(_frames(row["added"] if row else 0, row["deleted"] if row else 0,
                                 row["n"] if row else 0, hsize))
            out.append(entry)
        out.sort(key=lambda x: (not x["is_dir"], -x["churn"], x["name"]))
        return out

    return _cached(repo["id"], "children", (repo["updated_at"], f.key(), path), compute)


_SORTS = {
    "churn": "SUM(ch.added + ch.deleted) DESC",
    "growth": "SUM(ch.added - ch.deleted) DESC",
    "added": "SUM(ch.added) DESC",
    "removed": "SUM(ch.deleted) DESC",
    "modifications": "COUNT(DISTINCT ch.commit_id) DESC",
    "mod_frequency": "COUNT(DISTINCT ch.commit_id) DESC",
    "churn_rate": "SUM(ch.added + ch.deleted) DESC",
}


def top_files(repo, f: Filters, under: str = "", sort: str = "churn",
              limit: int = 100) -> list[dict]:
    """Files ranked by a metric, optionally under a directory subtree."""

    def compute() -> list[dict]:
        with db.session() as conn:
            conds, params = f.where(conn, repo)
            sql = (f"SELECT ch.path p, COALESCE(SUM(ch.added),0) added, COALESCE(SUM(ch.deleted),0) deleted, "
                   f"COUNT(DISTINCT ch.commit_id) n FROM changes ch "
                   f"JOIN commits c ON c.id = ch.commit_id "
                   f"WHERE ch.repo_id = ? AND ch.is_dir = 0")
            query_params: list = [repo["id"]]
            if under:
                sql += " AND ch.path >= ? AND ch.path < ?"
                query_params += [under + "/", under + "0"]
            for cond in conds:
                sql += " AND " + cond
            query_params += params
            order = _SORTS.get(sort, _SORTS["churn"])
            sql += f" GROUP BY ch.path ORDER BY {order}, ch.path LIMIT ?"
            rows = conn.execute(sql, (*query_params, limit)).fetchall()
            hsize = f.hsize(conn, repo)
        out = []
        for row in rows:
            entry = {"path": row["p"]}
            entry.update(_frames(row["added"], row["deleted"], row["n"], hsize))
            out.append(entry)
        return out

    return _cached(repo["id"], "topfiles", (repo["updated_at"], f.key(), under, sort, limit), compute)


def author_breakdown(repo, f: Filters, path: str, is_dir: bool, limit: int = 50) -> dict:
    """Per-author churn/modifications/ownership for one object.

    Ownership uses the context commit set (same filters but WITHOUT the author
    filter), so omega = lambda_a / lambda stays meaningful when a specific
    author is selected in the dashboard.
    """

    def compute() -> dict:
        with db.session() as conn:
            conds, params = f.where(conn, repo, include_author=False)
            sql = (f"SELECT g.id, g.display_name, g.email, g.origin, "
                   f"COALESCE(SUM(ch.added),0) added, COALESCE(SUM(ch.deleted),0) deleted, "
                   f"COUNT(DISTINCT ch.commit_id) n FROM changes ch "
                   f"JOIN commits c ON c.id = ch.commit_id "
                   f"JOIN group_members m ON m.raw_id = c.author_id "
                   f"JOIN author_groups g ON g.id = m.group_id WHERE {_OBJ_COND}")
            for cond in conds:
                sql += " AND " + cond
            sql += " GROUP BY g.id ORDER BY SUM(ch.added + ch.deleted) DESC, g.display_name LIMIT ?"
            rows = conn.execute(sql, (repo["id"], 1 if is_dir else 0, path, *params, limit)).fetchall()
            totals = conn.execute(
                f"SELECT COALESCE(SUM(ch.added),0) added, COALESCE(SUM(ch.deleted),0) deleted, "
                f"COUNT(DISTINCT ch.commit_id) n "
                f"FROM changes ch JOIN commits c ON c.id = ch.commit_id WHERE {_OBJ_COND}"
                + "".join(" AND " + cond for cond in conds),
                (repo["id"], 1 if is_dir else 0, path, *params)).fetchone()
            hsize = f.hsize(conn, repo, include_author=False)
            commit_rows = conn.execute(
                "SELECT m.group_id gid, COUNT(DISTINCT c.id) n FROM commits c "
                "JOIN group_members m ON m.raw_id = c.author_id WHERE c.repo_id = ?"
                + "".join(" AND " + cond for cond in conds),
                (repo["id"], *params)).fetchall()
        commits_by = {r["gid"]: r["n"] for r in commit_rows}
        total_churn = totals["added"] + totals["deleted"]
        authors = []
        for row in rows:
            churn = row["added"] + row["deleted"]
            authors.append({
                "group_id": row["id"], "name": row["display_name"], "email": row["email"],
                "origin": row["origin"], "added": row["added"], "removed": row["deleted"],
                "growth": row["added"] - row["deleted"], "churn": churn,
                "commits": commits_by.get(row["id"], 0),
                "modifications": row["n"],
                "mod_frequency": (row["n"] / hsize) if hsize else 0.0,
                "churn_rate": (churn / hsize) if hsize else 0.0,
                "ownership": (churn / total_churn) if total_churn else 0.0,
            })
        return {"total": _frames(totals["added"], totals["deleted"], totals["n"], hsize),
                "authors": authors}

    return _cached(repo["id"], "abreak",
                   (repo["updated_at"], f.key(), path, is_dir, limit), compute)


_BUCKETS = {
    "day": ("c.ts / 86400", lambda b: _date_label(int(b) * 86400)),
    "week": ("c.ts / 604800", lambda b: _date_label(int(b) * 604800)),
    "month": ("strftime('%Y-%m', c.ts, 'unixepoch')", lambda b: b),
}


def _date_label(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def timeline(repo, f: Filters, path: str = "", is_dir: bool = True,
             bucket: str = "month") -> list[dict]:
    """Added/removed lines and modifications per time bucket."""

    def compute() -> list[dict]:
        expr, label = _BUCKETS[bucket]
        with db.session() as conn:
            conds, params = f.where(conn, repo)
            sql = (f"SELECT {expr} b, COALESCE(SUM(ch.added),0) added, COALESCE(SUM(ch.deleted),0) deleted, "
                   f"COUNT(DISTINCT ch.commit_id) n FROM changes ch "
                   f"JOIN commits c ON c.id = ch.commit_id WHERE {_OBJ_COND}")
            for cond in conds:
                sql += " AND " + cond
            sql += " GROUP BY b ORDER BY b"
            rows = conn.execute(sql, (repo["id"], 1 if is_dir else 0, path, *params)).fetchall()
        return [{"bucket": label(row["b"]), "added": row["added"], "removed": row["deleted"],
                 "growth": row["added"] - row["deleted"], "churn": row["added"] + row["deleted"],
                 "modifications": row["n"]} for row in rows]

    return _cached(repo["id"], "timeline",
                   (repo["updated_at"], f.key(), path, is_dir, bucket), compute)


def overview(repo, f: Filters, bucket: str = "month") -> dict:
    """Everything the overview tab needs in one hop."""

    def compute() -> dict:
        info = tree(repo)
        metrics = object_metrics(repo, f, "", True)
        authors = author_breakdown(repo, f, "", True, limit=10)
        with db.session() as conn:
            conds, params = f.where(conn, repo)
            sql = ("SELECT COUNT(DISTINCT m.group_id) n FROM commits c "
                   "JOIN group_members m ON m.raw_id = c.author_id WHERE c.repo_id = ?")
            for cond in conds:
                sql += " AND " + cond
            contributors = conn.execute(sql, (repo["id"], *params)).fetchone()["n"]
        return {
            "metrics": metrics,
            "authors": authors,
            "timeline": timeline(repo, f, "", True, bucket),
            "children": children_metrics(repo, f, "")[:40],
            "top_files": top_files(repo, f, "", "churn", 10),
            "contributors": contributors,
            "files": len(info["files"]),
            "dirs": len(info["dirs"]),
        }

    return _cached(repo["id"], "overview", (repo["updated_at"], f.key(), bucket), compute)


# ---------------------------------------------------------------------------
# commits
# ---------------------------------------------------------------------------

def commits_page(repo, f: Filters, page: int = 1, per: int = 50,
                 q: str | None = None, path: str | None = None,
                 is_dir: bool | None = None) -> dict:
    page = max(1, page)
    per = max(1, min(per, 500))
    with db.session() as conn:
        conds, params = f.where(conn, repo)
        sql_where = "c.repo_id = ?"
        query_params: list = [repo["id"], *params]
        for cond in conds:
            sql_where += " AND " + cond
        if q:
            sql_where += " AND (c.sha LIKE ? OR c.subject LIKE ?)"
            query_params += [f"{q}%", f"%{q}%"]
        if path is not None and is_dir is not None:
            sql_where += (" AND EXISTS (SELECT 1 FROM changes ch WHERE ch.commit_id = c.id "
                          "AND ch.is_dir = ? AND ch.path = ?)")
            query_params += [1 if is_dir else 0, path]
        total = conn.execute(f"SELECT COUNT(*) n FROM commits c WHERE {sql_where}",
                             query_params).fetchone()["n"]
        rows = conn.execute(
            f"""SELECT c.id, c.sha, c.ts, c.subject, c.added AS added, c.deleted AS removed,
                       COALESCE(g.display_name, '(unknown)') author
                FROM commits c
                LEFT JOIN group_members m ON m.raw_id = c.author_id
                LEFT JOIN author_groups g ON g.id = m.group_id
                WHERE {sql_where}
                ORDER BY c.ts DESC, c.id DESC LIMIT ? OFFSET ?""",
            (*query_params, per, (page - 1) * per)).fetchall()
    return {"total": total, "page": page, "per": per,
            "commits": [dict(row) for row in rows]}


def commit_detail(repo, commit_id: int) -> dict | None:
    row = db.query_one(
        """SELECT c.id, c.sha, c.ts, c.subject, c.added AS added, c.deleted AS removed,
                  COALESCE(g.display_name, '(unknown)') author, COALESCE(g.email, '') email
           FROM commits c
           LEFT JOIN group_members m ON m.raw_id = c.author_id
           LEFT JOIN author_groups g ON g.id = m.group_id
           WHERE c.id = ? AND c.repo_id = ?""", (commit_id, repo["id"]))
    if not row:
        return None
    files = db.query(
        """SELECT path, added AS added, deleted AS removed FROM changes
           WHERE commit_id = ? AND is_dir = 0 ORDER BY (added + deleted) DESC, path""",
        (commit_id,))
    return {**dict(row), "files": [dict(f) for f in files]}
