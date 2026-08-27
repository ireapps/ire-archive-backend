"""Configuration management for the semantic search API.

Configuration is loaded from environment variables with sensible defaults.
Override any setting by setting the corresponding environment variable.
"""

import json
import os

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# === QDRANT CONFIGURATION ===

#: Qdrant server hostname
QDRANT_HOST: str = os.getenv("QDRANT_HOST", "localhost")

#: Qdrant server port
QDRANT_PORT: int = int(os.getenv("QDRANT_PORT", "6333"))

#: Qdrant collection name for storing vectors
COLLECTION_NAME: str = os.getenv("COLLECTION_NAME", "nonprofit_knowledge")

#: Stable alias used by the API. Publication builds replace its target, never its contents.
SERVING_COLLECTION_ALIAS: str = os.getenv("SERVING_COLLECTION_ALIAS", f"{COLLECTION_NAME}_live")

#: Timeout in seconds for Qdrant operations
QDRANT_TIMEOUT: int = 60

#: Skip Qdrant version compatibility check for faster startup
QDRANT_CHECK_COMPATIBILITY: bool = False

# === MODEL CONFIGURATION ===

#: Embedding vector dimension (384 for all-MiniLM-L6-v2)
VECTOR_SIZE: int = int(os.getenv("VECTOR_SIZE", "384"))

#: Base directory for cached ML models
MODEL_CACHE_DIR: str = os.getenv("MODEL_CACHE_DIR", "./models_cache")

#: Path to pre-quantized dense embedding model (all-MiniLM-L6-v2 ONNX int8)
DENSE_MODEL_PATH: str = f"{MODEL_CACHE_DIR}/optimized_embedder"

#: HuggingFace model name for PyTorch loading (cross-platform consistency)
#: Used instead of ONNX path to ensure identical inference across Mac/Linux
DENSE_MODEL_NAME: str = "sentence-transformers/all-MiniLM-L6-v2"

#: Sparse embedding model name (fastembed registry, downloads to its own cache)
SPARSE_MODEL_NAME: str = "Qdrant/bm25"

#: HuggingFace model name for PyTorch loading (cross-platform consistency)
#: Used instead of cached path to ensure identical inference across Mac/Linux
CROSS_ENCODER_NAME: str = "cross-encoder/ms-marco-TinyBERT-L-2-v2"

# === SEARCH CONFIGURATION ===

#: Batch size for Qdrant scroll operations when fetching all records (1-10000)
SCROLL_BATCH_SIZE: int = int(os.getenv("SCROLL_BATCH_SIZE", "100"))

#: Multiplier for dense vector prefetch in hybrid search (1-20)
#: Higher values cast a wider net but increase computation
DENSE_PREFETCH_MULTIPLIER: int = int(os.getenv("DENSE_PREFETCH_MULTIPLIER", "10"))

#: Multiplier for sparse vector prefetch in hybrid search (1-20)
SPARSE_PREFETCH_MULTIPLIER: int = int(os.getenv("SPARSE_PREFETCH_MULTIPLIER", "10"))

#: Default relevance score when score is unavailable (0.0-1.0)
DEFAULT_SCORE: float = float(os.getenv("DEFAULT_SCORE", "1.0"))

#: Minimum score threshold for filtering search results (0.0-1.0)
DEFAULT_SCORE_THRESHOLD: float = float(os.getenv("DEFAULT_SCORE_THRESHOLD", "0.3"))

#: Maximum results to consider for cross-encoder reranking
MAX_RERANK_RESULTS: int = int(os.getenv("MAX_RERANK_RESULTS", "500"))

#: Candidate pool multiplier for reranking (e.g., 15× means fetch 15× limit)
RERANK_CANDIDATE_MULTIPLIER: int = int(os.getenv("RERANK_CANDIDATE_MULTIPLIER", "15"))

#: Minimum score for including results after reranking
RERANK_MIN_SCORE: float = float(os.getenv("RERANK_MIN_SCORE", "-8.0"))

#: Maximum text length for cross-encoder reranking in characters
#: Longer texts are truncated for performance
MAX_RERANK_TEXT_LENGTH: int = int(os.getenv("MAX_RERANK_TEXT_LENGTH", "1000"))

# === CACHE CONFIGURATION ===

