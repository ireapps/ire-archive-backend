"""Search service for semantic and hybrid search operations."""

from collections.abc import Sequence
from typing import Any, Optional, Union, cast

import structlog
from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, Fusion, FusionQuery, Prefetch, Record, ScoredPoint, SparseVector
from sentence_transformers import SentenceTransformer

from app.config import (
    DEFAULT_SCORE,
    DEFAULT_SCORE_THRESHOLD,
    DENSE_PREFETCH_MULTIPLIER,
    MAX_RERANK_RESULTS,
    RERANK_CANDIDATE_MULTIPLIER,
    SCROLL_BATCH_SIZE,
    SPARSE_PREFETCH_MULTIPLIER,
    get_serving_collection_name,
)
from app.diagnostics import (
    SearchDiagnostics,
    log_dense_embedding,
    log_final_results,
    log_fusion_results,
    log_query_input,
    log_reranking,
    log_sparse_embedding,
)

logger = structlog.get_logger()
from app.services.cache_service import get_rerank_cache_key, reranked_cache, reranked_cache_metrics
from app.services.reranking_service import rerank_results


def _get_resource_year(result: ScoredPoint | Record) -> int | None:
    """Extract resource year from result metadata.

    Args:
        result: Qdrant search result (ScoredPoint) or record (Record)

    Returns:
        Resource year as integer if present in metadata, None otherwise
    """
    if result.payload is None:
        return None
    metadata = result.payload.get("metadata", {})
    if not metadata:
        return None
    return metadata.get("year_computed")


def _filter_by_year(
    results: Sequence[ScoredPoint | Record],
) -> list[ScoredPoint | Record]:
    """Filter results to only include records with year metadata.

    Used for date-based sorting to ensure only dated records are sorted.
    Undated records are handled separately to avoid sorting errors.

    Args:
        results: Sequence of Qdrant results or records

    Returns:
        List containing only results that have year_computed in metadata
    """
    return [r for r in results if _get_resource_year(r) is not None]


def _sort_by_date(results: Sequence[ScoredPoint | Record], sort_by: str) -> list[ScoredPoint | Record]:
    """Sort results by year, with undated records at the bottom.

    Two-tier sorting strategy:
    1. Records WITH year_computed: sorted by date (newest/oldest)
    2. Records WITHOUT year_computed: kept in original order (relevance)

    This ensures dated records appear first while preserving all high-quality results.

    Args:
        results: Sequence of Qdrant results or records to sort
        sort_by: Sort direction, either "newest" (descending) or "oldest" (ascending)

    Returns:
        Sorted list with dated records first (sorted by year), followed by
        undated records (in original relevance order)
    """
    # Partition into dated and undated groups
    dated = [r for r in results if _get_resource_year(r) is not None]
    undated = [r for r in results if _get_resource_year(r) is None]

    # Sort only the dated group (use 0 as default for type safety, though we know it's not None)
    dated_sorted = sorted(dated, key=lambda r: _get_resource_year(r) or 0, reverse=(sort_by == "newest"))

    # Return dated first, then undated (in original relevance order)
    return dated_sorted + undated


def _deduplicate_results(
    results: Sequence[ScoredPoint | Record],
) -> list[ScoredPoint | Record]:
    """Deduplicate results by ID, keeping first occurrence (highest relevance).

    Must be called BEFORE pagination to ensure accurate total counts.

    Args:
        results: Sequence of results that may contain duplicates

    Returns:
        List of unique results, preserving order (first occurrence kept)
    """
    seen_ids = set()
    unique = []
    duplicates_found = 0
    for result in results:
        if result.id in seen_ids:
            duplicates_found += 1
            continue
        seen_ids.add(result.id)
        unique.append(result)
    if duplicates_found > 0:
        logger.warning("duplicates_removed_before_pagination", count=duplicates_found)
    return unique


def _paginate_results(
    results: Sequence[ScoredPoint | Record], offset: int, limit: int
) -> tuple[list[ScoredPoint | Record], int]:
    """Apply pagination to results and return total count.

    Args:
        results: Sequence of results to paginate (should be deduplicated first)
        offset: Number of results to skip
        limit: Maximum number of results to return

    Returns:
        Tuple of (paginated_results, total_count) where:
        - paginated_results: List slice from offset to offset+limit
        - total_count: Total number of results before pagination
    """
    total_count = len(results)
    paginated = list(results[offset : offset + limit])
    return paginated, total_count


