# OSS triage copilot — for Textualize/textual

An AI system that watches new issues on [Textualize/textual](https://github.com/Textualize/textual)
and drafts a triage suggestion: likely labels, similar past issues, a first-response
draft grounded in the docs, and a priority flag. Runs entirely on free infrastructure.

**Status: step 2 of 7 — data pipeline.** See the roadmap at the bottom.

Currently in **shadow mode**: it reads and reasons about issues but does not post
anything to GitHub yet. That comes later, once there are real accuracy numbers to
show the maintainers before asking permission to go live.

## What's here so far

- `src/ingest_issues.py` — pulls all issues (open + closed) from the repo into SQLite
- `src/ingest_docs.py` — pulls the markdown docs into the same database, as the
  corpus the RAG layer will retrieve from
- `.github/workflows/nightly-ingest.yml` — runs both scripts every night and commits
  the updated database, so the corpus stays current with zero servers to maintain

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Get a GitHub token (no scopes needed, just raises your rate limit from 60 to 5000
requests/hour): https://github.com/settings/tokens — paste it into `.env` as
`GITHUB_TOKEN=...`, then load it before running scripts:

```bash
export $(cat .env | xargs)   # or use python-dotenv / direnv if you prefer
```

## Run it

```bash
# Quick smoke test — just the first page of issues, to check everything works
python src/ingest_issues.py --repo Textualize/textual --max-pages 1

# Full pull — several thousand issues, will take a few minutes even with a token
python src/ingest_issues.py --repo Textualize/textual --state all

# Docs corpus
python src/ingest_docs.py --repo Textualize/textual --docs-path docs
```

Both scripts are idempotent — rerun them anytime and they'll just update rows
that changed (`ON CONFLICT ... DO UPDATE`), which is exactly what the nightly
Action does automatically.

Inspect the results:

```bash
sqlite3 data/triage.db "SELECT COUNT(*) FROM issues;"
sqlite3 data/triage.db "SELECT COUNT(*) FROM docs;"
sqlite3 data/triage.db "SELECT title, state FROM issues ORDER BY created_at DESC LIMIT 5;"
```

## Automating it

Push this repo to GitHub and the `nightly-ingest.yml` workflow just works — it uses
the automatically-provided `secrets.GITHUB_TOKEN`, no setup needed on your end. You
can also trigger it manually from the Actions tab (`workflow_dispatch`) to test it
without waiting for the schedule.

## Roadmap

1. ~~Scope the problem~~ — read real Textual issues, understand what's actually painful
2. **Data pipeline** ← you are here — issues + docs into SQLite, refreshed nightly
3. RAG layer — embed docs + closed issues into a vector DB (Chroma), retrieve similar
   issues and relevant doc sections for any new issue
4. Triage agent — LLM call that takes a new issue + retrieved context, outputs
   structured JSON: suggested labels, likely duplicates, a draft response, priority
5. Ship as a live bot — trigger on new issues via a GitHub Action, post the result
   as a comment (only after maintainer sign-off)
6. Dashboard — issue volume, response-time impact, label-accuracy over time
   (Streamlit Community Cloud, free hosting)
7. Iterate on real feedback

## Cost

Everything here runs free, indefinitely: GitHub API (free), GitHub Actions (free,
unlimited minutes on public repos), SQLite (free, no server), and — once step 3
lands — Chroma (free, self-hosted) plus a free-tier LLM API for the agent calls.
No credit card required anywhere in this stack.
