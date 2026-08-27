"""Cache management service for search results and filter options."""

import hashlib
import json
import time
from typing import Any, Optional

from cachetools import TTLCache

from app.config import (
    RERANKED_CACHE_SIZE,
    RERANKED_CACHE_TTL,
    RESOURCE_CACHE_SIZE,
    RESOURCE_CACHE_TTL,
    SEARCH_CACHE_SIZE,
    SEARCH_CACHE_TTL,
    SIMILAR_CACHE_SIZE,
    SIMILAR_CACHE_TTL,
)
from app.models import SearchResponse

# Initialize search cache
# TTL cache for search results with configurable size and expiration
search_cache: TTLCache[str, SearchResponse] = TTLCache(maxsize=SEARCH_CACHE_SIZE, ttl=SEARCH_CACHE_TTL)

# Initialize similar resources cache
# Separate cache for similar resource recommendations with longer TTL
similar_cache: TTLCache[str, Any] = TTLCache(
    maxsize=SIMILAR_CACHE_SIZE,
    ttl=SIMILAR_CACHE_TTL,
)

# Initialize resource detail cache
# Caches individual resource detail responses for faster page loads
resource_cache: TTLCache[str, Any] = TTLCache(
    maxsize=RESOURCE_CACHE_SIZE,
    ttl=RESOURCE_CACHE_TTL,
)

# Cache metrics for monitoring cache performance
# Tracks hits, misses, and start time for calculating hit rate
cache_metrics = {"hits": 0, "misses": 0, "start_time": time.time()}
similar_cache_metrics = {"hits": 0, "misses": 0}
resource_cache_metrics = {"hits": 0, "misses": 0}

# Initialize reranked results cache
# Caches full reranked result sets for consistent pagination across pages
# Key excludes offset/limit so page 1 and page 2 share the same cached results
reranked_cache: TTLCache[str, list[Any]] = TTLCache(
    maxsize=RERANKED_CACHE_SIZE,
    ttl=RERANKED_CACHE_TTL,
)
reranked_cache_metrics = {"hits": 0, "misses": 0}


def clear_publication_caches() -> None:
    """Clear every cache that can hold results from an old search collection."""
    search_cache.clear()
    similar_cache.clear()
    resource_cache.clear()
    reranked_cache.clear()


def get_cache_key(
    query: str, filters: dict | None, offset: int, limit: int, sort_by: str, search_mode: str = "hybrid"
) -> str:
    """Generate deterministic cache key from search parameters using MD5 hash.

    Creates a unique cache key by hashing all search parameters to ensure
    identical queries return cached results regardless of parameter order.

    Args:
        query: Search query text
        filters: Optional filter dictionary (e.g., {"category": "tipsheet"})
        offset: Pagination offset
        limit: Maximum results to return
        sort_by: Sort order ('relevance', 'newest', 'oldest')
        search_mode: Search mode ('hybrid' or 'keyword')

    Returns:
        MD5 hash string (32 hex characters) uniquely identifying this search

    Note:
        Uses json.dumps with sort_keys=True to ensure deterministic key generation
        regardless of dict key ordering.
    """
    cache_data = {
        "query": query,
        "filters": filters or {},
        "offset": offset,
        "limit": limit,
        "sort_by": sort_by,
        "search_mode": search_mode,
    }
    cache_str = json.dumps(cache_data, sort_keys=True)
    return hashlib.md5(cache_str.encode()).hexdigest()


def get_rerank_cache_key(query: str, filters: dict | None, sort_by: str, search_mode: str = "hybrid") -> str:
    """Generate cache key for reranked results that EXCLUDES offset/limit.

    This ensures all pages of the same search share the same cached result set,
    preventing the pagination bug where different pages have different candidates.

    Args:
        query: Search query text
        filters: Optional filter dictionary (e.g., {"category": "tipsheet"})
        sort_by: Sort order ('relevance', 'newest', 'oldest')
        search_mode: Search mode ('hybrid' or 'keyword')

    Returns:
        MD5 hash string uniquely identifying this search (pagination-independent)
    """
    cache_data = {
        "query": query,
        "filters": filters or {},
        "sort_by": sort_by,
        "search_mode": search_mode,
    }
    cache_str = json.dumps(cache_data, sort_keys=True)
    return hashlib.md5(cache_str.encode()).hexdigest()