def _build_hybrid_query(
    dense_embedding: list[float],
    sparse_embedding: Any,
    limit: int,
    score_threshold: float | None = None,  # Phase 1: Add threshold param
) -> tuple[list[Prefetch], FusionQuery]:
    """Build hybrid search query with dense and sparse prefetch.

    Args:
        dense_embedding: Dense vector embedding
        sparse_embedding: Sparse vector embedding
        limit: Number of results to fetch per vector type
        score_threshold: Optional minimum score threshold for dense prefetch

    Returns:
        Tuple of (prefetch_list, fusion_query)
    """
    # Phase 1: Add score_threshold to dense prefetch
    logger.info("building_hybrid_query", score_threshold=score_threshold, limit=limit)
    dense_prefetch = Prefetch(
        query=dense_embedding,
        using="dense",
        limit=limit * DENSE_PREFETCH_MULTIPLIER,
    )

    # Apply score threshold if provided
    if score_threshold is not None:
        dense_prefetch.score_threshold = score_threshold

    prefetch = [
        dense_prefetch,
        Prefetch(
            query=sparse_embedding.as_object(),
            using="sparse",
            limit=limit * SPARSE_PREFETCH_MULTIPLIER,
        ),
    ]

    fusion_query = FusionQuery(fusion=Fusion.RRF)
    return prefetch, fusion_query


def _fetch_all_filtered_records(
    qdrant_client: QdrantClient,
    qdrant_filter: Filter,
) -> list[Record | ScoredPoint]:
    """Fetch all records matching a filter using Qdrant scroll API.

    Uses pagination via scroll to efficiently retrieve large result sets
    without loading everything into memory at once.

    Args:
        qdrant_client: Qdrant client instance
        qdrant_filter: Filter to apply to the query

    Returns:
        List of all matching Record objects with payloads

    Note:
        Fetches in batches of SCROLL_BATCH_SIZE (100) until all records
        matching the filter have been retrieved.
    """
    all_records: list[Record | ScoredPoint] = []
    offset_point = None

    while True:
        records, offset_point = qdrant_client.scroll(
            collection_name=get_serving_collection_name(),
            scroll_filter=qdrant_filter,
            limit=SCROLL_BATCH_SIZE,
            offset=offset_point,
            with_payload=True,
        )

        if not records:
            break

        all_records.extend(records)

        if offset_point is None:
            break

    return all_records


def perform_filter_only_search(
    qdrant_client: QdrantClient,
    qdrant_filter: Filter,
    limit: int,
    offset: int,
    sort_by: str,
) -> tuple[list[ScoredPoint | Record], int]:
    """Perform filter-only browse without semantic search.

    Used when no query text is provided. Fetches all records matching
    the filter, applies date sorting if requested, and paginates results.

    Args:
        qdrant_client: Qdrant client instance
        qdrant_filter: Filter to apply (e.g., category filter)
        limit: Maximum number of results to return
        offset: Number of results to skip for pagination
        sort_by: Sort order ('relevance', 'newest', 'oldest')

    Returns:
        Tuple of (paginated_results, total_count) where:
        - paginated_results: List of results for current page
        - total_count: Total number of matching records

    Note:
        For date sorting, only records with year_computed are sorted.
        Records without dates are appended after sorted records.
    """
    logger.info("filter_only_browse", offset=offset, limit=limit, sort_by=sort_by)

    # Fetch all matching records
    all_records = _fetch_all_filtered_records(qdrant_client, qdrant_filter)

    # Apply date sorting if requested
    if sort_by in ["newest", "oldest"]:
        all_records = _filter_by_year(all_records)
        all_records = _sort_by_date(all_records, sort_by)
        logger.info("records_sorted", count=len(all_records), sort_by=sort_by)

    # Paginate and return
    return _paginate_results(all_records, offset, limit)


