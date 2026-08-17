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

Resumable: progress is checkpointed to <db>.cursor.json after every page.
If a run is interrupted (including by GitHub's secondary rate limiting),
rerunning the exact same command picks up where it left off instead of
starting over. The checkpoint is cleared automatically once a full pull
completes. (Checkpointing is skipped when --max-pages is set, since that
flag is meant for quick smoke tests, not partial progress toward a full
pull.)

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


def checkpoint_path(db_path: str) -> str:
    return f"{db_path}.cursor.json"


def load_checkpoint(db_path: str, repo: str) -> str | None:
    path = checkpoint_path(db_path)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        state = json.load(f)
    if state.get("repo") != repo:
        return None  # different repo than last time -- don't reuse a stale cursor
    return state.get("cursor")


def save_checkpoint(db_path: str, repo: str, cursor: str) -> None:
    with open(checkpoint_path(db_path), "w") as f:
        json.dump({"repo": repo, "cursor": cursor}, f)


def clear_checkpoint(db_path: str) -> None:
    path = checkpoint_path(db_path)
    if os.path.exists(path):
        os.remove(path)


def fetch_issue_pages(repo: str, token: str, start_cursor: str | None = None, max_pages: int | None = None):
    """Yield (nodes, end_cursor, has_next_page) once per page.

    Handles both GitHub's primary rate limit (hourly quota exhausted) and
    secondary rate limit (too many requests too quickly -- the more likely
    cause on Actions runners, which share heavily-used IP ranges) by backing
    off and retrying rather than crashing.
    """
    owner, name = repo.split("/", 1)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "oss-triage-copilot"}
    cursor = start_cursor
    page = 0

    while True:
        resp = requests.post(
            GRAPHQL_URL,
            headers=headers,
            json={"query": ISSUES_QUERY, "variables": {"owner": owner, "name": name, "cursor": cursor}},
            timeout=30,
        )

        if resp.status_code in (403, 429):
            retry_after = resp.headers.get("Retry-After")
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")

            if retry_after:
                wait = int(retry_after) + 2
                print(f"Secondary rate limit hit (status {resp.status_code}). Body: {resp.text[:300]}", file=sys.stderr)
                print(f"Waiting {wait}s (from Retry-After header) before retrying...", file=sys.stderr)
                time.sleep(wait)
                continue

            if remaining == "0" and reset:
                wait = max(int(reset) - int(time.time()), 0) + 5
                print(f"Rate limit exhausted. Waiting {wait}s until reset...", file=sys.stderr)
                time.sleep(wait)
                continue

            # Not a pattern we recognize -- surface the real body instead of a bare traceback
            print(f"Unexpected {resp.status_code} with no Retry-After/reset header. Body: {resp.text[:500]}", file=sys.stderr)

        if resp.status_code == 502:
            print("GitHub returned 502 (transient), retrying in 5s...", file=sys.stderr)
            time.sleep(5)
            continue

        resp.raise_for_status()

        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(f"GraphQL error: {payload['errors']}")

        issues_conn = payload["data"]["repository"]["issues"]
        page_info = issues_conn["pageInfo"]
        page += 1
        yield issues_conn["nodes"], page_info["endCursor"], page_info["hasNextPage"]

        if max_pages and page >= max_pages:
            break
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
        time.sleep(0.5)  # a little more conservative than before -- reduces secondary rate-limit risk


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
    use_checkpoint = args.max_pages is None

    resume_cursor = load_checkpoint(args.db, args.repo) if use_checkpoint else None
    if resume_cursor:
        print("Found a checkpoint from an interrupted run -- resuming from there instead of page 1.", file=sys.stderr)

    ingested, filtered_out = 0, 0
    final_has_next = True

    for nodes, end_cursor, has_next in fetch_issue_pages(args.repo, token, start_cursor=resume_cursor, max_pages=args.max_pages):
        for node in nodes:
            if upsert_issue(conn, node, args.state):
                ingested += 1
            else:
                filtered_out += 1
        conn.commit()

        if use_checkpoint:
            save_checkpoint(args.db, args.repo, end_cursor)

        print(f"...{ingested} issues so far" + (" (checkpoint saved)" if use_checkpoint else ""))
        final_has_next = has_next
        if not has_next:
            break

    conn.close()
    suffix = f", filtered out {filtered_out} by state" if filtered_out else ""

    if use_checkpoint and not final_has_next:
        clear_checkpoint(args.db)
        print(f"Done. Ingested {ingested} issues{suffix}. Full pull complete, checkpoint cleared.")
    elif use_checkpoint:
        print(f"Stopped partway through (rerun the same command to resume). Ingested {ingested} issues{suffix} this run.")
    else:
        print(f"Done (smoke test, --max-pages set). Ingested {ingested} issues{suffix}.")


if __name__ == "__main__":
    main()