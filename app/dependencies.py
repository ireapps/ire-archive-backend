"""Application dependencies and lifecycle management."""

from contextlib import asynccontextmanager
import asyncio
from typing import Any, Optional

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import CreateAlias, CreateAliasOperation, Distance, VectorParams
from redis.asyncio import Redis
from sentence_transformers import CrossEncoder, SentenceTransformer

logger = structlog.get_logger()

from app.auth.config import get_auth_settings
from app.auth.membersuite_client import MemberSuiteClient
from app.auth.session import SessionManager
from app.config import (
    COLLECTION_NAME,
    CROSS_ENCODER_NAME,
    DENSE_MODEL_NAME,
    QDRANT_HOST,
    QDRANT_PORT,
    SPARSE_MODEL_NAME,
    VECTOR_SIZE,
    SERVING_COLLECTION_ALIAS,
)
from app.diagnostics import log_environment_info


# Global state - initialized during lifespan startup
class AppState:
    """Application state container for models and clients"""

    embedding_model: SentenceTransformer | None = None
    sparse_model: SparseTextEmbedding | None = None
    qdrant_client: QdrantClient | None = None
    reranker: CrossEncoder | None = None
    redis: Redis | None = None
    ms_http_client: httpx.AsyncClient | None = None
    session_manager: SessionManager | None = None
    membersuite_client: MemberSuiteClient | None = None
    publication_store: Any = None