#: Search results cache size (number of unique queries)
SEARCH_CACHE_SIZE: int = int(os.getenv("SEARCH_CACHE_SIZE", "100"))

#: Search results cache TTL in seconds (10 minutes)
SEARCH_CACHE_TTL: int = int(os.getenv("SEARCH_CACHE_TTL", "600"))

#: Resource detail cache size (number of unique resources)
RESOURCE_CACHE_SIZE: int = int(os.getenv("RESOURCE_CACHE_SIZE", "5000"))

#: Resource detail cache TTL in seconds (24 hours)
RESOURCE_CACHE_TTL: int = int(os.getenv("RESOURCE_CACHE_TTL", "86400"))

#: Similar resources cache size (number of unique resource IDs)
SIMILAR_CACHE_SIZE: int = int(os.getenv("SIMILAR_CACHE_SIZE", "1000"))

#: Similar resources cache TTL in seconds (24 hours)
SIMILAR_CACHE_TTL: int = int(os.getenv("SIMILAR_CACHE_TTL", "86400"))

#: Reranked results cache size (number of unique query+filter combinations)
RERANKED_CACHE_SIZE: int = int(os.getenv("RERANKED_CACHE_SIZE", "500"))

#: Reranked results cache TTL in seconds (5 minutes)
#: Shorter TTL since search results may change more frequently
RERANKED_CACHE_TTL: int = int(os.getenv("RERANKED_CACHE_TTL", "300"))

# === RECOMMENDATION CONFIGURATION ===

#: Limit for chunk scroll when finding similar resources
SIMILAR_CHUNK_SCROLL_LIMIT: int = int(os.getenv("SIMILAR_CHUNK_SCROLL_LIMIT", "100"))

#: Fetch multiplier for similar resources (e.g., 10× means fetch 10× limit)
#: Higher multiplier ensures enough unique resources after deduplication
SIMILAR_FETCH_MULTIPLIER: int = int(os.getenv("SIMILAR_FETCH_MULTIPLIER", "10"))

# === API CONFIGURATION ===

#: Allowed CORS origins for API requests
ALLOWED_ORIGINS: list[str] = [
    "http://localhost:5173",  # Vite dev server
    "http://localhost:4173",  # Vite preview server
    "https://archive.ire.org",  # Vercel production
]

# Allow operators to add extra origins without changing code.
# Supports a JSON array (preferred) or a comma-separated list.
# Example: ADDITIONAL_ALLOWED_ORIGINS='["https://your-frontend.example.com"]'
_additional_origins_env: str = os.getenv("ADDITIONAL_ALLOWED_ORIGINS", "")
if _additional_origins_env:
    _existing = set(ALLOWED_ORIGINS)
    try:
        _extra = json.loads(_additional_origins_env)
        if not isinstance(_extra, list):
            raise ValueError("ADDITIONAL_ALLOWED_ORIGINS JSON must be an array")
        for _o in _extra:
            _o = str(_o)
            if _o not in _existing:
                ALLOWED_ORIGINS.append(_o)
                _existing.add(_o)
    except json.JSONDecodeError:
        # Fallback: treat as a comma-separated list
        for _o in _additional_origins_env.split(","):
            _o = _o.strip()
            if _o and _o not in _existing:
                ALLOWED_ORIGINS.append(_o)
                _existing.add(_o)

# === API REQUEST LIMITS ===

#: Maximum query string length in characters
MAX_QUERY_LENGTH: int = int(os.getenv("MAX_QUERY_LENGTH", "1000"))

#: Maximum pagination offset to prevent excessive skipping
MAX_OFFSET: int = int(os.getenv("MAX_OFFSET", "10000"))

#: Default results per page
DEFAULT_LIMIT: int = int(os.getenv("DEFAULT_LIMIT", "20"))

#: Minimum results per page
MIN_LIMIT: int = int(os.getenv("MIN_LIMIT", "1"))

#: Maximum results per page
MAX_LIMIT: int = int(os.getenv("MAX_LIMIT", "100"))

# === INDEXING CONFIGURATION ===

#: Unified batch size for embedding generation and upload
BATCH_SIZE: int = 1000

# === PUBLICATION CONFIGURATION ===

