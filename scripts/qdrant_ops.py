#!/usr/bin/env python3
"""Qdrant operations and progress helpers."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import time
from typing import Any
from collections.abc import Callable

from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    HnswConfigDiff,
    OptimizersConfigDiff,
    PointStruct,
    SparseVectorParams,
    VectorParams,
)
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sentence_transformers import SentenceTransformer
from tqdm import tqdm  # type: ignore[import-untyped]

from app.config import (
    COLLECTION_NAME,
    QDRANT_CHECK_COMPATIBILITY,
    QDRANT_HOST,
    QDRANT_INDEX_VERIFY_POLL_SECONDS,
    QDRANT_INDEX_VERIFY_TIMEOUT_SECONDS,
    QDRANT_PORT,
    QDRANT_TIMEOUT,
)

console = Console()


# ----------------------------------------------------------------------------
# Collection lifecycle
# ----------------------------------------------------------------------------


def create_hybrid_collection(
    client: QdrantClient,
    collection_name: str,
    vector_size: int,
    recreate: bool = False,
) -> bool:
    collections = client.get_collections().collections
    collection_names = [c.name for c in collections]

    if collection_name in collection_names:
        if recreate:
            try:
                client.delete_collection(collection_name)
            except Exception:  # noqa: BLE001
                pass
        else:
            info = client.get_collection(collection_name)
            vectors_config = info.config.params.vectors
            has_dense = isinstance(vectors_config, dict) and "dense" in vectors_config
            has_sparse = info.config.params.sparse_vectors is not None

            if has_dense and has_sparse:
                return True
            client.delete_collection(collection_name)

    client.create_collection(
        collection_name=collection_name,
        vectors_config={
            "dense": VectorParams(size=vector_size, distance=Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )

    return True


def apply_production_optimizations(client: QdrantClient) -> None:
    console.print("Applying production optimizations...", style="dim")

    client.update_collection(
        collection_name=COLLECTION_NAME,
        optimizer_config=OptimizersConfigDiff(
            indexing_threshold=100000,
        ),
        hnsw_config=HnswConfigDiff(
            m=32,
            ef_construct=200,
        ),
    )


def finalize_production_indexing(client: QdrantClient) -> None:
    console.print("\n[bold cyan]Optimizing collection and building index...[/bold cyan]")

    client.update_collection(
        collection_name=COLLECTION_NAME,
        optimizer_config=OptimizersConfigDiff(
            indexing_threshold=20000,
        ),
    )

    console.print("   Waiting for index optimization...", style="dim")
    time.sleep(5)


# ----------------------------------------------------------------------------
# Connection & validation
# ----------------------------------------------------------------------------


def connect_qdrant(is_production: bool) -> QdrantClient:
    console.print(f"\n[bold cyan]Connecting to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}...[/bold cyan]")

    try:
        client = QdrantClient(
            host=QDRANT_HOST,
            port=QDRANT_PORT,
            timeout=QDRANT_TIMEOUT,
            check_compatibility=QDRANT_CHECK_COMPATIBILITY,
        )
        collections = client.get_collections()
        console.print("[SUCCESS] Connected to Qdrant", style="green")
        console.print(f"   Existing collections: {[c.name for c in collections.collections]}", style="dim")
        return client
    except Exception as exc:  # noqa: BLE001
        console.print(f"[ERROR] Failed to connect to Qdrant: {exc}", style="red")
        if not is_production:
            console.print("\n[yellow]Please ensure Qdrant is running:[/yellow]")
            console.print("  ire dev start")
        raise


def validate_collection(
    client: QdrantClient,
    collection_name: str,
    dense_model: SentenceTransformer,
    test_queries: list[str] | None = None,
) -> bool:
    console.print("\n[bold cyan]Running validation tests...[/bold cyan]")

    test_queries = test_queries or [
        "investigative reporting techniques",
        "data journalism",
        "FOIA requests",
        "source protection",
        "fact checking",
    ]

    all_passed = True

    for query in test_queries:
        try:
            query_embedding = dense_model.encode(query).tolist()

            results = client.query_points(
                collection_name=collection_name,
                query=query_embedding,
                using="dense",
                limit=5,
            ).points

            if results:
                console.print(
                    f"   '{query}': {len(results)} results (top score: {results[0].score:.3f})",
                    style="green",
                )
            else:
                console.print(f"   '{query}': No results found", style="yellow")
                all_passed = False

        except Exception as exc:  # noqa: BLE001
            console.print(f"   '{query}': Error - {exc}", style="red")
            all_passed = False

    if all_passed:
        console.print("\n[SUCCESS] Validation complete - all tests passed", style="green")
    else:
        console.print("\n[WARNING] Validation complete - some tests failed", style="yellow")

    return all_passed


# ----------------------------------------------------------------------------
# Embedding + upload pipeline
# ----------------------------------------------------------------------------


def generate_embeddings_batch(
    dense_model: SentenceTransformer,
    sparse_model: SparseTextEmbedding,
    texts: list[str],
    show_progress: bool = False,
    log_timing: bool = False,
) -> tuple[list[list[float]], list[Any]]:
    embed_start = time.time()

    dense_embeddings = dense_model.encode(texts, batch_size=128, show_progress_bar=show_progress)
    dense_embeddings_list = [emb.tolist() for emb in dense_embeddings]

    sparse_embeddings = list(sparse_model.embed(texts))

    if log_timing:
        embed_time = time.time() - embed_start
        embed_speed = len(texts) / embed_time if embed_time > 0 else 0
        console.print(
            f"   Generated {len(texts)} hybrid embeddings in {embed_time:.2f}s ({embed_speed:.0f} docs/sec)",
            style="dim cyan",
        )

    return dense_embeddings_list, sparse_embeddings


def create_hybrid_point(
    point_id: str,
    dense_embedding: list[float],
    sparse_embedding: Any,
    text: str,
    metadata: dict[str, Any],
) -> PointStruct:
    return PointStruct(
        id=point_id,
        vector={
            "dense": dense_embedding,
            "sparse": sparse_embedding.as_object(),
        },
        payload={"text": text, **metadata},
    )


def upload_batch_with_retry(
    client: QdrantClient,
    collection_name: str,
    points: list[PointStruct],
    max_retries: int = 3,
    retry_delay: int = 5,
    wait: bool = True,
) -> tuple[int, int]:
    for attempt in range(max_retries):
        try:
            client.upsert(
                collection_name=collection_name,
                points=points,
                wait=wait,
            )
            return len(points), 0
        except Exception:  # noqa: BLE001
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (2**attempt))
            else:
                return 0, len(points)

    return 0, len(points)


def build_points_for_batch(
    batch: list[dict[str, Any]],
    dense_model: SentenceTransformer,
    sparse_model: SparseTextEmbedding,
    log_timing: bool = False,
) -> list[PointStruct]:
    """Embed a batch of documents and turn them into hybrid Qdrant points.

    This is the CPU-bound half of indexing a batch (no network calls), so it
    is safe to run on the main thread while a previous batch's upload runs on
    a background thread.
    """
    texts = [doc["text"] for doc in batch]

    dense_embeddings, sparse_embeddings = generate_embeddings_batch(
        dense_model, sparse_model, texts, show_progress=False, log_timing=log_timing
    )

    points = []
    for i, doc in enumerate(batch):
        metadata = {
            "title": doc["title"],
            "doc_type": doc["doc_type"],
            "metadata": doc["metadata"],
        }
        point = create_hybrid_point(
            doc["id"],
            dense_embeddings[i],
            sparse_embeddings[i],
            doc["text"],
            metadata,
        )
        points.append(point)

    return points


class OverlappedBatchUploader:
    """Runs an upload for each submitted batch on a single background thread.

    Batches are still uploaded strictly one at a time, in submission order -
    only one is ever in flight, and results are returned in the same order
    they were submitted. What changes is *when* the caller waits for a
    batch's upload to finish: instead of blocking right after submitting it,
    the caller can do the next batch's CPU-bound embedding work first, and
    only collect the previous batch's result when it submits the next one
    (or calls close() for the last one). That overlaps embedding time with
    upload time instead of spending them back-to-back, without ever having
    more than one upload in flight, reordering points, or dropping any.

    Example:
        uploader = OverlappedBatchUploader(upload_fn)
        for batch_idx, batch in enumerate(batches):
            points = build_points_for_batch(batch, ...)  # overlaps prior upload
            handle_result(uploader.submit(batch_idx, points))
        handle_result(uploader.close())
    """

    def __init__(self, upload_fn: Callable[[list[PointStruct]], tuple[int, int]]) -> None:
        self._upload_fn = upload_fn
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._pending: tuple[int, Future[tuple[int, int]]] | None = None

    def submit(self, batch_idx: int, points: list[PointStruct]) -> tuple[int, int, int] | None:
        """Queue a batch for upload and return the previously pending batch's result, if any."""
        drained = self._drain_pending()
        self._pending = (batch_idx, self._executor.submit(self._upload_fn, points))
        return drained

    def close(self) -> tuple[int, int, int] | None:
        """Wait for the last submitted batch's result and stop the worker thread."""
        try:
            return self._drain_pending()
        finally:
            self._executor.shutdown(wait=True)

    def abort(self) -> None:
        """Stop the worker thread without waiting for or raising a pending result.

        Use this when the caller is already unwinding from an unrelated error
        and doesn't want that error replaced by a failure from the last
        in-flight upload.
        """
        self._pending = None
        self._executor.shutdown(wait=False)

    def _drain_pending(self) -> tuple[int, int, int] | None:
        if self._pending is None:
            return None
        batch_idx, future = self._pending
        self._pending = None
        success_count, fail_count = future.result()
        return batch_idx, success_count, fail_count


