"""The triage agent: given a new issue, retrieve context from the RAG index
and ask Gemini for a structured triage decision.

Shadow mode only -- this prints the decision, it does not post anywhere.
Wiring the output to an actual GitHub comment is step 5 (post_comment.py),
which only happens after a maintainer has agreed to it.

Requires GEMINI_API_KEY -- get a free key at https://aistudio.google.com/app/apikey
(verify the free-tier limits there too, they can change).

Usage:
    # Test against a real issue already in your local DB. This is a genuine
    # leave-one-out test: the issue is excluded from its own retrieval
    # results, so it can't just "find itself".
    python src/agent.py --number 6699

    # Or supply a hypothetical new issue directly:
    python src/agent.py --title "App crashes on startup" --body "KeyError in CSS parser"
"""

import argparse
import os
import re
import sqlite3
import sys
import time
from typing import Literal, Optional

import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

load_dotenv()


class TriageDecision(BaseModel):
    labels: list[str]
    likely_duplicate_of: int  # issue number, or 0 if no likely duplicate
    duplicate_confidence: Literal["none", "low", "medium", "high"]
    duplicate_reasoning: str
    priority: Literal["low", "medium", "high", "critical"]
    draft_response: str


def require_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print(
            "GEMINI_API_KEY is required. Get a free key at "
            "https://aistudio.google.com/app/apikey and add it to .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


def load_issue_from_db(db_path: str, number: int) -> tuple[str, str]:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT title, body FROM issues WHERE number = ?", (number,)).fetchone()
    conn.close()
    if not row:
        print(f"Issue #{number} not found in {db_path}. Check the number, or run ingest_issues.py first.", file=sys.stderr)
        sys.exit(1)
    return row[0] or "", row[1] or ""


def retrieve_context(
    chroma_dir: str, collection_name: str, issue_text: str,
    exclude_number: Optional[int], k_issues: int = 5, k_docs: int = 3,
):
    ef = embedding_functions.DefaultEmbeddingFunction()
    client = chromadb.PersistentClient(path=chroma_dir)
    collection = client.get_collection(collection_name, embedding_function=ef)

    issue_results = collection.query(
        query_texts=[issue_text],
        n_results=k_issues + 1,  # +1 headroom in case the query issue itself is in the corpus
        where={"type": "issue"},
    )
    similar_issues = []
    for doc, meta, dist in zip(
        issue_results["documents"][0], issue_results["metadatas"][0], issue_results["distances"][0]
    ):
        if exclude_number and meta.get("number") == exclude_number:
            continue  # leave-one-out: don't let an issue match itself
        similar_issues.append({
            "number": meta["number"], "title": meta["title"], "state": meta["state"],
            "url": meta["url"], "text": doc, "distance": dist,
        })
    similar_issues = similar_issues[:k_issues]

    doc_results = collection.query(query_texts=[issue_text], n_results=k_docs, where={"type": "doc"})
    doc_snippets = [
        {"path": meta["path"], "text": doc, "distance": dist}
        for doc, meta, dist in zip(doc_results["documents"][0], doc_results["metadatas"][0], doc_results["distances"][0])
    ]

    return similar_issues, doc_snippets


def build_prompt(title: str, body: str, similar_issues: list[dict], doc_snippets: list[dict]) -> str:
    issues_block = "\n\n".join(
        f"- Issue #{i['number']} [{i['state']}]: {i['title']}\n  {i['text'][:600]}"
        for i in similar_issues
    ) or "(no similar issues found)"

    docs_block = "\n\n".join(
        f"- {d['path']}:\n  {d['text'][:500]}"
        for d in doc_snippets
    ) or "(no relevant docs found)"

    return f"""You are triaging a new GitHub issue for the Textualize/textual project.

NEW ISSUE
Title: {title}
Body: {body}

SIMILAR PAST ISSUES (retrieved by semantic search, may or may not be true duplicates)
{issues_block}

RELEVANT DOCUMENTATION (retrieved by semantic search)
{docs_block}

Your job:
1. Suggest 1-4 labels for this issue (e.g. bug, enhancement, documentation, question).
2. Decide if this looks like a likely duplicate of one of the similar issues above.
   An OPEN duplicate is more actionable (can be consolidated right now) than a CLOSED
   one (already resolved, just worth citing). Only flag a duplicate if you're genuinely
   confident. If none of the issues above are a real duplicate, set duplicate_confidence
   to "none" and likely_duplicate_of to 0.
3. Assess priority (low/medium/high/critical) based on apparent severity and impact.
4. Draft a short, helpful first response a maintainer could send as-is or lightly edit.
   Reference a similar issue or doc section naturally if one is directly relevant.
   Keep it concise and genuinely useful, not generic boilerplate.
"""


def format_decision(decision: "TriageDecision", similar_issues: list[dict]) -> str:
    """Build the actual output -- this is what step 5 would eventually post to
    GitHub, not just a debug print.

    Always lists the raw retrieved candidates, regardless of what the LLM
    concluded about duplicates. This matters concretely: on issue #5225 during
    development, the true duplicate (#4955) was sitting at rank #2 in
    retrieval the whole time, but the LLM's own judgment still said "no
    duplicate found" -- likely because the connecting evidence relied on
    maintainer knowledge that isn't written in either issue's text at all, a
    gap no amount of prompt tuning can close. A maintainer skimming the raw
    candidate list can catch a case like that themselves, but only if the
    list is actually shown -- not silently dropped whenever the LLM is
    confident there's no duplicate. See README's Known Limitations.
    """
    lines = [
        f"**Suggested labels:** {', '.join(decision.labels)}",
        f"**Priority:** {decision.priority}",
    ]

    if decision.duplicate_confidence != "none":
        lines.append(
            f"**Possible duplicate:** #{decision.likely_duplicate_of} "
            f"(confidence: {decision.duplicate_confidence})"
        )
        lines.append(f"  Reasoning: {decision.duplicate_reasoning}")
    else:
        lines.append("**Possible duplicate:** none identified with confidence")

    lines.append("")
    lines.append(decision.draft_response)

    if similar_issues:
        lines.append("")
        lines.append("<details><summary>Related issues found by search (for maintainer review)</summary>")
        lines.append("")
        lines.extend(f"- #{i['number']} [{i['state']}] {i['title']}" for i in similar_issues)
        lines.append("")
        lines.append("</details>")

    return "\n".join(lines)


def _extract_retry_seconds(error: genai_errors.ClientError, default: float = 65.0) -> float:
    """Google's free-tier 429 spells out exactly how long to wait, e.g.
    '...Please retry in 48.460395643s.' -- use that instead of guessing."""
    match = re.search(r"retry in ([\d.]+)s", str(error.details))
    if match:
        return float(match.group(1)) + 2.0  # small buffer past the exact boundary
    return default


def _is_daily_quota_exhausted(error: genai_errors.ClientError) -> bool:
    """A per-day quota (resets in ~24h) is fundamentally different from a
    per-minute one (resets in under a minute) -- retrying the former for a
    few minutes is pure wasted time against something that can't resolve
    today. Google's error details include the specific quotaId, e.g.
    'GenerateRequestsPerDayPerProjectPerModel-FreeTier' vs '...PerMinute...'."""
    return "PerDay" in str(error.details)


def call_gemini_with_retry(client, model: str, prompt: str, max_retries: int = 5):
    """Two genuinely different transient conditions land here, and they need
    different handling:
    - 429 per-minute quota: resets fast, worth retrying with the exact delay
      Google's error message provides.
    - 429 per-day quota: doesn't reset for ~24h, retrying within this run is
      pure wasted time -- fail fast instead.
    - 503 (ServerError, e.g. "high demand, try again later"): genuinely
      transient server-side overload, not a quota issue at all -- worth
      retrying with a plain backoff, since Google gives no exact delay hint
      for this one the way it does for 429s.
    Anything else (4xx auth/config errors, etc.) isn't retried -- it won't
    resolve by waiting, so it's surfaced immediately instead of masked.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TriageDecision,
                ),
            )
        except genai_errors.APIError as e:
            if e.code == 429:
                if _is_daily_quota_exhausted(e):
                    raise RuntimeError(
                        "Gemini daily free-tier quota exhausted (resets on Google's ~24h "
                        "schedule, not something retrying within this run can fix)."
                    ) from e
                wait = _extract_retry_seconds(e)
                kind = "per-minute rate limit"
            elif e.code in (500, 503, 504):
                wait = min(15 * attempt, 60)  # simple backoff -- Google gives no exact delay for this one
                kind = "server overload (transient, per Google's own message)"
            else:
                raise  # not something retrying will fix -- surface it immediately

            last_error = e
            if attempt < max_retries:
                print(f"Gemini {kind} hit (attempt {attempt}/{max_retries}). Waiting {wait:.0f}s...", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"Gemini errors persisted after {max_retries} attempts") from last_error


def run_triage(
    title: str, body: str, chroma_dir: str, collection: str, model: str,
    api_key: str, exclude_number: Optional[int] = None,
):
    """Retrieval + Gemini call for one issue. Returns (decision, similar_issues, doc_snippets).

    This is the one place the actual triage logic lives -- both agent.py's
    CLI and watch_new_issues.py's polling loop call this, rather than each
    reimplementing the retrieve-then-prompt-then-call sequence separately.
    """
    similar_issues, doc_snippets = retrieve_context(
        chroma_dir, collection, f"{title}\n\n{body}", exclude_number
    )
    prompt = build_prompt(title, body, similar_issues, doc_snippets)

    client = genai.Client(api_key=api_key)
    response = call_gemini_with_retry(client, model, prompt)
    decision = TriageDecision.model_validate_json(response.text)
    return decision, similar_issues, doc_snippets


def main():
    parser = argparse.ArgumentParser(description="Run the triage agent on one issue (shadow mode -- prints only).")
    parser.add_argument("--number", type=int, help="Pull this issue number from the local DB (leave-one-out test)")
    parser.add_argument("--title", help="Or supply a hypothetical new issue title directly")
    parser.add_argument("--body", default="", help="Body text to go with --title")
    parser.add_argument("--db", default="data/triage.db")
    parser.add_argument("--chroma-dir", default="data/chroma")
    parser.add_argument("--collection", default="textual_corpus")
    parser.add_argument("--model", default="gemini-3.6-flash",
                         help="Verify current model names at ai.google.dev if this one goes stale")
    args = parser.parse_args()

    if args.number:
        title, body = load_issue_from_db(args.db, args.number)
        exclude_number = args.number
    elif args.title:
        title, body = args.title, args.body
        exclude_number = None
    else:
        parser.error("Provide either --number (test against a real DB issue) or --title (a hypothetical new issue)")

    print(f"Triaging: {title}\n")

    api_key = require_gemini_key()
    decision, similar_issues, doc_snippets = run_triage(
        title, body, args.chroma_dir, args.collection, args.model, api_key, exclude_number,
    )

    print("--- Retrieved context (debug only -- distances aren't meaningful to a maintainer) ---")
    for i in similar_issues:
        print(f"  similar issue #{i['number']} [{i['state']}] (distance={i['distance']:.3f}): {i['title']}")
    for d in doc_snippets:
        print(f"  doc (distance={d['distance']:.3f}): {d['path']}")

    print("\n--- Final output (what step 5 would eventually post to GitHub) ---")
    print(format_decision(decision, similar_issues))


if __name__ == "__main__":
    main()