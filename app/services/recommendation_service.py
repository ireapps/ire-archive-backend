"""Recommendation service for finding similar resources."""

from typing import Any, cast

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.config import SIMILAR_CHUNK_SCROLL_LIMIT, SIMILAR_FETCH_MULTIPLIER, get_serving_collection_name


def get_similar_resources(
    qdrant_client: QdrantClient,
    vector_id: str,
    limit: int = 5,
    collection_name: str | None = None,
) -> list[dict[str, Any]]:
    """Find similar resources based on vector similarity.

    Args:
        qdrant_client: Qdrant client instance
        vector_id: The vector ID (MD5 hash) to find similar resources for
        limit: Maximum number of similar resources to return (default: 5)

    Returns:
        List of similar resources with metadata and scores
    """
    collection_name = collection_name or get_serving_collection_name()
    # Step 0: Retrieve the point by its ID to get the resource_id
    point_result = qdrant_client.retrieve(
        collection_name=collection_name,
        ids=[vector_id],
        with_payload=True,
    )

    if not point_result or len(point_result) == 0:
        # Point not found
        return []

    first_payload = point_result[0].payload or {}

    # Extract resource_id from the point's metadata
    resource_id = first_payload.get("metadata", {}).get("resource_id")
    if not resource_id:
        # No resource_id in metadata
        return []

    # Step 1: Find all chunks for the current resource
    current_resource_filter = Filter(
        must=[
            FieldCondition(
                key="metadata.resource_id",
                match=MatchValue(value=resource_id),
            )
        ]
    )

    current_chunks = qdrant_client.scroll(
        collection_name=collection_name,
        scroll_filter=current_resource_filter,
        limit=SIMILAR_CHUNK_SCROLL_LIMIT,
        with_payload=True,
        with_vectors=True,
    )[0]

    if not current_chunks:
        # Resource not found
        return []

    # Step 2: Use the first chunk's vectors as the query
    first_chunk = current_chunks[0]

    # Extract dense vector from the named vectors dict
    # The vector attribute is a dict with keys "dense" and "sparse"
    if isinstance(first_chunk.vector, dict):
        dense_vector = first_chunk.vector.get("dense")
    else:
        # Fallback: if vector is not a dict, assume it's the dense vector directly
        dense_vector = first_chunk.vector

    # Fallback to empty list if no dense vector is present (tests/mocks may omit vectors)
    dense_query = cast(list[float] | list[list[float]], dense_vector if dense_vector is not None else [])

    # Step 3: Query for similar points using dense vector search
    # Fetch more candidates to ensure we get enough unique resources after deduplication
    fetch_limit = limit * SIMILAR_FETCH_MULTIPLIER

    similar_points = qdrant_client.query_points(
        collection_name=collection_name,
        query=dense_query,
        using="dense",
        limit=fetch_limit,
        with_payload=True,
    ).points

    # Step 4: Filter out chunks from the current resource
    filtered_points = [
        point
        for point in similar_points
        if point.payload and point.payload.get("metadata", {}).get("resource_id") != resource_id
    ]

    # Step 5: Deduplicate by resource_id (keep highest-scoring chunk per resource)
    # For each unique resource, we need to find its first chunk (chunk_index=0) to get the vector_id
    seen_resources: dict[str, Any] = {}
    resource_first_chunks: dict[str, str] = {}  # Maps resource_id to first chunk's vector_id

    for point in filtered_points:
        if not point.payload:
            continue

        res_id = point.payload.get("metadata", {}).get("resource_id")
        if not res_id:
            continue

        # Track the first chunk (chunk_index=0) for each resource to get the vector_id
        chunk_index = point.payload.get("metadata", {}).get("chunk_index", 0)
        if chunk_index == 0:
            resource_first_chunks[res_id] = str(point.id)

        if res_id not in seen_resources or point.score > seen_resources[res_id]["score"]:
            seen_resources[res_id] = {
                "resource_id": res_id,
                "score": float(point.score),
                "title": point.payload.get("title", ""),
                "metadata": point.payload.get("metadata", {}),
                "temp_vector_id": str(point.id),  # Store this chunk's ID temporarily
            }

    # Now update each resource with the correct vector_id (from first chunk)
    for res_id, resource_data in seen_resources.items():
        # Use the first chunk's point.id as the resource vector_id
        if res_id in resource_first_chunks:
            resource_vector_id = resource_first_chunks[res_id]
        else:
            # Fallback: use the highest-scoring chunk's vector_id
            resource_vector_id = resource_data["temp_vector_id"]

        # Add vector_id to the result
        resource_data["vector_id"] = resource_vector_id
        # Remove temporary field
        del resource_data["temp_vector_id"]

    unique_resources = sorted(seen_resources.values(), key=lambda x: x["score"], reverse=True)[:limit]

    return unique_resources
