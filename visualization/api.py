"""Standalone FastAPI server for the embedding visualization.

Run separately from the main archive API so that ``umap-learn`` and its
heavy transitive dependencies never ship to the Fly.io production image.

Usage — see the README in this directory for full setup instructions::

    cd visualization
    uvicorn api:app --port 8001

The SvelteKit frontend defaults to ``http://localhost:8001`` (see
``VITE_API_BASE_URL`` in the visualization README).
"""

import os
import time
from typing import Any

import structlog
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from qdrant_client import QdrantClient

from visualization_service import generate_embedding_map

load_dotenv()

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()

# --- Configuration (mirrors main app defaults) ---
QDRANT_HOST: str = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT: int = int(os.getenv("QDRANT_PORT", "6333"))

# --- Qdrant client ---
qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, timeout=60)

# --- FastAPI app ---
app = FastAPI(title="IRE Embedding Visualization API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:4173",  # Vite preview
    ],
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# --- In-memory cache ---
_cache: dict[str, tuple[Any, float]] = {}
_CACHE_TTL: int = 3600  # 1 hour


@app.get("/visualize/embeddings")
async def get_embedding_map(
    sample: int | None = None,
    n_neighbors: int = 15,
    min_dist: float = 0.1,
):
    """Return 2D UMAP projection of all document embeddings.

    Query parameters:
        sample: Cap on number of points (faster for dev).
        n_neighbors: UMAP locality (default 15).
        min_dist: UMAP minimum distance (default 0.1).
    """
    cache_key = f"{sample}:{n_neighbors}:{min_dist}"

    if cache_key in _cache:
        cached_result, cached_at = _cache[cache_key]
        if (time.time() - cached_at) < _CACHE_TTL:
            logger.info("embedding_map_cache_hit")
            return cached_result

    logger.info(
        "embedding_map_request",
        sample=sample,
        n_neighbors=n_neighbors,
        min_dist=min_dist,
    )

    result = generate_embedding_map(
        qdrant_client,
        sample_limit=sample,
        n_neighbors=n_neighbors,
        min_dist=min_dist,
    )

    _cache[cache_key] = (result, time.time())
    return result


@app.get("/healthz")
async def healthz():
    """Health check."""
    return {"status": "ok"}
