#!/usr/bin/env python3
"""Benchmark indexing throughput on a representative sample of records.

Generates synthetic-but-representative documents (similar text length to real
tipsheets/transcripts), indexes them into a scratch Qdrant collection using the
current scripts/qdrant_ops.py pipeline, and reports records/sec plus an
extrapolated full-catalog (33,344 record) completion estimate.

Run this once on `main` and once on this branch (same machine, same sample
size) to compare before/after throughput - see the PR description for
measured numbers.

Usage:
    uv run python -m scripts.benchmark_indexing --count 3000
"""

from __future__ import annotations

import argparse
import random
import sys
import time
import uuid
from pathlib import Path

# Add parent directory to path for app imports
sys.path.append(str(Path(__file__).parent.parent))

from qdrant_client.models import Distance, SparseVectorParams, VectorParams

from app.config import COLLECTION_NAME, VECTOR_SIZE
from scripts.models import load_dense_model, load_sparse_model
from scripts.qdrant_ops import connect_qdrant, get_batch_list, index_batches

FULL_CATALOG_RECORD_COUNT = 33_344

# Representative of real tipsheet/transcript descriptions + extracted text: a
# few paragraphs, not a single sentence and not a full multi-page transcript.
_WORDS = (
    "investigative reporting data journalism public records foia request source "
    "protection fact checking documents database analysis nonprofit accountability "
    "government contracts spending audit whistleblower interview transcript panel "
    "conference tipsheet training workshop reporter editor newsroom methodology"
).split()


def _synthetic_text(rng: random.Random, min_words: int = 150, max_words: int = 400) -> str:
    word_count = rng.randint(min_words, max_words)
    return " ".join(rng.choice(_WORDS) for _ in range(word_count))


def _synthetic_documents(count: int) -> list[dict]:
    rng = random.Random(42)  # noqa: S311 - deterministic sample, not security-sensitive
    return [
        {
            "id": str(uuid.uuid4()),
            "title": f"Benchmark resource {i}",
            "doc_type": "ire_resource",
            "metadata": {"benchmark": True},
            "text": _synthetic_text(rng),
        }
        for i in range(count)
    ]


def _wait_for_point_count(client, collection_name: str, expected_count: int, timeout: float = 120) -> int:
    """Poll Qdrant until the collection reports the expected point count or timeout.

    Implemented locally (not imported from scripts.qdrant_ops) so this script
    can be run unmodified against both the "before" and "after" code when
    comparing throughput.
    """
    deadline = time.time() + timeout
    last_count = 0
    while True:
        last_count = client.get_collection(collection_name).points_count or 0
        if last_count >= expected_count or time.time() >= deadline:
            return last_count
        time.sleep(1)


def run_benchmark(count: int, batch_size: int) -> None:
    print(f"Generating {count:,} synthetic documents...")
    documents = _synthetic_documents(count)

    print("Loading embedding models (all-MiniLM-L6-v2 + BM25 sparse)...")
    dense_model = load_dense_model()
    sparse_model = load_sparse_model()

    print("Connecting to Qdrant...")
    client = connect_qdrant(is_production=False)

    # index_batches() always targets app.config.COLLECTION_NAME (it doesn't take
    # a collection_name argument), so the benchmark has to (re)create that
    # collection rather than a separate scratch one. This is a local dev Qdrant
    # instance, so that's safe; the collection is deleted again at the end.
    collection_name = COLLECTION_NAME
    try:
        client.delete_collection(collection_name)
    except Exception:  # noqa: BLE001
        pass
    client.create_collection(
        collection_name=collection_name,
        vectors_config={"dense": VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE)},
        sparse_vectors_config={"sparse": SparseVectorParams()},
    )

    try:
        batches = get_batch_list(documents, batch_size)
        errors: list[str] = []

        start = time.time()
        successful, failed = index_batches(client, batches, dense_model, sparse_model, errors)
        indexed_count = _wait_for_point_count(client, collection_name, successful)
        elapsed = time.time() - start

        rate = count / elapsed if elapsed > 0 else 0.0
        estimated_full_catalog_seconds = FULL_CATALOG_RECORD_COUNT / rate if rate > 0 else float("inf")

        print("\n=== Benchmark results ===")
        print(f"Records:            {count:,}")
        print(f"Batch size:         {batch_size}")
        print(f"Successful/failed:  {successful:,} / {failed:,}")
        print(f"Qdrant point count: {indexed_count:,}")
        print(f"Elapsed:            {elapsed:.2f}s")
        print(f"Throughput:         {rate:.1f} records/sec")
        print(
            f"Estimated full catalog ({FULL_CATALOG_RECORD_COUNT:,} records): "
            f"{estimated_full_catalog_seconds / 60:.1f} minutes"
        )
        if errors:
            print(f"\n{len(errors)} errors occurred, e.g.: {errors[:3]}")
    finally:
        client.delete_collection(collection_name)
        del sparse_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=3000, help="Number of synthetic records to index")
    parser.add_argument("--batch-size", type=int, default=1000, help="Batch size to use for indexing")
    args = parser.parse_args()

    run_benchmark(args.count, args.batch_size)


if __name__ == "__main__":
    main()