# Publication endpoints remain disabled until both independent signing secrets and
# narrow URL rules are configured.
PUBLICATION_DISPATCH_SECRET: str | None = os.getenv("PUBLICATION_DISPATCH_SECRET")
PUBLICATION_CALLBACK_SECRET: str | None = os.getenv("PUBLICATION_CALLBACK_SECRET")
PUBLICATION_STATE_DB: str = os.getenv("PUBLICATION_STATE_DB", "/data/qdrant_storage/publication-state.sqlite3")
PUBLICATION_WORK_DIR: str = os.getenv("PUBLICATION_WORK_DIR", "/tmp/ire-publications")
PUBLICATION_SNAPSHOT_URL_PREFIXES: tuple[str, ...] = tuple(
    value.strip() for value in os.getenv("PUBLICATION_SNAPSHOT_URL_PREFIXES", "").split(",") if value.strip()
)
PUBLICATION_CALLBACK_URLS: tuple[str, ...] = tuple(
    value.strip() for value in os.getenv("PUBLICATION_CALLBACK_URLS", "").split(",") if value.strip()
)
PUBLICATION_CALLBACK_URL_PREFIXES: tuple[str, ...] = tuple(
    value.strip() for value in os.getenv("PUBLICATION_CALLBACK_URL_PREFIXES", "").split(",") if value.strip()
)
PUBLICATION_MAX_REQUEST_BYTES: int = int(os.getenv("PUBLICATION_MAX_REQUEST_BYTES", "16384"))
PUBLICATION_MAX_SNAPSHOT_BYTES: int = int(os.getenv("PUBLICATION_MAX_SNAPSHOT_BYTES", "2147483648"))
PUBLICATION_MAX_UPLOAD_BYTES: int = int(os.getenv("PUBLICATION_MAX_UPLOAD_BYTES", "8000000"))
PUBLICATION_MAX_RECORD_BYTES: int = int(os.getenv("PUBLICATION_MAX_RECORD_BYTES", "20000000"))
PUBLICATION_MAX_EXTRACTED_TEXT_CHARS: int = int(os.getenv("PUBLICATION_MAX_EXTRACTED_TEXT_CHARS", "5000000"))
PUBLICATION_SIGNATURE_MAX_AGE_SECONDS: int = int(os.getenv("PUBLICATION_SIGNATURE_MAX_AGE_SECONDS", "300"))
PUBLICATION_CALLBACK_RETRIES: int = int(os.getenv("PUBLICATION_CALLBACK_RETRIES", "3"))
PUBLICATION_BUILD_LOCK_LEASE_SECONDS: int = int(os.getenv("PUBLICATION_BUILD_LOCK_LEASE_SECONDS", "7200"))


def get_serving_collection_name() -> str:
    """Return the alias that API reads use instead of a mutable collection."""
    return SERVING_COLLECTION_ALIAS


# === STATIC CATEGORY FILTERS ===

#: Valid categories in the IRE resources dataset
#: These are the only standardized categories available for filtering
VALID_CATEGORIES: frozenset[str] = frozenset(
    [
        "audio",
        "contest entry",
        "dataset",
        "journal",
        "tipsheet",
        "webinar",
    ]
)

#: Category normalization mapping (source → standardized)
#: Used during indexing to normalize source data categories
CATEGORY_MAPPING: dict[str, str] = {
    "audio": "audio",
    "contest": "contest entry",
    "data": "dataset",
    "journal": "journal",
    "newsletter": "journal",
    "tipsheet": "tipsheet",
    "transcript": "tipsheet",
    "webinar": "webinar",
}

# === RATE LIMITING CONFIGURATION ===

#: Rate limit for search endpoint (format: "requests/period")
#: Period can be: second, minute, hour, day
RATE_LIMIT_SEARCH: str = os.getenv("RATE_LIMIT_SEARCH", "60/minute;10/second")

#: Rate limit for resource detail endpoint
RATE_LIMIT_RESOURCE: str = os.getenv("RATE_LIMIT_RESOURCE", "120/minute;20/second")

#: Rate limit for admin endpoints (heavily restricted)
RATE_LIMIT_ADMIN: str = os.getenv("RATE_LIMIT_ADMIN", "10/minute")

#: Rate limit for stats endpoint
RATE_LIMIT_STATS: str = os.getenv("RATE_LIMIT_STATS", "30/minute")