def wait_for_indexed(
    client: QdrantClient,
    collection_name: str,
    expected_count: int,
    timeout: float = QDRANT_INDEX_VERIFY_TIMEOUT_SECONDS,
    poll_interval: float = QDRANT_INDEX_VERIFY_POLL_SECONDS,
) -> int:
    """Poll Qdrant until a collection reports the expected point count.

    Batches are uploaded with wait=False, so Qdrant may still be applying the
    last few of them when the final upsert call returns. This single poll
    replaces blocking on every batch's durability confirmation - it is called
    once, after all batches have been submitted.

    Args:
        client: Connected Qdrant client.
        collection_name: Name of the collection to check.
        expected_count: The point count we expect once indexing has caught up.
        timeout: Give up after this many seconds and return whatever count
            Qdrant last reported, even if it's short of expected_count.
        poll_interval: Seconds to sleep between polls.

    Returns:
        The last point count Qdrant reported for the collection. Callers
        should compare this to expected_count themselves; a short count means
        the timeout was reached before indexing caught up.
    """
    deadline = time.monotonic() + timeout
    last_count = 0
    while True:
        info = client.get_collection(collection_name)
        last_count = info.points_count or 0
        if last_count >= expected_count or time.monotonic() >= deadline:
            return last_count
        time.sleep(poll_interval)


