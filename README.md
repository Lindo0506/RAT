# RAT - Repo Analysis Tool

A web application for analysing git repositories: clone a repo from a URL (or upload a
`.zip` of one), and explore its history through an interactive dashboard - churn, growth,
timelines, author identity management and drill-downs per file or directory.

## Quick start

Requirements:

- Python 3.10+ (tested on 3.12)
- `git` on your PATH

One command:

```bash
./start.sh
```

The script creates a virtualenv, installs the dependencies and starts the server.
Then open **http://localhost:8000** in your browser.

Use a different port with:

```bash
PORT=9000 ./start.sh
```

### Manual setup (alternative)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py                # or: python run.py --port 8000
```

## Using the dashboard

1. Click **+ Add repository** and either:
   - **Clone** - paste a public git URL (e.g. `https://github.com/DaveGamble/cJSON.git`), or
   - **Upload** - attach a `.zip` archive of a repository (must include its `.git` directory).
2. Wait for the ingest to finish (cloning / parsing progress is shown per repo).
3. Explore the metrics:
   - **Overview** - commit activity timeline (day / week / month buckets), totals.
   - **Files** - churn, growth, modification frequency per file or directory, with drill-down.
   - **Authors** - raw `(name, email)` identities, merge duplicates, apply `.mailmap`.
   - **Commits** - searchable, paginated commit browser with per-commit diffs.
   - Filters everywhere: reference (branch/tag/sha), date range, author group, custom commit set.
4. Export any view to **CSV** from the UI.

All data is stored locally in a SQLite database (`data/rat.db`), created automatically on
first run. `data/` is machine-specific and git-ignored.

## Project layout

```
run.py            entry point (uvicorn)
start.sh          one-command startup script
requirements.txt  Python dependencies (FastAPI, uvicorn, python-multipart)
app/              FastAPI app, git parsing, ingest, metrics, SQLite storage
static/           dashboard frontend (vanilla JS/CSS, no build step)
data/             runtime data: SQLite DB + cloned/uploaded repos (git-ignored)
```

## API

The dashboard is a static SPA backed by a JSON REST API (mounted under `/api/*`).
A few useful endpoints:

- `GET /api/health` - server + git availability
- `GET /api/repos` - list analysed repositories
- `POST /api/repos/clone` - clone and analyse a git URL
- `POST /api/repos/upload` - upload a `.zip` repository archive
- `GET /api/repos/{id}/overview` - headline metrics + timeline
- `GET /api/repos/{id}/export?kind=files|commits|authors` - CSV export