#: Storage backend for rate limiting: "memory" (single instance) or "redis" (distributed)
RATE_LIMIT_STORAGE: str = os.getenv("RATE_LIMIT_STORAGE", "memory")

#: Redis URL for distributed rate limiting (optional)
RATE_LIMIT_REDIS_URL: str | None = os.getenv("RATE_LIMIT_REDIS_URL", None)

#: Whether to include rate limit headers in responses
RATE_LIMIT_HEADERS_ENABLED: bool = os.getenv("RATE_LIMIT_HEADERS_ENABLED", "true").lower() == "true"

#: Optional bypass token for rate limiting (internal testing only)
#: No default is provided; set RATE_LIMIT_BYPASS_TOKEN in your environment if needed
RATE_LIMIT_BYPASS_TOKEN: str | None = os.getenv("RATE_LIMIT_BYPASS_TOKEN")

#: IP addresses to whitelist from rate limiting (comma-separated)
#: Example: "127.0.0.1,10.0.0.1"
RATE_LIMIT_WHITELIST: list[str] = [ip.strip() for ip in os.getenv("RATE_LIMIT_WHITELIST", "").split(",") if ip.strip()]

# === CONFIGURATION VALIDATION ===

# Validate critical configuration values
assert 1 <= SCROLL_BATCH_SIZE <= 10000, "SCROLL_BATCH_SIZE must be between 1 and 10000"
assert 1 <= DENSE_PREFETCH_MULTIPLIER <= 20, "DENSE_PREFETCH_MULTIPLIER must be between 1 and 20"
assert 1 <= SPARSE_PREFETCH_MULTIPLIER <= 20, "SPARSE_PREFETCH_MULTIPLIER must be between 1 and 20"
assert 0.0 <= DEFAULT_SCORE <= 1.0, "DEFAULT_SCORE must be between 0.0 and 1.0"
assert 0.0 <= DEFAULT_SCORE_THRESHOLD <= 1.0, "DEFAULT_SCORE_THRESHOLD must be between 0.0 and 1.0"
assert MAX_RERANK_RESULTS > 0, "MAX_RERANK_RESULTS must be positive"
assert RERANK_CANDIDATE_MULTIPLIER > 0, "RERANK_CANDIDATE_MULTIPLIER must be positive"
assert MAX_RERANK_TEXT_LENGTH > 0, "MAX_RERANK_TEXT_LENGTH must be positive"
assert SIMILAR_FETCH_MULTIPLIER > 0, "SIMILAR_FETCH_MULTIPLIER must be positive"
assert MAX_QUERY_LENGTH > 0, "MAX_QUERY_LENGTH must be positive"
assert MAX_OFFSET > 0, "MAX_OFFSET must be positive"
assert MIN_LIMIT >= 1, "MIN_LIMIT must be at least 1"
assert MAX_LIMIT >= MIN_LIMIT, "MAX_LIMIT must be >= MIN_LIMIT"
assert MIN_LIMIT <= DEFAULT_LIMIT <= MAX_LIMIT, "DEFAULT_LIMIT must be between MIN_LIMIT and MAX_LIMIT"
assert PUBLICATION_MAX_REQUEST_BYTES > 0, "PUBLICATION_MAX_REQUEST_BYTES must be positive"
assert PUBLICATION_MAX_SNAPSHOT_BYTES > 0, "PUBLICATION_MAX_SNAPSHOT_BYTES must be positive"
assert PUBLICATION_MAX_UPLOAD_BYTES > 0, "PUBLICATION_MAX_UPLOAD_BYTES must be positive"
assert PUBLICATION_MAX_RECORD_BYTES > 0, "PUBLICATION_MAX_RECORD_BYTES must be positive"
assert PUBLICATION_MAX_EXTRACTED_TEXT_CHARS > 0, "PUBLICATION_MAX_EXTRACTED_TEXT_CHARS must be positive"
assert PUBLICATION_BUILD_LOCK_LEASE_SECONDS >= 60, "PUBLICATION_BUILD_LOCK_LEASE_SECONDS must be at least 60"
assert PUBLICATION_SIGNATURE_MAX_AGE_SECONDS > 0, "PUBLICATION_SIGNATURE_MAX_AGE_SECONDS must be positive"
