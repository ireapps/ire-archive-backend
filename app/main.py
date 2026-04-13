# app/main.py
"""FastAPI application for semantic search API."""

import hashlib
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

# Configure structlog
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger()

# Add immediate startup message
logger.info("fastapi_main_loading", python_version=sys.version)

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from qdrant_client.models import VectorParams
from slowapi.errors import RateLimitExceeded

from app.auth.config import get_auth_settings
from app.auth.dependencies import require_member
from app.auth.exceptions import MemberSuiteError
from app.auth.routes import router as auth_router
from app.auth.session import Session
from app.config import (
    ALLOWED_ORIGINS,
    COLLECTION_NAME,
    QDRANT_HOST,
    QDRANT_PORT,
    RATE_LIMIT_ADMIN,
    RATE_LIMIT_RESOURCE,
    RATE_LIMIT_SEARCH,
    RATE_LIMIT_STATS,
    VECTOR_SIZE,
)
from app.dependencies import get_embedding_model, get_qdrant_client, get_sparse_model, lifespan
from app.exceptions import APIError, ResourceNotFoundError
from app.models import ErrorResponse, SearchQuery, SearchResponse, SimilarResource, SimilarResourcesResponse
from app.rate_limit import RateLimitBypassMiddleware, limit_with_bypass, limiter, rate_limit_exceeded_handler
from app.services.cache_service import (
    cache_metrics,
    get_cache_key,
    reranked_cache,
    reranked_cache_metrics,
    resource_cache,
    resource_cache_metrics,
    search_cache,
    similar_cache,
    similar_cache_metrics,
)
from app.services.filter_service import build_qdrant_filter
from app.services.recommendation_service import get_similar_resources
from app.services.search_service import (
    format_search_results,
    perform_filter_only_search,
    perform_keyword_search,
    perform_semantic_search,
)

logger.info("imports_successful")

# Request ID context variable for tracking requests across logs
request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Create FastAPI app
logger.info("creating_fastapi_app")
app = FastAPI(title="Semantic Search API", version="1.0.0", lifespan=lifespan)
logger.info("fastapi_app_created", app_title="Semantic Search API", app_version="1.0.0")

# Add rate limiter to app state
app.state.limiter = limiter

# Register rate limit error handler
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Rate limit bypass middleware (must be added BEFORE CORS to process first)
app.add_middleware(RateLimitBypassMiddleware)

# Include auth router
app.include_router(auth_router)
logger.info("auth_router_included")

# Update CORS origins to include auth frontend if configured
auth_settings = get_auth_settings()
cors_origins = list(ALLOWED_ORIGINS)  # Copy to avoid mutation
if auth_settings.frontend_url and auth_settings.frontend_url not in cors_origins:
    cors_origins.append(auth_settings.frontend_url)

# CORS middleware - Allow Vercel, localhost, and auth frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
logger.info("cors_middleware_added", allowed_origins=cors_origins)


# Session Extension Middleware (Sliding Expiration)
@app.middleware("http")
async def extend_session_middleware(request: Request, call_next):
    """Extend session TTL on each authenticated request (sliding expiration)."""
    response = await call_next(request)

    # Only extend for successful requests with session
    if response.status_code < 400:
        settings = get_auth_settings()
        cookie = request.cookies.get(settings.session_cookie_name)

        if cookie and hasattr(request.app.state, "session_manager"):
            session_mgr = request.app.state.session_manager
            session = await session_mgr.get_session(cookie)
            if session:
                await session_mgr.extend_session(session.session_id)

    return response


