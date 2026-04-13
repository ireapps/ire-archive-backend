"""Visualization service for 2D embedding projection via UMAP.

Retrieves dense vectors from Qdrant, reduces them to 2D coordinates,
and returns the result with document metadata for interactive visualization.
"""

import time
from typing import Any

import numpy as np
import structlog
import umap
from qdrant_client import QdrantClient

from app.config import COLLECTION_NAME, SCROLL_BATCH_SIZE

logger = structlog.get_logger()

# UMAP defaults tuned for archive-sized collections (hundreds to low thousands)
DEFAULT_N_NEIGHBORS = 15
DEFAULT_MIN_DIST = 0.1
DEFAULT_METRIC = "cosine"


def _scroll_all_vectors(
    client: QdrantClient,
    *,
    sample_limit: int | None = None,
) -> tuple[list[str], list[list[float]], list[dict[str, Any]]]:
    """Scroll through all points and collect dense vectors + metadata.

    Args:
        client: Qdrant client instance.
        sample_limit: If set, stop after collecting this many points
            (useful during development or for very large collections).

    Returns:
        Tuple of (point_ids, vectors, payloads) where each list has
        the same length and indices correspond to the same document.
    """
    point_ids: list[str] = []
    vectors: list[list[float]] = []
    payloads: list[dict[str, Any]] = []
    offset = None
    batch_num = 0

    while True:
        records, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=SCROLL_BATCH_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )

        if not records:
            break

        batch_num += 1

        for record in records:
            # Extract dense vector
            dense_vec: list[float] | None = None
            if isinstance(record.vector, dict):
                dense_vec = record.vector.get("dense")  # type: ignore[assignment]
            elif record.vector is not None:
                dense_vec = list(record.vector)  # type: ignore[arg-type]

            if dense_vec is None:
                continue

            point_ids.append(str(record.id))
            vectors.append(dense_vec)

            payload = record.payload or {}
            metadata = payload.get("metadata", {})
            payloads.append(
                {
                    "vector_id": str(record.id),
                    "title": payload.get("title", ""),
                    "category": metadata.get("category", ""),
                    "year": metadata.get("year_computed"),
                    "resource_id": metadata.get("resource_id"),
                    "conference": metadata.get("conference", ""),
                    "text_preview": (payload.get("text", "") or "")[:120],
                }
            )

        if sample_limit and len(point_ids) >= sample_limit:
            point_ids = point_ids[:sample_limit]
            vectors = vectors[:sample_limit]
            payloads = payloads[:sample_limit]
            break

        if offset is None:
            break

    logger.info(
        "scroll_complete",
        total_points=len(point_ids),
        batches=batch_num,
    )
    return point_ids, vectors, payloads


def compute_projection(
    vectors: list[list[float]],
    *,
    n_neighbors: int = DEFAULT_N_NEIGHBORS,
    min_dist: float = DEFAULT_MIN_DIST,
    metric: str = DEFAULT_METRIC,
    random_state: int = 42,
) -> list[list[float]]:
    """Run UMAP to project high-dimensional vectors to 2D.

    Args:
        vectors: List of dense embedding vectors (384-dim each).
        n_neighbors: UMAP locality parameter — larger values give more
            global structure, smaller values preserve local clusters.
        min_dist: Minimum distance between points in the 2D layout.
        metric: Distance metric (should match Qdrant index — cosine).
        random_state: Seed for reproducibility.

    Returns:
        List of [x, y] coordinate pairs, one per input vector.
    """
    arr = np.array(vectors, dtype=np.float32)
    logger.info("umap_start", n_points=arr.shape[0], n_dims=arr.shape[1])
    t0 = time.time()

    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=n_neighbors,
        min_dist=min_dist,
        metric=metric,
        random_state=random_state,
    )
    coords = reducer.fit_transform(arr)
    elapsed = time.time() - t0
    logger.info("umap_complete", elapsed_seconds=round(elapsed, 2))

    # Normalize to [-1, 1] range for consistent frontend rendering
    x_min, x_max = coords[:, 0].min(), coords[:, 0].max()
    y_min, y_max = coords[:, 1].min(), coords[:, 1].max()
    x_range = x_max - x_min or 1.0
    y_range = y_max - y_min or 1.0
    coords[:, 0] = 2.0 * (coords[:, 0] - x_min) / x_range - 1.0
    coords[:, 1] = 2.0 * (coords[:, 1] - y_min) / y_range - 1.0

    return coords.tolist()


def generate_embedding_map(
    client: QdrantClient,
    *,
    sample_limit: int | None = None,
    n_neighbors: int = DEFAULT_N_NEIGHBORS,
    min_dist: float = DEFAULT_MIN_DIST,
) -> dict[str, Any]:
    """Full pipeline: scroll vectors → UMAP → return projection with metadata.

    Args:
        client: Qdrant client.
        sample_limit: Optional cap on number of points to process.
        n_neighbors: UMAP n_neighbors parameter.
        min_dist: UMAP min_dist parameter.

    Returns:
        Dictionary with ``points`` list and ``meta`` summary.
    """
    t0 = time.time()

    point_ids, vectors, payloads = _scroll_all_vectors(
        client,
        sample_limit=sample_limit,
    )

    if len(vectors) < 5:
        return {
            "points": [],
            "meta": {
                "total": 0,
                "error": "Not enough vectors for UMAP projection (need ≥ 5)",
            },
        }

    coords = compute_projection(
        vectors,
        n_neighbors=min(n_neighbors, len(vectors) - 1),
        min_dist=min_dist,
    )

    points = []
    for i, (coord, payload) in enumerate(zip(coords, payloads)):
        points.append(
            {
                "x": round(coord[0], 5),
                "y": round(coord[1], 5),
                **payload,
            }
        )

    elapsed = time.time() - t0

    # Collect category counts for legend
    category_counts: dict[str, int] = {}
    for p in payloads:
        cat = p.get("category") or "unknown"
        category_counts[cat] = category_counts.get(cat, 0) + 1

    return {
        "points": points,
        "meta": {
            "total": len(points),
            "elapsed_seconds": round(elapsed, 2),
            "umap_params": {
                "n_neighbors": n_neighbors,
                "min_dist": min_dist,
            },
            "categories": category_counts,
        },
    }
