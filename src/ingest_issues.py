"""Pull GitHub issues into a local SQLite DB via the GraphQL API.

Uses GraphQL with cursor-based pagination rather than the REST issues
endpoint, for two reasons that matter once a repo has more than ~1000 issues:

1. REST's offset-style pagination (?page=11) hits a hard wall around 1000
   results -- GitHub returns 422 past that point, regardless of sort order.
   Cursor pagination has no such cap.
2. REST sorted by `updated` can silently duplicate or skip items if issues
   change while you're mid-fetch, since the sort order shifts under you.
   This query sorts by `createdAt`, which never changes after creation, so
   pagination stays stable even on a slow, multi-minute pull.

Requires GITHUB_TOKEN -- unlike REST's 60 req/hour unauthenticated quota,
GraphQL has effectively no unauthenticated quota at all.

Usage:
    python src/ingest_issues.py --repo Textualize/textual --state all
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()  # reads .env into the environment -- works the same on Windows, Mac, Linux

GRAPHQL_URL = "https://api.github.com/graphql"

ISSUES_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        databaseId
        number
        title
        body
        state
        labels(first: 20) { nodes { name } }
        createdAt
        updatedAt
        closedAt
        comments { totalCount }
        url
      }
    }
  }
}
"""


def init_db(db_path: str) -> sqlite3.Connection:
    dirname = os.path.dirname(db_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY,
            number INTEGER NOT NULL,
            title TEXT,
            body TEXT,
            state TEXT,
            labels TEXT,
            created_at TEXT,
            updated_at TEXT,
            closed_at TEXT,
            comments_count INTEGER,
            html_url TEXT,
            fetched_at TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_number ON issues(number)")
    conn.commit()
    return conn


def require_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "GITHUB_TOKEN is required for this script -- GraphQL has no usable "
            "unauthenticated quota. Set it in .env (see .env.example) and load it "
            "before running.",
            file=sys.stderr,
        )
        sys.exit(1)
    return token


def fetch_issues(repo: str, token: str, max_pages: int | None = None):
    """Yield raw GraphQL issue nodes, walking pages via cursor (not offset)."""
    owner, name = repo.split("/", 1)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "oss-triage-copilot"}
    cursor = None
    page = 0

    while True:
        resp = requests.post(
            GRAPHQL_URL,
            headers=headers,
            json={"query": ISSUES_QUERY, "variables": {"owner": owner, "name": name, "cursor": cursor}},
            timeout=30,
        )
        if resp.status_code == 502:
            print("GitHub returned 502 (transient), retrying in 5s...", file=sys.stderr)
            time.sleep(5)
            continue
        resp.raise_for_status()

        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"GraphQL error: {payload['errors']}")

        issues_conn = payload["data"]["repository"]["issues"]
        for node in issues_conn["nodes"]:
            yield node

        page += 1
        if max_pages and page >= max_pages:
            break

        page_info = issues_conn["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        time.sleep(0.2)  # be polite even though the quota is generous


def upsert_issue(conn: sqlite3.Connection, node: dict, state_filter: str) -> bool:
    state = (node.get("state") or "").lower()  # GraphQL returns "OPEN"/"CLOSED"
    if state_filter != "all" and state != state_filter:
        return False

    labels = json.dumps([label["name"] for label in node.get("labels", {}).get("nodes", [])])
    conn.execute(
        """
        INSERT INTO issues
            (id, number, title, body, state, labels, created_at, updated_at, closed_at, comments_count, html_url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, body=excluded.body, state=excluded.state, labels=excluded.labels,
            updated_at=excluded.updated_at, closed_at=excluded.closed_at,
            comments_count=excluded.comments_count, fetched_at=excluded.fetched_at
        """,
        (
            node["databaseId"], node["number"], node.get("title"), node.get("body"), state,
            labels, node.get("createdAt"), node.get("updatedAt"), node.get("closedAt"),
            node.get("comments", {}).get("totalCount"), node.get("url"),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def main():
    parser = argparse.ArgumentParser(description="Ingest GitHub issues into SQLite via GraphQL (no 1000-item cap).")
    parser.add_argument("--repo", default="Textualize/textual", help="owner/repo")
    parser.add_argument("--state", default="all", choices=["open", "closed", "all"])
    parser.add_argument("--db", default="data/triage.db")
    parser.add_argument("--max-pages", type=int, default=None, help="Limit pages, useful for a quick local test")
    args = parser.parse_args()

    token = require_token()
    conn = init_db(args.db)
    ingested, filtered_out = 0, 0

    for node in fetch_issues(args.repo, token, max_pages=args.max_pages):
        if upsert_issue(conn, node, args.state):
            ingested += 1
        else:
            filtered_out += 1
        if ingested and ingested % 100 == 0:
            conn.commit()
            print(f"...{ingested} issues so far")

    conn.commit()
    conn.close()
    suffix = f", filtered out {filtered_out} by state" if filtered_out else ""
    print(f"Done. Ingested {ingested} issues{suffix}.")


if __name__ == "__main__":
    main()