def get_batch_list(items: list[Any], batch_size: int) -> list[list[Any]]:
    batches = []
    for i in range(0, len(items), batch_size):
        batches.append(items[i : i + batch_size])
    return batches


def index_batches(
    client: QdrantClient,
    batches: list[list[dict]],
    dense_model: SentenceTransformer,
    sparse_model: SparseTextEmbedding,
    errors: list[str],
) -> tuple[int, int]:
    """Embed and upload batches, overlapping each batch's embedding with the previous batch's upload.

    Uploads use wait=False; call wait_for_indexed(...) afterwards to confirm
    the collection has caught up before relying on the result.
    """
    successful_points = 0
    failed_points = 0

    def upload_fn(points: list[PointStruct]) -> tuple[int, int]:
        return upload_batch_with_retry(client, COLLECTION_NAME, points, max_retries=3, retry_delay=5, wait=False)

    def record_result(result: tuple[int, int, int] | None) -> None:
        nonlocal successful_points, failed_points
        if result is None:
            return
        batch_idx, success_count, fail_count = result
        successful_points += success_count
        failed_points += fail_count
        if fail_count > 0:
            errors.append(f"Batch {batch_idx + 1}: {fail_count} points failed")

    with tqdm(total=len(batches), desc=f"Indexing {sum(len(b) for b in batches):,} documents", unit="batch") as bar:
        uploader = OverlappedBatchUploader(upload_fn)
        try:
            for batch_idx, batch in enumerate(batches):
                try:
                    points = build_points_for_batch(batch, dense_model, sparse_model)
                except Exception as exc:  # noqa: BLE001
                    failed_points += len(batch)
                    errors.append(f"Batch {batch_idx + 1} error: {str(exc)[:100]}")
                    console.print(f"   [ERROR] Batch {batch_idx + 1} failed: {exc}", style="red")
                    bar.update(1)
                    continue

                record_result(uploader.submit(batch_idx, points))
                bar.update(1)
        finally:
            record_result(uploader.close())

    return successful_points, failed_points