# Singleton instance
app_state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager - handles startup and shutdown"""
    # Startup
    logger.info("fastapi_lifespan_startup")

    # Load dense embedding model (PyTorch backend for cross-platform consistency)
    # Uses HuggingFace model name instead of ONNX path for identical inference on Mac/Linux
    logger.info("loading_dense_model", model_name=DENSE_MODEL_NAME)
    try:
        app_state.embedding_model = SentenceTransformer(
            DENSE_MODEL_NAME,
            device="cpu",
            trust_remote_code=True,
        )
        logger.info("dense_model_loaded", backend="pytorch", model=DENSE_MODEL_NAME)
    except Exception as e:
        logger.error("dense_model_load_failed", error=str(e))
        raise

    # Load sparse embedding model (fastembed downloads to its own cache)
    logger.info("loading_sparse_model", model_name=SPARSE_MODEL_NAME)
    try:
        app_state.sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)
        logger.info("sparse_model_loaded", model_name=SPARSE_MODEL_NAME)
    except Exception as e:
        logger.error("sparse_model_load_failed", error=str(e))
        raise

    # Load cross-encoder reranker (PyTorch backend for cross-platform consistency)
    # Uses HuggingFace model name instead of cached path for identical inference on Mac/Linux
    logger.info("loading_cross_encoder", model_name=CROSS_ENCODER_NAME)
    try:
        app_state.reranker = CrossEncoder(
            CROSS_ENCODER_NAME,
            max_length=256,
            device="cpu",
        )
        logger.info("reranker_loaded", backend="pytorch", model=CROSS_ENCODER_NAME)
    except Exception as e:
        logger.error("reranker_load_failed", error=str(e))
        raise

    # Connect to Qdrant
    logger.info("connecting_to_qdrant", host=QDRANT_HOST, port=QDRANT_PORT)
    try:
        app_state.qdrant_client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        logger.info("qdrant_connected")

        # Initialize collection
        collections = app_state.qdrant_client.get_collections().collections
        collection_names = [c.name for c in collections]
        logger.info("existing_collections", collections=collection_names)

        if COLLECTION_NAME not in collection_names:
            app_state.qdrant_client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
            )
            logger.info("collection_created", collection_name=COLLECTION_NAME)
        else:
            info = app_state.qdrant_client.get_collection(COLLECTION_NAME)
            logger.info("collection_exists", collection_name=COLLECTION_NAME, points_count=info.points_count)
        aliases = app_state.qdrant_client.get_aliases().aliases
        if not any(alias.alias_name == SERVING_COLLECTION_ALIAS for alias in aliases):
            app_state.qdrant_client.update_collection_aliases(
                change_aliases_operations=[
                    CreateAliasOperation(
                        create_alias=CreateAlias(collection_name=COLLECTION_NAME, alias_name=SERVING_COLLECTION_ALIAS)
                    )
                ]
            )
            logger.info("serving_alias_created", alias=SERVING_COLLECTION_ALIAS, collection_name=COLLECTION_NAME)

    except Exception as e:
        logger.error("qdrant_connection_failed", error=str(e))
        raise

    # Initialize auth infrastructure if configured
    auth_settings = get_auth_settings()
    if auth_settings.is_configured:
        try:
            # Redis for sessions
            redis_client: Redis = Redis.from_url(
                auth_settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            app_state.redis = redis_client
            logger.info("redis_connected", url=auth_settings.redis_url)

            # HTTP client for MemberSuite API
            app_state.ms_http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,  # We need Location headers
            )
            logger.info("membersuite_http_client_created")

            # Session manager
            app_state.session_manager = SessionManager(redis_client, auth_settings)
            logger.info("session_manager_initialized")

            # MemberSuite client
            app_state.membersuite_client = MemberSuiteClient(auth_settings, app_state.ms_http_client)
            logger.info("membersuite_client_initialized")

            logger.info("auth_infrastructure_initialized")
        except Exception as e:
            logger.error("auth_infrastructure_failed", error=str(e))
            # Don't raise - allow app to start without auth if not fully configured
    else:
        logger.warning(
            "auth_not_configured",
            missing=auth_settings.validate(),
        )

    # Log environment info for cross-platform debugging
    log_environment_info()

    # The store is intentionally SQLite-backed, not an in-memory task registry.
    # It lets queued work and callback delivery survive an application restart.
    from app.publication_service import PublicationStore, PublicationService

    app_state.publication_store = PublicationStore()
    publication_service = PublicationService(
        app_state.publication_store,
        app_state.qdrant_client,
        app_state.embedding_model,
        app_state.sparse_model,
    )
    asyncio.create_task(asyncio.to_thread(publication_service.recover))

    logger.info("fastapi_startup_complete")

    yield

    # Shutdown
    logger.info("fastapi_shutting_down")

    # Cleanup auth resources
    if app_state.redis:
        await app_state.redis.close()
        logger.info("redis_closed")
    if app_state.ms_http_client:
        await app_state.ms_http_client.aclose()
        logger.info("membersuite_http_client_closed")


def get_embedding_model() -> SentenceTransformer:
    """Get the dense embedding model instance"""
    if app_state.embedding_model is None:
        raise RuntimeError("Embedding model not initialized")
    return app_state.embedding_model


def get_sparse_model() -> SparseTextEmbedding:
    """Get the sparse embedding model instance"""
    if app_state.sparse_model is None:
        raise RuntimeError("Sparse embedding model not initialized")
    return app_state.sparse_model


def get_qdrant_client() -> QdrantClient:
    """Get the Qdrant client instance"""
    if app_state.qdrant_client is None:
        raise RuntimeError("Qdrant client not initialized")
    return app_state.qdrant_client


def get_reranker() -> CrossEncoder:
    """Get the cross-encoder reranker instance"""
    if app_state.reranker is None:
        raise RuntimeError("Cross-encoder reranker not initialized")
    return app_state.reranker


def get_session_manager(request: Request) -> SessionManager:
    """Get session manager from app state."""
    if app_state.session_manager is None:
        raise HTTPException(503, "Authentication service not configured")
    return app_state.session_manager


def get_membersuite_client(request: Request) -> MemberSuiteClient:
    """Get MemberSuite client from app state."""
    if app_state.membersuite_client is None:
        raise HTTPException(503, "Authentication service not configured")
    return app_state.membersuite_client


def get_publication_service() -> Any:
    """Build a publication worker around the already initialized application clients."""
    from app.publication_service import PublicationService, PublicationStore

    if app_state.publication_store is None:
        app_state.publication_store = PublicationStore()
    return PublicationService(
        app_state.publication_store,
        get_qdrant_client(),
        get_embedding_model(),
        get_sparse_model(),
    )
