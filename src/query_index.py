"""Manually test retrieval: paste in issue-like text, see what the index surfaces.

This is worth running on its own, before wiring up the LLM agent -- if
retrieval quality is bad here, no amount of prompt engineering fixes it
downstream.

Usage:
    python src/query_index.py "app crashes on startup with a KeyError"
"""

import argparse

import chromadb
from chromadb.utils import embedding_functions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Issue title/body text to search for similar context")
    parser.add_argument("--chroma-dir", default="data/chroma")
    parser.add_argument("--collection", default="textual_corpus")
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()

    ef = embedding_functions.DefaultEmbeddingFunction()
    client = chromadb.PersistentClient(path=args.chroma_dir)
    collection = client.get_collection(args.collection, embedding_function=ef)

    results = collection.query(query_texts=[args.query], n_results=args.k)

    for i, (doc, meta, dist) in enumerate(zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    )):
        print(f"\n--- #{i + 1} ({meta['type']}, distance={dist:.3f}) ---")
        if meta["type"] == "issue":
            print(f"Issue #{meta['number']}: {meta['title']}  ({meta['url']})")
        else:
            print(f"Doc: {meta['path']}")
        preview = doc[:300].replace("\n", " ")
        print(preview + ("..." if len(doc) > 300 else ""))


if __name__ == "__main__":
    main()