# ----------------------------------------------------------------------------
# Summary display
# ----------------------------------------------------------------------------


def display_indexing_summary(
    total_documents: int,
    total_chunks: int,
    total_vectors: int,
    successful_points: int,
    failed_points: int,
    total_time: float,
    transform_errors: list[tuple[int, str]] | None = None,
    errors: list[str] | None = None,
    collection_stats: dict[str, Any] | None = None,
    memory_gb: float | None = None,
    cpu_cores: int | None = None,
    parallel_mode: bool | None = None,
) -> None:
    console.print("\n" + "=" * 60)

    summary = Table(title="Indexing Summary", show_header=True)
    summary.add_column("Metric", style="cyan")
    summary.add_column("Value", style="white")

    summary.add_row("Total Documents", f"{total_documents:,}")

    if transform_errors is not None:
        summary.add_row("Successfully Transformed", f"{total_documents - len(transform_errors):,}")
        summary.add_row("Transform Errors", str(len(transform_errors)))

    if total_chunks != total_documents:
        summary.add_row("Total Chunks", f"{total_chunks:,}")

    summary.add_row("Successfully Indexed", f"{successful_points:,}")
    summary.add_row("Failed to Index", str(failed_points))
    summary.add_row("Total Time", f"{total_time:.2f} seconds")

    if total_time > 0:
        docs_per_sec = int(total_documents / total_time)
        vecs_per_sec = int(total_vectors / total_time) if total_vectors > 0 else 0
        summary.add_row("Documents/Second", str(docs_per_sec))
        if total_vectors > 0:
            summary.add_row("Vectors/Second", str(vecs_per_sec))

    if errors is not None:
        summary.add_row("Errors", str(len(errors)))
    if memory_gb is not None:
        summary.add_row("Peak Memory Usage", f"{memory_gb:.2f} GB")
    if cpu_cores is not None:
        summary.add_row("CPU Cores Used", str(cpu_cores))
    if parallel_mode is not None:
        summary.add_row("Parallel Mode", "Enabled" if parallel_mode else "Disabled (Sequential)")

    console.print(summary)

    if transform_errors:
        console.print(
            f"\n[WARNING] {len(transform_errors)} resources failed transformation:",
            style="yellow",
        )
        for idx, error in transform_errors[:5]:
            console.print(f"   Resource {idx}: {error}", style="yellow")

    if errors:
        console.print(f"\n{len(errors)} errors occurred:", style="yellow")
        for error in errors[:5]:
            console.print(f"   {error}", style="yellow")

    if collection_stats:
        console.print("\nFinal collection stats:")
        console.print(f"   Total points in collection: {collection_stats.get('points_count', 0)}")

    if total_time < 60:
        time_str = f"{total_time:.1f} seconds"
    else:
        minutes = int(total_time // 60)
        seconds = int(total_time % 60)
        time_str = f"{minutes}m {seconds}s"

    if total_time > 0:
        docs_per_sec_float = total_documents / total_time
        vecs_per_sec_float = total_vectors / total_time if total_vectors > 0 else 0
        speed_text = f"[dim]Processing speed: {docs_per_sec_float:.0f} docs/sec"
        if total_vectors > 0:
            speed_text += f" | {vecs_per_sec_float:.0f} vectors/sec"
        speed_text += "[/dim]"
    else:
        speed_text = ""

    if successful_points == total_documents:
        status = "[bold green]All documents indexed successfully![/bold green]"
    elif successful_points > 0:
        status = f"[yellow]Partial success: {successful_points}/{total_documents} documents indexed[/yellow]"
    else:
        status = "[bold red]Indexing failed completely[/bold red]"

    console.print(
        Panel.fit(
            f"[bold green]Successfully indexed {successful_points:,} documents "
            f"({total_vectors:,} vectors) in {time_str}[/bold green]\n"
            f"{speed_text}",
            title="Success",
        )
    )
    console.print(f"\n{status}")
