"""Build a Chroma vector index from the SQLite corpus (docs + closed issues).

This is what step 4 (the triage agent) will query for "what's the relevant
doc section" and "have we seen this issue before".

Usage:
    python src/build_index.py --db data/triage.db --chroma-dir data/chroma

Uses Chroma's built-in embedding function (all-MiniLM-L6-v2 via ONNX) --
runs locally, no API calls, no cost, and no torch dependency needed.
First run downloads the model (~80MB); after that it works offline.
"""

import argparse
import sqlite3
import sys

import chromadb
from chromadb.utils import embedding_functions

CHUNK_SIZE = 800      # characters
CHUNK_OVERLAP = 100


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start:start + size])
        start += size - overlap
    return chunks


def load_docs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT path, content FROM docs").fetchall()
    items = []
    for path, content in rows:
        for i, chunk in enumerate(chunk_text(content)):
            items.append({
                "id": f"doc:{path}:{i}",
                "text": chunk,
                "metadata": {"type": "doc", "path": path},
            })
    return items


def load_issues(conn: sqlite3.Connection, states=("closed",)) -> list[dict]:
    """Only closed issues by default -- these are the ones with known resolutions,
    which is what makes them useful as duplicate/similarity reference points."""
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT number, title, body, html_url, state FROM issues WHERE state IN ({placeholders})",
        states,
    ).fetchall()
    items = []
    for number, title, body, url, state in rows:
        text = f"{title}\n\n{body or ''}".strip()
        if not text:
            continue
        items.append({
            "id": f"issue:{number}",
            "text": text[:4000],  # cap length -- a handful of issues can be enormous
            "metadata": {"type": "issue", "number": number, "title": title or "", "url": url or "", "state": state},
        })
    return items


def main():
    parser = argparse.ArgumentParser(description="Build the Chroma index from docs + closed issues.")
    parser.add_argument("--db", default="data/triage.db")
    parser.add_argument("--chroma-dir", default="data/chroma")
    parser.add_argument("--collection", default="textual_corpus")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    items = load_docs(conn) + load_issues(conn)
    conn.close()

    if not items:
        print("No docs or issues found in the DB -- run ingest_issues.py and ingest_docs.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"Embedding {len(items)} chunks (docs + closed issues)...")
    ef = embedding_functions.DefaultEmbeddingFunction()

    client = chromadb.PersistentClient(path=args.chroma_dir)
    collection = client.get_or_create_collection(args.collection, embedding_function=ef)

    for i in range(0, len(items), args.batch_size):
        batch = items[i:i + args.batch_size]
        collection.upsert(
            ids=[b["id"] for b in batch],
            documents=[b["text"] for b in batch],
            metadatas=[b["metadata"] for b in batch],
        )
        print(f"...{min(i + args.batch_size, len(items))}/{len(items)}")

    print(f"Done. Index has {collection.count()} chunks in '{args.chroma_dir}'.")


if __name__ == "__main__":
    main()