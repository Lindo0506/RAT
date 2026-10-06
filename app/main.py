"""FastAPI application for the Repo Analysis Tool: REST API + static dashboard."""
from __future__ import annotations

import csv
import io
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import authors as authors_mod
from . import gitlog
from . import ingest
from . import metrics
from .db import db

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_schema()
    db.execute("UPDATE repos SET status='error', phase='error', "
               "message='interrupted by server restart - use Re-analyze' "
               "WHERE status IN ('cloning','parsing','pending')")
    yield


app = FastAPI(title="Repo Analysis Tool", version="1.0", lifespan=lifespan)


@app.exception_handler(gitlog.GitError)
async def _git_error(_: Request, exc: gitlog.GitError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(ValueError)
async def _value_error(_: Request, exc: ValueError):
    return JSONResponse({"detail": str(exc) or "invalid request"}, status_code=400)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _repo_row(repo_id: int, require_ready: bool = False):
    repo = db.query_one("SELECT * FROM repos WHERE id=?", (repo_id,))
    if not repo:
        raise HTTPException(404, "unknown repository")
    if require_ready and repo["status"] != "ready":
        raise HTTPException(409, f"repository is not ready yet (status: {repo['status']})")
    return repo


def _repo_dict(repo_id: int) -> dict:
    row = db.query_one(
        """SELECT r.*, (SELECT COUNT(*) FROM commits c WHERE c.repo_id = r.id) commit_count
           FROM repos r WHERE r.id = ?""", (repo_id,))
    if not row:
        raise HTTPException(404, "unknown repository")
    return dict(row)


def _filters(request: Request) -> metrics.Filters:
    q = request.query_params
    commit_ids = None
    commits = q.get("commits")
    if commits is not None:
        commit_ids = []
        for part in commits.split(","):
            part = part.strip()
            if part:
                try:
                    commit_ids.append(int(part))
                except ValueError:
                    raise HTTPException(400, f"bad commit id: {part}")
    return metrics.Filters(
        ref=q.get("ref") or None,
        since=int(q["since"]) if q.get("since") else None,
        until=int(q["until"]) if q.get("until") else None,
        author_group=int(q["author_group"]) if q.get("author_group") else None,
        commit_ids=commit_ids,
    )


def _obj(request: Request) -> tuple[str, bool]:
    q = request.query_params
    path = q.get("path") or ""
    is_dir = (q.get("is_dir") or "1").lower() in ("1", "true", "yes")
    return path, is_dir


def _bucket(request: Request) -> str:
    bucket = (request.query_params.get("bucket") or "month").lower()
    return bucket if bucket in ("day", "week", "month") else "month"


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    git_version = gitlog.run(".", ["--version"]).stdout.decode().strip()
    return {"ok": True, "git": git_version}


@app.get("/api/repos")
def list_repos() -> dict:
    rows = db.query(
        """SELECT r.*, (SELECT COUNT(*) FROM commits c WHERE c.repo_id = r.id) commit_count
           FROM repos r ORDER BY r.id DESC""")
    return {"repos": [dict(row) for row in rows]}


@app.post("/api/repos/clone")
def clone_repo(payload: dict = Body(...)) -> dict:
    url = ingest.validate_clone_url(str(payload.get("url") or ""))
    name = ingest.slug(url, "repo")
    repo_id = ingest.create_repo(name, "url", url)
    ingest.ingest_clone(repo_id, url)
    return _repo_dict(repo_id)


@app.post("/api/repos/upload")
async def upload_repo(file: UploadFile) -> dict:
    filename = Path(file.filename or "upload.zip").name
    if not filename.lower().endswith(".zip"):
        raise HTTPException(400, "please upload a .zip archive of the repository (including its .git)")
    dest = ingest.UPLOADS_DIR / f"{uuid.uuid4().hex}_{filename}"
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            out.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "the uploaded file is empty")
    repo_id = ingest.create_repo(ingest.slug(Path(filename).stem, "upload"), "zip", filename)
    ingest.ingest_zip(repo_id, dest)
    return _repo_dict(repo_id)