def perform_keyword_search(
    qdrant_client: QdrantClient,
    sparse_model: SparseTextEmbedding,
    query: str,
    limit: int,
    offset: int,
    sort_by: str,
    qdrant_filter: Filter | None,
) -> tuple[list[ScoredPoint | Record], int]:
    """Perform keyword-only search using BM25 sparse embeddings.

    This provides traditional keyword matching without semantic understanding.
    Uses only the sparse (BM25) vector index for lexical term matching.

    Args:
        qdrant_client: Qdrant client instance
        sparse_model: Sparse embedding model (BM25)
        query: Search query text
        limit: Number of results to return
        offset: Number of results to skip
        sort_by: Sort order ('relevance', 'newest', 'oldest')
        qdrant_filter: Optional filter to apply

    Returns:
        Tuple of (paginated_results, total_count)
    """
    import hashlib

    query_hash = hashlib.md5(query.encode()).hexdigest()[:8]
    logger.info(
        "keyword_search_start",
        query_hash=query_hash,
        query_length=len(query),
        limit=limit,
        offset=offset,
        sort_by=sort_by,
    )

    # Generate sparse embedding for keyword matching
    sparse_embedding = list(sparse_model.embed([query]))[0]
    log_sparse_embedding(query, sparse_embedding)

    # Fetch more results to allow for sorting and deduplication
    fetch_limit = MAX_RERANK_RESULTS * RERANK_CANDIDATE_MULTIPLIER
    logger.info("fetching_keyword_candidates", fetch_limit=fetch_limit)

    # Convert sparse embedding to SparseVector for Qdrant query
    indices_list = list(map(int, cast(Sequence[int], sparse_embedding.indices.tolist())))
    values_list = [float(v) for v in cast(Sequence[float], sparse_embedding.values.tolist())]

    sparse_vector = SparseVector(
        indices=indices_list,
        values=values_list,
    )

    # Query using only sparse vector (keyword matching)
    results: list[ScoredPoint] = qdrant_client.query_points(
        collection_name=get_serving_collection_name(),
        query=sparse_vector,
        using="sparse",
        limit=fetch_limit,
        offset=0,
        query_filter=qdrant_filter,
    ).points

    logger.info("fetched_keyword_results", count=len(results))

    # Handle date sorting if requested
    if sort_by in ["newest", "oldest"]:
        results = cast(list[ScoredPoint], _sort_by_date(results, sort_by))
        logger.info("after_date_sort", count=len(results), sort_by=sort_by)

    # Deduplicate results
    results = cast(list[ScoredPoint], _deduplicate_results(results))

    # Paginate and return
    return _paginate_results(results, offset, limit)


