"""Poll for newly created issues and run the triage agent on each one.

This is step 5's "trigger" -- but note the constraint that shapes this whole
file: we don't have webhook/admin access to Textualize/textual, since it
isn't our repo. GitHub's `on: issues: opened` only fires for events in the
repo containing the workflow file, so a real push-based trigger isn't
available to us here. Instead, this polls periodically for recently created
issues and checks each one against a local "already triaged" table -- a
scheduled Action, not an event-driven one.

Shadow mode by default, and this is a structural guarantee, not just a flag:
post_comment.py doesn't exist yet, so there is currently no code path in this
project that can post to GitHub at all, regardless of any environment
variable. Once a maintainer has actually agreed to it and post_comment.py
exists, posting will additionally require POST_TO_GITHUB=true to be set
explicitly -- so it can never start posting just because a flag got left on
by accident somewhere.

Usage:
    python src/watch_new_issues.py --repo Textualize/textual --lookback 20
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

load_dotenv()

import agent  # reuses retrieve_context, run_triage, format_decision, require_gemini_key

GRAPHQL_URL = "https://api.github.com/graphql"

RECENT_ISSUES_QUERY = """
query($owner: String!, $name: String!, $count: Int!) {
  repository(owner: $owner, name: $name) {
    issues(first: $count, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes {
        databaseId
        number
        title
        body
        state
        createdAt
      }
    }
  }
}
"""


def init_triaged_log(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triaged_log (
            number INTEGER PRIMARY KEY,
            triaged_at TEXT,
            labels TEXT,
            priority TEXT,
            duplicate_confidence TEXT,
            likely_duplicate_of INTEGER,
            output TEXT
        )
        """
    )
    conn.commit()
    return conn


def already_triaged(conn: sqlite3.Connection, number: int) -> bool:
    return conn.execute("SELECT 1 FROM triaged_log WHERE number = ?", (number,)).fetchone() is not None


def fetch_recent_issues(repo: str, token: str, count: int) -> list[dict]:
    owner, name = repo.split("/", 1)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "oss-triage-copilot"}
    resp = requests.post(
        GRAPHQL_URL,
        headers=headers,
        json={"query": RECENT_ISSUES_QUERY, "variables": {"owner": owner, "name": name, "count": count}},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL error: {payload['errors']}")
    return payload["data"]["repository"]["issues"]["nodes"]


def record_triage(conn: sqlite3.Connection, number: int, decision, output: str) -> None:
    conn.execute(
        """
        INSERT INTO triaged_log
            (number, triaged_at, labels, priority, duplicate_confidence, likely_duplicate_of, output)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            number, datetime.now(timezone.utc).isoformat(), json.dumps(decision.labels),
            decision.priority, decision.duplicate_confidence, decision.likely_duplicate_of, output,
        ),
    )
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Poll for new issues and triage them (shadow mode).")
    parser.add_argument("--repo", default="Textualize/textual")
    parser.add_argument("--db", default="data/triage.db")
    parser.add_argument("--chroma-dir", default="data/chroma")
    parser.add_argument("--collection", default="textual_corpus")
    parser.add_argument("--lookback", type=int, default=20,
                         help="How many of the most recent issues to check each run")
    parser.add_argument("--model", default="gemini-3.6-flash")
    args = parser.parse_args()

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        print("GITHUB_TOKEN is required to check for new issues.", file=sys.stderr)
        sys.exit(1)
    api_key = agent.require_gemini_key()

    conn = init_triaged_log(args.db)
    recent = fetch_recent_issues(args.repo, github_token, args.lookback)
    new_issues = [n for n in recent if not already_triaged(conn, n["number"])]

    print(f"Checked {len(recent)} most recent issues, {len(new_issues)} not yet triaged.")
    if not new_issues:
        print("Nothing new to triage.")
        conn.close()
        return

    for i, node in enumerate(new_issues):
        title, body = node.get("title") or "", node.get("body") or ""
        print(f"\n=== Triaging new issue #{node['number']}: {title} ===")

        try:
            decision, similar_issues, _ = agent.run_triage(
                title, body, args.chroma_dir, args.collection, args.model, api_key,
                exclude_number=node["number"],
            )
        except RuntimeError as e:
            print(f"Could not triage #{node['number']}: {e}", file=sys.stderr)
            if "daily" in str(e).lower():
                remaining = len(new_issues) - i - 1
                print(
                    f"Daily quota exhausted -- stopping here rather than failing on every "
                    f"remaining issue one by one. {remaining} issue(s) left unprocessed this "
                    f"run will be picked up automatically on a future poll, since they were "
                    f"never added to triaged_log.",
                    file=sys.stderr,
                )
                break
            continue  # some other persistent failure specific to this one issue -- skip, keep going

        output = agent.format_decision(decision, similar_issues)
        print(output)

        record_triage(conn, node["number"], decision, output)

        if os.environ.get("POST_TO_GITHUB", "").lower() == "true":
            print("POST_TO_GITHUB is set, but post_comment.py doesn't exist yet -- nothing was posted.")
        else:
            print("(shadow mode -- not posted)")

        if i < len(new_issues) - 1:
            time.sleep(13)  # ~5 req/min free tier -- pace proactively, don't just rely on retry-after-failure

    conn.close()


if __name__ == "__main__":
    main()