@app.delete("/api/repos/{repo_id}")
def remove_repo(repo_id: int) -> dict:
    ingest.delete_repo(repo_id)
    return {"deleted": repo_id}


@app.post("/api/repos/{repo_id}/ref")
def set_reference(repo_id: int, payload: dict = Body(...)) -> dict:
    ref = str(payload.get("ref") or "").strip()
    if not ref:
        raise HTTPException(400, "missing ref")
    ingest.set_reference(repo_id, ref)
    return _repo_dict(repo_id)


@app.post("/api/repos/{repo_id}/reanalyze")
def reanalyze(repo_id: int) -> dict:
    ingest.reanalyze(repo_id)
    return _repo_dict(repo_id)


@app.get("/api/repos/{repo_id}/refs")
def repo_refs(repo_id: int) -> dict:
    repo = _repo_row(repo_id)
    if not repo["path"]:
        raise HTTPException(409, "repository is not available yet")
    info = gitlog.list_refs(repo["path"])
    return {**info, "active": repo["ref"], "active_sha": (repo["resolved_sha"] or "")[:12]}


# ---------------------------------------------------------------------------
# authors
# ---------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/authors")
def list_authors(repo_id: int) -> dict:
    _repo_row(repo_id)
    return {"groups": authors_mod.author_view(repo_id)}


@app.post("/api/repos/{repo_id}/authors/merge")
def merge_authors(repo_id: int, payload: dict = Body(...)) -> dict:
    raw_ids = payload.get("raw_ids") or []
    if not isinstance(raw_ids, list) or not raw_ids:
        raise HTTPException(400, "select at least one identity to merge")
    group_id = authors_mod.merge_raw_authors(
        repo_id, [int(r) for r in raw_ids],
        payload.get("display_name") or None, payload.get("email") or None)
    metrics.invalidate(repo_id)
    return {"group_id": group_id, "groups": authors_mod.author_view(repo_id)}


@app.post("/api/repos/{repo_id}/authors/unmerge")
def unmerge_authors(repo_id: int, payload: dict = Body(...)) -> dict:
    if payload.get("group_id") is not None:
        authors_mod.unmerge_group(int(payload["group_id"]))
    elif payload.get("raw_id") is not None:
        authors_mod.unmerge_member(int(payload["raw_id"]))
    else:
        raise HTTPException(400, "group_id or raw_id required")
    metrics.invalidate(repo_id)
    return {"groups": authors_mod.author_view(repo_id)}


