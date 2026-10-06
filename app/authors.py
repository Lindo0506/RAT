"""Author identity handling: mailmap resolution + manual author merging.

Each commit stores a RAW identity (name, email).  Every raw identity belongs
to exactly one canonical `author_group`.  Groups are created:

  * automatically from the repository's `.mailmap` (origin='mailmap') when the
    raw identity is first seen,
  * one per raw identity otherwise (origin='raw'),
  * manually through the dashboard (origin='manual'), which can merge any
    selection of raw identities - even without a mailmap.
"""
from __future__ import annotations

from . import gitlog
from .db import db


# ---------------------------------------------------------------------------
# automatic grouping (mailmap)
# ---------------------------------------------------------------------------

def ensure_groups(repo_id: int, git_dir: str, ref: str,
                  new_raws: list[tuple[int, str, str]]) -> int:
    """Create canonical groups for raw identities that have none yet.

    Returns the number of identities that were merged by the mailmap.
    """
    if not new_raws:
        return 0
    mapping = gitlog.check_mailmap(git_dir, ref, [(n, e) for _, n, e in new_raws])
    by_canon: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for raw_id, name, email in new_raws:
        canon = mapping.get((name, email), (name, email))
        by_canon.setdefault(canon, []).append((raw_id, name, email))

    merged = 0
    with db.transaction() as conn:
        for (cname, cemail), members in by_canon.items():
            changed = any((n, e) != (cname, cemail) for _, n, e in members)
            row = conn.execute(
                "SELECT id FROM author_groups WHERE repo_id=? AND display_name=? AND email=?",
                (repo_id, cname, cemail)).fetchone()
            if row:
                group_id = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO author_groups(repo_id, display_name, email, origin) VALUES(?,?,?,?)",
                    (repo_id, cname or "Unknown", cemail, "mailmap" if changed else "raw"))
                group_id = cur.lastrowid
            if changed:
                merged += len(members)
            for raw_id, _, _ in members:
                conn.execute(
                    "INSERT OR IGNORE INTO group_members(group_id, raw_id) VALUES(?,?)",
                    (group_id, raw_id))
    return merged


def reapply_mailmap(repo_id: int, git_dir: str, ref: str) -> int:
    """Re-run mailmap grouping for every non-manual membership (QoL action)."""
    rows = db.query(
        """SELECT m.raw_id, r.name, r.email
           FROM group_members m
           JOIN raw_authors r ON r.id = m.raw_id
           JOIN author_groups g ON g.id = m.group_id
           WHERE r.repo_id = ? AND g.origin != 'manual'""", (repo_id,))
    raws = [(row["raw_id"], row["name"], row["email"]) for row in rows]
    if not raws:
        return 0
    raw_ids = [r[0] for r in raws]
    with db.transaction() as conn:
        q = ",".join("?" * len(raw_ids))
        conn.execute(f"DELETE FROM group_members WHERE raw_id IN ({q})", raw_ids)
    merged = ensure_groups(repo_id, git_dir, ref, raws)
    _drop_empty_groups(repo_id)
    return merged


# ---------------------------------------------------------------------------
# manual merging
# ---------------------------------------------------------------------------

def _drop_empty_groups(repo_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            """DELETE FROM author_groups WHERE repo_id = ? AND id NOT IN
               (SELECT DISTINCT group_id FROM group_members)""", (repo_id,))


def _private_group(conn, repo_id: int, raw_id: int, name: str, email: str) -> int:
    cur = conn.execute(
        "INSERT INTO author_groups(repo_id, display_name, email, origin) VALUES(?,?,?,'raw')",
        (repo_id, name or "Unknown", email))
    conn.execute("INSERT OR IGNORE INTO group_members(group_id, raw_id) VALUES(?,?)",
                 (cur.lastrowid, raw_id))
    return cur.lastrowid