# Request ID Middleware
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Add unique request ID to each request for tracking."""
    req_id = str(uuid.uuid4())[:8]  # Short ID for readability
    request_id_var.set(req_id)
    request.state.request_id = req_id

    # Add request ID to structlog context
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=req_id)

    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    return response


def get_request_id() -> str:
    """Get current request ID from context."""
    return request_id_var.get()


# Exception Handlers
@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    """Handle custom API errors with consistent format."""
    request_id = getattr(request.state, "request_id", None)

    # Log with full details (internal only)
    logger.error(
        "api_error",
        error_code=exc.error_code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
        request_id=request_id,
    )

    # Return client-facing response (no internal details)
    error_response = ErrorResponse(
        error=exc.error_code,
        message=exc.message,
        status_code=exc.status_code,
        request_id=request_id,
    )
    return JSONResponse(status_code=exc.status_code, content=error_response.model_dump())


@app.exception_handler(MemberSuiteError)
async def membersuite_error_handler(request: Request, exc: MemberSuiteError):
    """Handle MemberSuite authentication errors."""
    request_id = getattr(request.state, "request_id", None)

    logger.error(
        "membersuite_error",
        error_code=exc.error_code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
        request_id=request_id,
    )

    error_response = ErrorResponse(
        error=exc.error_code,
        message=exc.message,
        status_code=exc.status_code,
        request_id=request_id,
    )
    return JSONResponse(status_code=exc.status_code, content=error_response.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Handle Pydantic validation errors with user-friendly messages."""
    request_id = getattr(request.state, "request_id", None)

    # Extract first error for user-friendly message
    errors = exc.errors()
    if errors:
        first_error = errors[0]
        field = ".".join(str(loc) for loc in first_error.get("loc", ["unknown"]))
        msg = first_error.get("msg", "Invalid value")
        user_message = f"Validation error for '{field}': {msg}"
    else:
        user_message = "Request validation failed"

    # Log full validation details (internal only)
    logger.warning(
        "validation_error",
        errors=errors,
        request_id=request_id,
    )

    error_response = ErrorResponse(
        error="VALIDATION_ERROR",
        message=user_message,
        status_code=422,
        request_id=request_id,
    )
    return JSONResponse(status_code=422, content=error_response.model_dump())


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch all unhandled exceptions - hide internal details from clients."""
    request_id = getattr(request.state, "request_id", None)

    # Log full exception details (internal only)
    logger.error(
        "unhandled_exception",
        exception=str(exc),
        exception_type=type(exc).__name__,
        request_id=request_id,
    )

    # Return generic message to client (security: don't expose internals)
    error_response = ErrorResponse(
        error="INTERNAL_ERROR",
        message="An unexpected error occurred. Please try again later.",
        status_code=500,
        request_id=request_id,
    )
    return JSONResponse(status_code=500, content=error_response.model_dump())


# API Endpoints
@app.get("/")
async def root():
    """API information and health check"""
    qdrant_client = get_qdrant_client()
    return {
        "service": "Semantic Search API",
        "model": "all-MiniLM-L6-v2 (cached)",
        "vector_db": f"qdrant://{QDRANT_HOST}:{QDRANT_PORT}",
        "collection": COLLECTION_NAME,
        "documents": qdrant_client.get_collection(COLLECTION_NAME).points_count,
        "endpoints": {
            "search": "/search",
            "stats": "/stats",
            "resource": "/resource/{id}",
            "similar": "/resource/{id}/similar",
        },
    }


@app.get("/healthz")
async def healthz():
    """Lightweight health endpoint for platform checks."""
    return {"status": "ok"}


@app.post("/search", response_model=SearchResponse)
@limit_with_bypass(RATE_LIMIT_SEARCH)
async def search(request: Request, response: Response, query: SearchQuery, session: Session = Depends(require_member)):
    """Semantic search endpoint with server-side pagination and caching - requires active membership"""
    # Build filters dict for service layer
    filters = None
    if query.categories:
        filters = {"categories": query.categories}

    # Log only hashed query metadata for privacy (avoid storing raw search text)
    query_hash = None
    query_length = 0
    if query.query:
        query_length = len(query.query)
        query_hash = hashlib.md5(query.query.encode()).hexdigest()[:8]

    logger.info(
        "search_request",
        query_hash=query_hash,
        query_length=query_length,
        limit=query.limit,
        offset=query.offset,
        filters=filters,
        sort_by=query.sort_by,
        search_mode=query.search_mode,
        user_id=session.user_id,
    )
    # Get dependencies
    embedding_model = get_embedding_model()
    sparse_model = get_sparse_model()
    qdrant_client = get_qdrant_client()

    # Generate cache key (include search_mode to avoid mixing cached results)
    cache_key = get_cache_key(query.query or "", filters, query.offset, query.limit, query.sort_by, query.search_mode)

    # Check cache
    if cache_key in search_cache:
        cache_metrics["hits"] += 1
        logger.info("search_cache_hit", cache_key_prefix=cache_key[:8])
        return search_cache[cache_key]

    cache_metrics["misses"] += 1
    logger.info("search_cache_miss", cache_key_prefix=cache_key[:8])

    # Build filter if provided
    qdrant_filter = build_qdrant_filter(filters) if filters else None

    # FILTER-ONLY BROWSE MODE: No query but filters exist
    if (not query.query or query.query.strip() == "") and qdrant_filter:
        paginated_results, total_count = perform_filter_only_search(
            qdrant_client, qdrant_filter, query.limit, query.offset, query.sort_by
        )
        formatted_results = format_search_results(paginated_results)

        # Adjust total for any duplicates removed during formatting
        # This ensures pagination works correctly when duplicates exist
        duplicates_removed = len(paginated_results) - len(formatted_results)
        adjusted_total = total_count - duplicates_removed

        search_response = SearchResponse(
            query="",
            results=formatted_results,
            count=len(formatted_results),
            total=adjusted_total,
            limit=query.limit,
            offset=query.offset,
            has_more=(query.offset + query.limit) < total_count,
        )

        # Store in cache
        search_cache[cache_key] = search_response

        return search_response

    # SEARCH MODE: Query provided (query is guaranteed to exist by validator)
    # Assert for type checker - the validator ensures query is not None here
    assert query.query is not None, "Query should not be None at this point"

    # Route to appropriate search function based on search_mode
    if query.search_mode == "keyword":
        # KEYWORD SEARCH: Use BM25 sparse vectors only (traditional keyword matching)
        paginated_results, total_count = perform_keyword_search(
            qdrant_client,
            sparse_model,
            query.query,
            query.limit,
            query.offset,
            query.sort_by,
            qdrant_filter,
        )
    else:
        # HYBRID SEARCH (default): Use dense + sparse vectors with RRF fusion
        paginated_results, total_count = perform_semantic_search(
            qdrant_client,
            embedding_model,
            sparse_model,
            query.query,
            query.limit,
            query.offset,
            query.sort_by,
            qdrant_filter,
        )
    formatted_results = format_search_results(paginated_results)

    # Adjust total for any duplicates removed during formatting
    # This ensures pagination works correctly when duplicates exist
    duplicates_removed = len(paginated_results) - len(formatted_results)
    adjusted_total = total_count - duplicates_removed

    search_response = SearchResponse(
        query=query.query,
        results=formatted_results,
        count=len(formatted_results),
        total=adjusted_total,
        limit=query.limit,
        offset=query.offset,
        has_more=(query.offset + query.limit) < total_count,
    )

    # Store in cache
    search_cache[cache_key] = search_response

    return search_response


@app.get("/stats")
@limit_with_bypass(RATE_LIMIT_STATS)
async def get_stats(request: Request, response: Response):
    """Get collection statistics including cache metrics and vector configuration diagnostics"""
    logger.info("stats_request")
    qdrant_client = get_qdrant_client()
    info = qdrant_client.get_collection(COLLECTION_NAME)

    # Calculate search cache hit rate
    total_requests = cache_metrics["hits"] + cache_metrics["misses"]
    hit_rate = (cache_metrics["hits"] / total_requests * 100) if total_requests > 0 else 0
    uptime = time.time() - cache_metrics["start_time"]

    # Calculate resource cache hit rate
    resource_total = resource_cache_metrics["hits"] + resource_cache_metrics["misses"]
    resource_hit_rate = (resource_cache_metrics["hits"] / resource_total * 100) if resource_total > 0 else 0

    # Calculate similar resources cache hit rate
    similar_total = similar_cache_metrics["hits"] + similar_cache_metrics["misses"]
    similar_hit_rate = (similar_cache_metrics["hits"] / similar_total * 100) if similar_total > 0 else 0

    # Extract vector configuration diagnostics
    vectors_config = {}
    sparse_vectors_configured = False
    dense_vectors_configured = False

    # Parse vectors_config from collection info
    if hasattr(info.config, "params") and hasattr(info.config.params, "vectors"):
        vectors_param_obj: Any = info.config.params.vectors
        if isinstance(vectors_param_obj, dict):
            # Named vectors configuration (e.g., {"dense": VectorParams, "sparse": SparseVectorParams})
            for name, config in vectors_param_obj.items():
                if isinstance(config, VectorParams):
                    vectors_config[name] = {
                        "type": "dense",
                        "size": config.size,
                        "distance": str(config.distance) if hasattr(config, "distance") else "unknown",
                    }
                    dense_vectors_configured = True
                else:
                    vectors_config[name] = {"type": "sparse"}
                    sparse_vectors_configured = True
        elif isinstance(vectors_param_obj, VectorParams):
            dense_param: VectorParams = vectors_param_obj
            vectors_config["default"] = {
                "type": "dense",
                "size": dense_param.size,
                "distance": str(dense_param.distance) if hasattr(dense_param, "distance") else "unknown",
            }
            dense_vectors_configured = True

    # Check sparse_vectors separately (Qdrant stores them in a different config section)
    if hasattr(info.config, "params") and hasattr(info.config.params, "sparse_vectors"):
        sparse_param = info.config.params.sparse_vectors
        if sparse_param:
            if isinstance(sparse_param, dict):
                for name, config in sparse_param.items():
                    vectors_config[name] = {"type": "sparse"}
                    sparse_vectors_configured = True

    return {
        "collection": COLLECTION_NAME,
        "total_points": info.points_count,
        "vectors_size": VECTOR_SIZE,
        "distance": "cosine",
        "status": info.status,
        "indexed_vectors_count": info.indexed_vectors_count,
        "vector_diagnostics": {
            "dense_vectors_configured": dense_vectors_configured,
            "sparse_vectors_configured": sparse_vectors_configured,
            "vectors_config": vectors_config,
            "hybrid_search_ready": dense_vectors_configured and sparse_vectors_configured,
        },
        "search_cache": {
            "hits": cache_metrics["hits"],
            "misses": cache_metrics["misses"],
            "total_requests": total_requests,
            "hit_rate_percent": round(hit_rate, 2),
            "current_size": len(search_cache),
            "max_size": search_cache.maxsize,
            "ttl_seconds": search_cache.ttl,
            "uptime_seconds": round(uptime, 2),
        },
        "resource_cache": {
            "hits": resource_cache_metrics["hits"],
            "misses": resource_cache_metrics["misses"],
            "total_requests": resource_total,
            "hit_rate_percent": round(resource_hit_rate, 2),
            "current_size": len(resource_cache),
            "max_size": resource_cache.maxsize,
            "ttl_seconds": resource_cache.ttl,
        },
        "similar_cache": {
            "hits": similar_cache_metrics["hits"],
            "misses": similar_cache_metrics["misses"],
            "total_requests": similar_total,
            "hit_rate_percent": round(similar_hit_rate, 2),
            "current_size": len(similar_cache),
            "max_size": similar_cache.maxsize,
            "ttl_seconds": similar_cache.ttl,
        },
        "reranked_cache": {
            "hits": reranked_cache_metrics["hits"],
            "misses": reranked_cache_metrics["misses"],
            "total_requests": reranked_cache_metrics["hits"] + reranked_cache_metrics["misses"],
            "hit_rate_percent": round(
                (
                    reranked_cache_metrics["hits"]
                    / (reranked_cache_metrics["hits"] + reranked_cache_metrics["misses"])
                    * 100
                )
                if (reranked_cache_metrics["hits"] + reranked_cache_metrics["misses"]) > 0
                else 0,
                2,
            ),
            "current_size": len(reranked_cache),
            "max_size": reranked_cache.maxsize,
            "ttl_seconds": reranked_cache.ttl,
        },
    }


@app.post("/admin/clear-cache")
@limit_with_bypass(RATE_LIMIT_ADMIN)
async def clear_cache(request: Request, response: Response):
    """Clear all caches (search, resource, similar resources)

    Admin-only helper to reset in-memory caches and metrics.
    """
    logger.info("cache_clear_requested")

    # Clear all caches
    search_cache.clear()
    resource_cache.clear()
    similar_cache.clear()
    reranked_cache.clear()

    # Reset metrics
    cache_metrics["hits"] = 0
    cache_metrics["misses"] = 0
    cache_metrics["start_time"] = time.time()
    resource_cache_metrics["hits"] = 0
    resource_cache_metrics["misses"] = 0
    similar_cache_metrics["hits"] = 0
    similar_cache_metrics["misses"] = 0
    reranked_cache_metrics["hits"] = 0
    reranked_cache_metrics["misses"] = 0

    return {
        "status": "success",
        "message": "All caches cleared and metrics reset",
        "timestamp": time.time(),
    }


@app.get("/resource/{vector_id}")
@limit_with_bypass(RATE_LIMIT_RESOURCE)
async def get_resource(
    request: Request, response: Response, vector_id: str, session: Session = Depends(require_member)
):
    """Get a single resource by vector_id (MD5 hash generated from chunk text) - cached for 24 hours - requires active membership"""
    logger.info("resource_request", vector_id=vector_id, user_id=session.user_id)

    # Generate cache key
    cache_key = f"resource:{vector_id}"

    # Check cache
    if cache_key in resource_cache:
        resource_cache_metrics["hits"] += 1
        logger.info("resource_cache_hit", vector_id=vector_id)
        return resource_cache[cache_key]

    resource_cache_metrics["misses"] += 1
    logger.info("resource_cache_miss", vector_id=vector_id)

    # Get Qdrant client
    qdrant_client = get_qdrant_client()

    # Retrieve the point directly by its ID
    result = qdrant_client.retrieve(
        collection_name=COLLECTION_NAME,
        ids=[vector_id],
        with_payload=True,
    )

    if not result or len(result) == 0:
        raise ResourceNotFoundError(resource_type="Resource", resource_id=vector_id)

    # Format the result - return vector_id as the vector_id field
    record = result[0]
    payload = record.payload or {}
    metadata = payload.get("metadata", {})

    resource_data = {
        "vector_id": vector_id,  # Use vector_id (the MD5 hash)
        "title": payload.get("title", ""),
        "text": payload.get("text", ""),
        "doc_type": payload.get("doc_type", ""),
        "metadata": metadata,
    }

    # Store in cache (24-hour TTL is already configured in cache_service)
    resource_cache[cache_key] = resource_data

    return resource_data


@app.get("/embedding/{vector_id}")
@limit_with_bypass(RATE_LIMIT_RESOURCE)
async def get_embedding_diagnostic(request: Request, response: Response, vector_id: str):
    """Get stored embedding info for a document (database inspection only).

    This endpoint retrieves the stored vector from Qdrant and returns
    diagnostic information including dimensions, fingerprint, and statistics.
    Useful for verifying embeddings are stored correctly across environments.
    """
    import hashlib

    import numpy as np

    logger.info("embedding_diagnostic_request", vector_id=vector_id)

    # Get Qdrant client
    qdrant_client = get_qdrant_client()

    # Retrieve the point with vectors
    result = qdrant_client.retrieve(
        collection_name=COLLECTION_NAME,
        ids=[vector_id],
        with_payload=True,
        with_vectors=True,
    )

    if not result or len(result) == 0:
        raise ResourceNotFoundError(resource_type="Resource", resource_id=vector_id)

    record = result[0]
    payload = record.payload or {}

    # Get stored vectors (handle named vectors)
    dense_vector = None
    sparse_vector = None
    if isinstance(record.vector, dict):
        # Named vectors - get 'dense' and 'sparse' vectors
        dense_vector = record.vector.get("dense")
        sparse_vector = record.vector.get("sparse")
    else:
        # Single vector (assume dense)
        dense_vector = record.vector

    if dense_vector is None:
        return {
            "vector_id": vector_id,
            "title": payload.get("title", ""),
            "error": "No dense vector found for this document",
        }

    # Get document info
    text = payload.get("text", "")
    title = payload.get("title", "")

    # Convert to list of floats for consistent handling
    vector_list: list[float] = list(dense_vector)  # type: ignore[arg-type]

    # Compute embedding stats and fingerprint
    emb_array = np.array(vector_list)
    rounded = np.round(emb_array, decimals=4)
    fingerprint = hashlib.md5(rounded.tobytes()).hexdigest()[:16]

    stored_info = {
        "dimensions": len(vector_list),
        "fingerprint": fingerprint,
        "stats": {
            "min": float(np.min(emb_array)),
            "max": float(np.max(emb_array)),
            "mean": float(np.mean(emb_array)),
            "std": float(np.std(emb_array)),
            "l2_norm": float(np.linalg.norm(emb_array)),
        },
        "first_5": [round(x, 6) for x in vector_list[:5]],
        "last_5": [round(x, 6) for x in vector_list[-5:]],
    }

    # Sparse vector diagnostics
    sparse_info = None
    if sparse_vector is not None:
        # Sparse vectors have indices and values attributes
        if hasattr(sparse_vector, "indices") and hasattr(sparse_vector, "values"):
            sparse_info = {
                "populated": True,
                "num_non_zero_terms": len(sparse_vector.indices),
                "sample_indices": list(sparse_vector.indices[:5]) if len(sparse_vector.indices) > 0 else [],
                "sample_values": [round(v, 4) for v in list(sparse_vector.values[:5])]
                if len(sparse_vector.values) > 0
                else [],
            }
        else:
            sparse_info = {"populated": True, "format": "unknown"}
    else:
        sparse_info = {"populated": False}

    return {
        "vector_id": vector_id,
        "title": title,
        "stored_embedding": stored_info,
        "sparse_embedding": sparse_info,
        "text_preview": text[:200] + "..." if len(text) > 200 else text,
    }


@app.get("/resource/{vector_id}/similar", response_model=SimilarResourcesResponse)
@limit_with_bypass(RATE_LIMIT_RESOURCE)
async def get_similar_resources_endpoint(
    request: Request, response: Response, vector_id: str, session: Session = Depends(require_member)
):
    """Get similar resources based on vector similarity - cached for 24 hours - requires active membership"""
    logger.info("similar_resources_request", vector_id=vector_id, user_id=session.user_id)

    # Generate cache key
    cache_key = f"similar:{vector_id}"

    # Check cache
    if cache_key in similar_cache:
        cache_metrics["hits"] += 1
        logger.info("similar_cache_hit", vector_id=vector_id)
        return similar_cache[cache_key]

    cache_metrics["misses"] += 1
    logger.info("similar_cache_miss", vector_id=vector_id)

    # Get Qdrant client
    qdrant_client = get_qdrant_client()

    # Get similar resources
    similar_resources = get_similar_resources(
        qdrant_client=qdrant_client,
        vector_id=vector_id,
        limit=5,
    )

    # Check if source resource was found
    if not similar_resources and vector_id:
        # Try to check if the point exists
        result = qdrant_client.retrieve(
            collection_name=COLLECTION_NAME,
            ids=[vector_id],
            with_payload=False,
        )
        if not result or len(result) == 0:
            raise ResourceNotFoundError(resource_type="Resource", resource_id=vector_id)

    # Convert to response model
    similar_resource_models = [
        SimilarResource(
            vector_id=res["vector_id"],
            resource_id=res["resource_id"],
            title=res["title"],
            score=res["score"],
            metadata=res["metadata"],
        )
        for res in similar_resources
    ]

    similar_response = SimilarResourcesResponse(
        vector_id=vector_id,
        similar_resources=similar_resource_models,
        count=len(similar_resource_models),
    )

    # Store in cache (24-hour TTL is already configured in cache_service)
    similar_cache[cache_key] = similar_response

    return similar_response


logger.info("fastapi_main_loaded")