@app.post("/api/repos/{repo_id}/authors/reapply_mailmap")
def reapply_mailmap(repo_id: int) -> dict:
    repo = _repo_row(repo_id)
    if not repo["path"]:
        raise HTTPException(409, "repository is not available yet")
    merged = authors_mod.reapply_mailmap(repo_id, repo["path"], repo["resolved_sha"] or repo["ref"])
    metrics.invalidate(repo_id)
    return {"merged": merged, "groups": authors_mod.author_view(repo_id)}


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/metrics")
def get_metrics(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    path, is_dir = _obj(request)
    return metrics.object_metrics(repo, _filters(request), path, is_dir)


@app.get("/api/repos/{repo_id}/children")
def get_children(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    path, _is_dir = _obj(request)
    return {"path": path, "children": metrics.children_metrics(repo, _filters(request), path)}


@app.get("/api/repos/{repo_id}/top_files")
def get_top_files(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    path, is_dir = _obj(request)
    sort = request.query_params.get("sort") or "churn"
    limit = min(int(request.query_params.get("limit") or 100), 1000)
    files = metrics.top_files(repo, _filters(request), path if is_dir else "", sort, limit)
    return {"sort": sort, "files": files}


@app.get("/api/repos/{repo_id}/authors_breakdown")
def get_authors_breakdown(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    path, is_dir = _obj(request)
    limit = min(int(request.query_params.get("limit") or 50), 500)
    return metrics.author_breakdown(repo, _filters(request), path, is_dir, limit)


@app.get("/api/repos/{repo_id}/timeline")
def get_timeline(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    path, is_dir = _obj(request)
    bucket = _bucket(request)
    return {"bucket": bucket, "buckets": metrics.timeline(repo, _filters(request), path, is_dir, bucket)}


@app.get("/api/repos/{repo_id}/overview")
def get_overview(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    return metrics.overview(repo, _filters(request), _bucket(request))


@app.get("/api/repos/{repo_id}/tree")
def get_tree(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    path, _is_dir = _obj(request)
    if path and path not in metrics.tree(repo)["dirs"]:
        raise HTTPException(404, f"no such directory: {path}")
    info = metrics.object_children(repo, path)
    return {"path": path, "dirs": info["dirs"], "files": info["files"]}


@app.get("/api/repos/{repo_id}/commits")
def get_commits(repo_id: int, request: Request) -> dict:
    repo = _repo_row(repo_id, require_ready=True)
    q = request.query_params
    path = q.get("path")
    is_dir = (q.get("is_dir") or "0").lower() in ("1", "true", "yes")
    return metrics.commits_page(
        repo, _filters(request),
        page=int(q.get("page") or 1),
        per=int(q.get("per") or 50),
        q=(q.get("q") or "").strip() or None,
        path=path if path is not None else None,
        is_dir=is_dir if path is not None else None,
    )


@app.get("/api/commits/{commit_id}")
def get_commit(commit_id: int) -> dict:
    row = db.query_one("SELECT repo_id FROM commits WHERE id=?", (commit_id,))
    if not row:
        raise HTTPException(404, "unknown commit")
    repo = _repo_row(row["repo_id"])
    detail = metrics.commit_detail(repo, commit_id)
    if not detail:
        raise HTTPException(404, "unknown commit")
    return detail


# ---------------------------------------------------------------------------
# CSV export (QoL)
# ---------------------------------------------------------------------------

@app.get("/api/repos/{repo_id}/export")
def export_csv(repo_id: int, request: Request) -> Response:
    repo = _repo_row(repo_id, require_ready=True)
    kind = (request.query_params.get("kind") or "files").lower()
    f = _filters(request)
    path, is_dir = _obj(request)
    buf = io.StringIO()
    writer = csv.writer(buf)
    if kind == "commits":
        writer.writerow(["sha", "date_utc", "author", "subject", "added", "removed"])
        page = 1
        from datetime import datetime, timezone
        while page * 500 <= 5000:
            data = metrics.commits_page(repo, f, page=page, per=500)
            for c in data["commits"]:
                writer.writerow([c["sha"], datetime.fromtimestamp(c["ts"], timezone.utc).isoformat(),
                                 c["author"], c["subject"], c["added"], c["removed"]])
            if data["total"] <= page * 500:
                break
            page += 1
    elif kind == "authors":
        writer.writerow(["author", "email", "identities", "origin", "commits", "churn"])
        for g in authors_mod.author_view(repo_id):
            writer.writerow([g["display_name"], g["email"], len(g["members"]), g["origin"],
                             g["commits"], g["churn"]])
    else:
        writer.writerow(["path", "added", "removed", "growth", "churn",
                         "modifications", "mod_frequency", "churn_rate"])
        for row in metrics.top_files(repo, f, path if is_dir else "", "churn", 5000):
            writer.writerow([row["path"], row["added"], row["removed"], row["growth"], row["churn"],
                             row["modifications"], round(row["mod_frequency"], 6),
                             round(row["churn_rate"], 6)])
    filename = f"{repo['name']}-{kind}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# dashboard (mounted last so /api/* wins)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="dashboard")
