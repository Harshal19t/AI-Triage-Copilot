"""Pull a repo's markdown documentation into the same SQLite DB, for the RAG corpus.

Usage:
    python src/ingest_docs.py --repo Textualize/textual --docs-path docs --db data/triage.db
"""

import argparse
import os
import sqlite3
from datetime import datetime, timezone

import requests

GITHUB_API = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"


def github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "oss-triage-copilot"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def init_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS docs (
            path TEXT PRIMARY KEY,
            content TEXT,
            sha TEXT,
            fetched_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_default_branch(repo: str) -> str:
    resp = requests.get(f"{GITHUB_API}/repos/{repo}", headers=github_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["default_branch"]


def list_markdown_files(repo: str, branch: str, docs_path: str) -> list[dict]:
    """One API call gets the whole file tree, which is cheaper than walking directories."""
    resp = requests.get(
        f"{GITHUB_API}/repos/{repo}/git/trees/{branch}",
        headers=github_headers(),
        params={"recursive": "1"},
        timeout=30,
    )
    resp.raise_for_status()
    tree = resp.json().get("tree", [])
    return [
        item for item in tree
        if item["type"] == "blob"
        and item["path"].startswith(docs_path)
        and item["path"].endswith((".md", ".mdx"))
    ]


def fetch_raw(repo: str, branch: str, path: str) -> str:
    # raw.githubusercontent.com doesn't count against the api.github.com rate limit
    resp = requests.get(f"{RAW_BASE}/{repo}/{branch}/{path}", timeout=30)
    resp.raise_for_status()
    return resp.text


def main():
    parser = argparse.ArgumentParser(description="Ingest a repo's markdown docs into SQLite for RAG.")
    parser.add_argument("--repo", default="Textualize/textual")
    parser.add_argument("--docs-path", default="docs")
    parser.add_argument("--db", default="data/triage.db")
    args = parser.parse_args()

    conn = init_db(args.db)
    branch = get_default_branch(args.repo)
    files = list_markdown_files(args.repo, branch, args.docs_path)
    print(f"Found {len(files)} markdown files under '{args.docs_path}' on branch '{branch}'")

    for f in files:
        content = fetch_raw(args.repo, branch, f["path"])
        conn.execute(
            """
            INSERT INTO docs (path, content, sha, fetched_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET content=excluded.content, sha=excluded.sha, fetched_at=excluded.fetched_at
            """,
            (f["path"], content, f["sha"], datetime.now(timezone.utc).isoformat()),
        )

    conn.commit()
    conn.close()
    print(f"Done. Ingested {len(files)} doc files.")


if __name__ == "__main__":
    main()
