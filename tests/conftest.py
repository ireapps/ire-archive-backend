"""Pytest configuration and shared fixtures for backend tests."""

from unittest.mock import MagicMock, Mock
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct, Record, ScoredPoint

from app.auth.dependencies import require_member
from app.auth.session import Session
from app.dependencies import (
    get_embedding_model,
    get_qdrant_client,
    get_sparse_model,
)
from app.main import app


@pytest.fixture
def mock_auth_session():
    """Mock authenticated session for testing protected endpoints."""
    import time

    session = Session(
        session_id="test-session-id",
        user_id="test-user-123",
        email="test@example.com",
        first_name="Test",
        last_name="User",
        full_name="Test User",
        is_active_member=True,
        membership_id="test-membership-456",
        created_at=time.time(),
        expires_at=9999999999,  # Far future
    )
    return session


@pytest.fixture(autouse=True)
def override_auth_dependency(mock_auth_session):
    """Override auth dependency to bypass authentication in tests."""

    async def mock_require_member():
        """Mock require_member dependency that returns a test session."""
        return mock_auth_session

    # Override the dependency
    app.dependency_overrides[require_member] = mock_require_member

    yield

    # Clean up
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    """FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def mock_qdrant_client():
    """Mock Qdrant client for testing without database."""
    # Use Mock without spec to allow adding methods dynamically
    mock = Mock()

    # Mock collection info
    mock_collection_info = Mock()
    mock_collection_info.points_count = 1000
    mock_collection_info.status = "green"
    mock_collection_info.indexed_vectors_count = 1000
    mock.get_collection.return_value = mock_collection_info
    mock.get_aliases.return_value = SimpleNamespace(
        aliases=[SimpleNamespace(alias_name="nonprofit_knowledge_live", collection_name="nonprofit_knowledge")]
    )

    # Add search method for similar resources
    mock.search.return_value = []

    # Mock scroll method - returns tuple (results, next_page_offset)
    mock.scroll.return_value = ([], None)

    # Mock count method
    mock.count.return_value = Mock(count=0)

    # Mock query_points method (used by recommendation service)
    mock_query_result = Mock()
    mock_query_result.points = []
    mock.query_points.return_value = mock_query_result

    # Mock retrieve method (used by resource endpoint)
    mock.retrieve.return_value = []

    return mock


@pytest.fixture
def mock_embedding_model():
    """Mock embedding model."""
    import numpy as np

    mock = Mock()
    # Return numpy array so .tolist() works in search_service
    mock.encode.return_value = np.array([0.1] * 384)
    return mock


@pytest.fixture
def mock_sparse_model():
    """Mock sparse embedding model."""
    mock = Mock()

    # Mock sparse embedding output - fastembed uses .embed() which returns a generator
    mock_result = Mock()
    mock_result.indices = [1, 5, 10, 20]
    mock_result.values = [0.5, 0.3, 0.2, 0.1]

    # Mock as_object() method to return a valid SparseVector-like dict
    mock_result.as_object.return_value = {
        "indices": [1, 5, 10, 20],
        "values": [0.5, 0.3, 0.2, 0.1],
    }

    # Return an iterable (list) from embed method
    mock.embed.return_value = [mock_result]
    # Also mock passage_embed for backward compatibility
    mock.passage_embed.return_value = [mock_result]
    return mock


@pytest.fixture(autouse=True)
def mock_cross_encoder(monkeypatch):
    """Mock cross-encoder for reranking tests."""
    import numpy as np

    mock_reranker = Mock()

    # Return variable-length scores based on input
    # All scores above -5.0 threshold to pass filtering
    def mock_predict(pairs, batch_size=16):
        # Return scores for each pair (higher = better relevance)
        n = len(pairs)
        # Generate scores from 0.9 down to 0.5 based on position
        scores = [0.9 - (i * 0.1) for i in range(n)]
        return np.array(scores)

    mock_reranker.predict = mock_predict

    # Mock the get_reranker function to return our mock
    def mock_get_reranker():
        return mock_reranker

    monkeypatch.setattr("app.services.reranking_service.get_reranker", mock_get_reranker)
    return mock_reranker


@pytest.fixture
def sample_search_results():
    """Sample Qdrant search results."""
    return [
        ScoredPoint(
            id="test-uuid-1",
            score=0.95,
            payload={
                "title": "Test Resource 1",
                "text": "This is a test resource about data journalism.",
                "doc_type": "tipsheet",
                "metadata": {
                    "resource_id": "12345",
                    "authors": "John Doe",
                    "affiliations": "Test News",
                    "category": "tipsheet",
                    "tags": ["data", "journalism"],
                    "resource_date_created": "2024-01-01",
                },
            },
            version=1,
        ),
        ScoredPoint(
            id="test-uuid-2",
            score=0.85,
            payload={
                "title": "Test Resource 2",
                "text": "Another test resource about investigative reporting.",
                "doc_type": "audio",
                "metadata": {
                    "resource_id": "12346",
                    "authors": "Jane Smith",
                    "affiliations": "News Corp",
                    "category": "audio",
                    "tags": ["investigation", "reporting"],
                    "resource_date_created": "2024-01-02",
                },
            },
            version=1,
        ),
    ]


@pytest.fixture
def sample_point_record():
    """Sample Qdrant point record."""
    return Record(
        id="test-uuid-1",
        payload={
            "title": "Test Resource 1",
            "text": "This is a test resource about data journalism.",
            "doc_type": "tipsheet",
            "metadata": {
                "resource_id": "12345",
                "authors": "John Doe",
                "affiliations": "Test News",
                "category": "tipsheet",
                "tags": ["data", "journalism"],
                "resource_date_created": "2024-01-01",
            },
        },
        vector=None,
    )


@pytest.fixture(autouse=True)
def override_dependencies(mock_qdrant_client, mock_embedding_model, mock_sparse_model):
    """Override app_state with mock dependencies for all tests."""
    from app.dependencies import app_state

    # Store original values
    original_qdrant = app_state.qdrant_client
    original_embedding = app_state.embedding_model
    original_sparse = app_state.sparse_model

    # Set mock values
    app_state.qdrant_client = mock_qdrant_client
    app_state.embedding_model = mock_embedding_model
    app_state.sparse_model = mock_sparse_model

    yield

    # Restore original values
    app_state.qdrant_client = original_qdrant
    app_state.embedding_model = original_embedding
    app_state.sparse_model = original_sparse


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear all caches before each test."""
    from app.services.cache_service import (
        reranked_cache,
        resource_cache,
        search_cache,
        similar_cache,
    )

    search_cache.clear()
    resource_cache.clear()
    similar_cache.clear()
    reranked_cache.clear()

    yield

    # Clean up after test
    search_cache.clear()
    resource_cache.clear()
    similar_cache.clear()
    reranked_cache.clear()