def merge_raw_authors(repo_id: int, raw_ids: list[int],
                      display_name: str | None = None, email: str | None = None) -> int:
    """Merge raw identities into one manual group. Returns the group id."""
    raw_ids = list(dict.fromkeys(raw_ids))
    if not raw_ids:
        raise ValueError("no identities selected")
    q = ",".join("?" * len(raw_ids))
    rows = db.query(f"SELECT id, name, email FROM raw_authors WHERE repo_id=? AND id IN ({q})",
                    (repo_id, *raw_ids))
    if len(rows) != len(raw_ids):
        raise ValueError("some identities do not belong to this repository")

    with db.transaction() as conn:
        if display_name is None or email is None:
            best = conn.execute(
                f"""SELECT r.name, r.email, COUNT(c.id) n
                    FROM raw_authors r LEFT JOIN commits c ON c.author_id = r.id
                    WHERE r.id IN ({q})
                    GROUP BY r.id ORDER BY n DESC, r.id LIMIT 1""", raw_ids).fetchone()
            display_name = display_name or (best["name"] if best else rows[0]["name"])
            email = email or (best["email"] if best else rows[0]["email"])
        cur = conn.execute(
            "INSERT INTO author_groups(repo_id, display_name, email, origin) VALUES(?,?,?,'manual')",
            (repo_id, display_name, email))
        group_id = cur.lastrowid
        conn.execute(f"DELETE FROM group_members WHERE raw_id IN ({q})", raw_ids)
        conn.executemany("INSERT INTO group_members(group_id, raw_id) VALUES(?,?)",
                         [(group_id, rid) for rid in raw_ids])
        conn.execute(
            """DELETE FROM author_groups WHERE repo_id = ? AND id != ? AND id NOT IN
               (SELECT DISTINCT group_id FROM group_members)""", (repo_id, group_id))
    return group_id


def unmerge_group(group_id: int) -> None:
    """Dissolve a group: every member becomes its own identity again."""
    group = db.query_one("SELECT id, repo_id FROM author_groups WHERE id=?", (group_id,))
    if not group:
        raise ValueError("unknown author group")
    members = db.query(
        """SELECT r.id raw_id, r.name, r.email FROM group_members m
           JOIN raw_authors r ON r.id = m.raw_id WHERE m.group_id=?""", (group_id,))
    with db.transaction() as conn:
        conn.execute("DELETE FROM group_members WHERE group_id=?", (group_id,))
        conn.execute("DELETE FROM author_groups WHERE id=?", (group_id,))
        for member in members:
            _private_group(conn, group["repo_id"], member["raw_id"], member["name"], member["email"])


def unmerge_member(raw_id: int) -> None:
    """Pull a single raw identity out of its (multi-member) group."""
    row = db.query_one(
        """SELECT m.group_id, r.repo_id, r.name, r.email FROM group_members m
           JOIN raw_authors r ON r.id = m.raw_id WHERE m.raw_id=?""", (raw_id,))
    if not row:
        raise ValueError("unknown identity")
    size = db.query_one("SELECT COUNT(*) n FROM group_members WHERE group_id=?",
                        (row["group_id"],))["n"]
    if size <= 1:
        return
    with db.transaction() as conn:
        conn.execute("DELETE FROM group_members WHERE raw_id=?", (raw_id,))
        _private_group(conn, row["repo_id"], raw_id, row["name"], row["email"])


def author_view(repo_id: int) -> list[dict]:
    """Canonical authors + their raw identities + commit/churn totals."""
    groups = db.query(
        "SELECT id, display_name, email, origin FROM author_groups WHERE repo_id=?",
        (repo_id,))
    members = db.query(
        """SELECT m.group_id, r.id raw_id, r.name, r.email,
                  COUNT(c.id) AS commits, COALESCE(SUM(c.added + c.deleted), 0) AS churn
           FROM group_members m
           JOIN raw_authors r ON r.id = m.raw_id
           LEFT JOIN commits c ON c.author_id = r.id
           WHERE r.repo_id = ?
           GROUP BY r.id""", (repo_id,))
    out = []
    by_group: dict[int, list] = {}
    for m in members:
        by_group.setdefault(m["group_id"], []).append({
            "raw_id": m["raw_id"], "name": m["name"], "email": m["email"],
            "commits": m["commits"], "churn": m["churn"],
        })
    for g in groups:
        ms = by_group.get(g["id"], [])
        out.append({
            "id": g["id"], "display_name": g["display_name"], "email": g["email"],
            "origin": g["origin"], "members": ms,
            "commits": sum(m["commits"] for m in ms),
            "churn": sum(m["churn"] for m in ms),
        })
    out.sort(key=lambda g: (-g["commits"], g["display_name"].lower()))
    return out