def perform_semantic_search(
    qdrant_client: QdrantClient,
    embedding_model: SentenceTransformer,
    sparse_model: SparseTextEmbedding,
    query: str,
    limit: int,
    offset: int,
    sort_by: str,
    qdrant_filter: Filter | None,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    serving_generation: str | int = 0,
) -> tuple[list[ScoredPoint | Record], int]:
    """Perform semantic search with hybrid (dense + sparse) embeddings.

    Uses cached reranked results for consistent pagination across pages.
    The cache key excludes offset/limit so all pages share the same result set.

    Args:
        qdrant_client: Qdrant client instance
        embedding_model: Dense embedding model
        sparse_model: Sparse embedding model
        query: Search query text
        limit: Number of results to return
        offset: Number of results to skip
        sort_by: Sort order ('relevance', 'newest', 'oldest')
        qdrant_filter: Optional filter to apply
        score_threshold: Minimum score threshold

    Returns:
        Tuple of (paginated_results, total_count)
    """
    # Convert filter to dict for cache key (None-safe)
    filter_dict: dict[str, Any] | None = None
    if qdrant_filter:
        # Extract filter conditions for cache key
        filter_dict = qdrant_filter.model_dump() if hasattr(qdrant_filter, "model_dump") else None

    # Check reranked cache first (key excludes offset/limit for consistent pagination)
    rerank_cache_key = get_rerank_cache_key(query, filter_dict, sort_by, serving_generation=serving_generation)

    if rerank_cache_key in reranked_cache:
        reranked_cache_metrics["hits"] += 1
        cached_results = reranked_cache[rerank_cache_key]
        logger.info(
            "rerank_cache_hit",
            result_count=len(cached_results),
            offset=offset,
            limit=limit,
        )
        return _paginate_results(cached_results, offset, limit)

    reranked_cache_metrics["misses"] += 1
    logger.info("rerank_cache_miss")

    # Log pipeline configuration
    logger.info(
        "search_pipeline_config",
        request_limit=limit,
        request_offset=offset,
        fetch_limit=MAX_RERANK_RESULTS * RERANK_CANDIDATE_MULTIPLIER,
        max_rerank_results=MAX_RERANK_RESULTS,
    )

    # DIAGNOSTIC: Log query input (hashed only for privacy)
    log_query_input(query, filter_dict, limit, offset, sort_by)

    # Generate embeddings
    dense_embedding = embedding_model.encode(query).tolist()
    # DIAGNOSTIC: Log dense embedding
    log_dense_embedding(query, dense_embedding)

    sparse_embedding = list(sparse_model.embed([query]))[0]
    # DIAGNOSTIC: Log sparse embedding
    log_sparse_embedding(query, sparse_embedding)

    # Build hybrid query with threshold
    prefetch, fusion_query = _build_hybrid_query(
        dense_embedding,
        sparse_embedding,
        limit,
        score_threshold=score_threshold,
    )

    # FIXED: Use constant fetch limit regardless of offset/limit
    # This ensures consistent candidate set for all pages
    fetch_limit = MAX_RERANK_RESULTS * RERANK_CANDIDATE_MULTIPLIER
    logger.info("fetching_candidates", fetch_limit=fetch_limit)

    # Fetch candidates
    results: list[ScoredPoint] = qdrant_client.query_points(
        collection_name=get_serving_collection_name(),
        prefetch=prefetch,
        query=fusion_query,
        limit=fetch_limit,
        offset=0,  # Always start from beginning
        query_filter=qdrant_filter,
    ).points

    logger.info("fetched_rrf_candidates", count=len(results))
    # DIAGNOSTIC: Log fusion results
    log_fusion_results(results)

    # Apply reranking to full candidate set
    if len(results) > 1:
        rerank_limit = min(len(results), MAX_RERANK_RESULTS)
        pre_rerank_count = len(results)
        logger.info(
            "reranking_input",
            candidates_in=pre_rerank_count,
            rerank_limit=rerank_limit,
        )
        # Capture RRF scores before reranking
        scores_before_rerank = [getattr(r, "score", 0.0) for r in results]
        results = rerank_results(query, results, limit=rerank_limit)
        # Capture cross-encoder scores after reranking
        scores_after_rerank = [getattr(r, "score", 0.0) for r in results]
        filtered_count = pre_rerank_count - len(results)
        logger.info("after_reranking", count=len(results))
        # DIAGNOSTIC: Log reranking results with full details
        log_reranking(
            query=query,
            input_count=pre_rerank_count,
            output_count=len(results),
            min_score_threshold=None,  # No longer using absolute threshold
            scores_before=scores_before_rerank,
            scores_after=scores_after_rerank,
            filtered_count=filtered_count,
        )

    # Handle date sorting if requested
    if sort_by in ["newest", "oldest"]:
        results = cast(list[ScoredPoint], _sort_by_date(results, sort_by))
        logger.info("after_date_sort", count=len(results), sort_by=sort_by)

    # Track count before deduplication
    pre_dedup_count = len(results)

    # Deduplicate BEFORE caching to ensure accurate total count
    results = cast(list[ScoredPoint], _deduplicate_results(results))
    deduplicated_count = pre_dedup_count - len(results)

    # Cache the full reranked result set (all pages will use this)
    reranked_cache[rerank_cache_key] = results
    logger.info("cached_reranked_results", count=len(results))

    # DIAGNOSTIC: Log final results before pagination
    log_final_results(
        results=results,
        total_count=len(results),
        deduplicated_count=deduplicated_count,
    )

    # Paginate from the full result set
    logger.info(
        "pagination_slice",
        total_available=len(results),
        offset=offset,
        limit=limit,
        slice_start=offset,
        slice_end=min(offset + limit, len(results)),
        returning=min(limit, max(0, len(results) - offset)),
    )
    return _paginate_results(results, offset, limit)


def format_search_results(
    results: Sequence[ScoredPoint | Record],
) -> list[dict[str, Any]]:
    """Format search results into standardized API response format.

    Extracts relevant fields from Qdrant results and normalizes them
    into a consistent dictionary structure for the API response.
    Deduplicates results by ID to prevent frontend rendering errors.

    Args:
        results: Sequence of Qdrant ScoredPoint or Record objects

    Returns:
        List of dictionaries with standardized fields:
        - id: Point ID (UUID)
        - score: Relevance score (defaults to 1.0 for Record objects)
        - text: Document text content
        - title: Document title
        - doc_type: Document type
        - metadata: Additional metadata dictionary

    Note:
        Safely handles missing payloads and fields with empty string defaults.
        Deduplicates by ID, keeping the first occurrence (highest relevance).
    """
    seen_ids = set()
    formatted = []
    for result in results:
        if result.id in seen_ids:
            logger.warning("skipping_duplicate_result", result_id=result.id)
            continue
        seen_ids.add(result.id)
        formatted.append(
            {
                "vector_id": result.id,
                "score": float(getattr(result, "score", DEFAULT_SCORE)),
                "text": result.payload.get("text", "") if result.payload else "",
                "title": result.payload.get("title", "") if result.payload else "",
                "doc_type": result.payload.get("doc_type", "") if result.payload else "",
                "metadata": result.payload.get("metadata", {}) if result.payload else {},
            }
        )
    return formatted
