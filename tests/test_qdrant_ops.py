"""Coverage for the overlapped embed/upload indexing pipeline in scripts/qdrant_ops.py."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import scripts.qdrant_ops as qdrant_ops
from scripts.qdrant_ops import get_batch_list, index_batches, wait_for_indexed


class _Vector:
    """Stand-in for a numpy row: only needs .tolist() like a real embedding."""

    def __init__(self, value: list[float]) -> None:
        self._value = value

    def tolist(self) -> list[float]:
        return self._value


class _SparseVector:
    def as_object(self) -> dict[str, list[Any]]:
        return {"indices": [], "values": []}


class _FakeDenseModel:
    def encode(self, texts: list[str], batch_size: int = 128, show_progress_bar: bool = False) -> list[_Vector]:
        return [_Vector([float(len(text))]) for text in texts]


class _FakeSparseModel:
    def embed(self, texts: list[str]) -> list[_SparseVector]:
        return [_SparseVector() for _ in texts]


class _FakeQdrantClient:
    """Thread-safe fake: records upsert calls in order and tallies points."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.upsert_calls: list[list[str]] = []
        self.points_by_id: dict[str, Any] = {}
        self.fail_batch_ids: set[str] = set()

    def upsert(self, *, collection_name: str, points: list[Any], wait: bool) -> None:
        assert wait is False, "batches must upload with wait=False"
        if any(point.id in self.fail_batch_ids for point in points):
            raise RuntimeError("simulated upload failure")
        with self._lock:
            self.upsert_calls.append([point.id for point in points])
            for point in points:
                self.points_by_id[point.id] = point

    def get_collection(self, _collection_name: str) -> SimpleNamespace:
        with self._lock:
            return SimpleNamespace(points_count=len(self.points_by_id))


def _documents(count: int) -> list[dict[str, Any]]:
    return [
        {"id": f"doc-{i}", "title": f"Title {i}", "doc_type": "ire_resource", "metadata": {}, "text": f"text {i}"}
        for i in range(count)
    ]


def _run_index_batches(
    client: _FakeQdrantClient,
    batches: list[list[dict[str, Any]]],
    errors: list[str],
) -> tuple[int, int]:
    """Call index_batches with fake models, cast for the real function's type hints."""
    return index_batches(
        cast(Any, client), batches, cast(Any, _FakeDenseModel()), cast(Any, _FakeSparseModel()), errors
    )


class TestIndexBatchesCorrectness:
    def test_all_points_uploaded_exactly_once_in_order(self):
        client = _FakeQdrantClient()
        docs = _documents(9)
        batches = get_batch_list(docs, batch_size=2)
        errors: list[str] = []

        successful, failed = _run_index_batches(client, batches, errors)

        assert successful == 9
        assert failed == 0
        assert errors == []
        assert set(client.points_by_id) == {doc["id"] for doc in docs}
        # Batches were uploaded in submission order, one at a time.
        uploaded_ids = [point_id for call in client.upsert_calls for point_id in call]
        assert uploaded_ids == [doc["id"] for doc in docs]

    def test_batches_upload_with_wait_false(self):
        client = _FakeQdrantClient()
        batches = get_batch_list(_documents(3), batch_size=3)
        errors: list[str] = []

        # _FakeQdrantClient.upsert asserts wait=False internally; a wait=True
        # call would raise instead of an AssertionError bubbling up as a
        # per-batch failure, so success here already proves the contract.
        successful, failed = _run_index_batches(client, batches, errors)

        assert successful == 3
        assert failed == 0

    def test_one_failing_batch_does_not_drop_or_reorder_others(self, monkeypatch):
        monkeypatch.setattr(qdrant_ops.time, "sleep", lambda _seconds: None)  # skip retry backoff
        client = _FakeQdrantClient()
        docs = _documents(6)
        client.fail_batch_ids = {"doc-2", "doc-3"}  # the second batch of 2
        batches = get_batch_list(docs, batch_size=2)
        errors: list[str] = []

        successful, failed = _run_index_batches(client, batches, errors)

        assert successful == 4
        assert failed == 2
        assert any("Batch 2" in error for error in errors)
        assert set(client.points_by_id) == {"doc-0", "doc-1", "doc-4", "doc-5"}
        uploaded_ids = [point_id for call in client.upsert_calls for point_id in call]
        assert uploaded_ids == ["doc-0", "doc-1", "doc-4", "doc-5"]


class TestIndexBatchesOverlap:
    def test_next_batch_embeds_while_previous_upload_is_in_flight(self, monkeypatch):
        client = _FakeQdrantClient()
        docs = _documents(3)
        batches = get_batch_list(docs, batch_size=1)
        errors: list[str] = []

        embed_started: list[str] = []
        real_generate_embeddings_batch = qdrant_ops.generate_embeddings_batch

        def spying_generate_embeddings_batch(dense_model, sparse_model, texts, **kwargs):
            embed_started.append(texts[0])
            return real_generate_embeddings_batch(dense_model, sparse_model, texts, **kwargs)

        monkeypatch.setattr(qdrant_ops, "generate_embeddings_batch", spying_generate_embeddings_batch)

        upload_started = threading.Event()
        release_upload = threading.Event()
        real_upsert = client.upsert

        def blocking_upsert(**kwargs):
            first_batch = len(client.upsert_calls) == 0
            if first_batch:
                upload_started.set()
                assert release_upload.wait(timeout=5), "test never released the blocked upload"
            return real_upsert(**kwargs)

        monkeypatch.setattr(client, "upsert", blocking_upsert)

        result: dict[str, tuple[int, int]] = {}

        def run_index() -> None:
            result["value"] = _run_index_batches(client, batches, errors)

        worker = threading.Thread(target=run_index)
        worker.start()
        try:
            assert upload_started.wait(timeout=5), "first batch's upload never started"
            deadline = time.time() + 5
            while len(embed_started) < 2 and time.time() < deadline:
                time.sleep(0.01)
            assert len(embed_started) >= 2, "batch 2 should embed while batch 1's upload is still in flight"
        finally:
            release_upload.set()
            worker.join(timeout=5)
        assert not worker.is_alive()

        successful, failed = result["value"]
        assert successful == 3
        assert failed == 0


class TestWaitForIndexed:
    def test_returns_once_expected_count_is_reached(self):
        client = _FakeQdrantClient()
        for i in range(3):
            client.points_by_id[f"doc-{i}"] = object()

        count = wait_for_indexed(cast(Any, client), "collection", expected_count=3, timeout=1, poll_interval=0.01)

        assert count == 3

    def test_gives_up_after_timeout_when_count_never_catches_up(self):
        client = _FakeQdrantClient()
        client.points_by_id["doc-0"] = object()  # only 1 of the expected 5

        start = time.monotonic()
        count = wait_for_indexed(cast(Any, client), "collection", expected_count=5, timeout=0.2, poll_interval=0.05)
        elapsed = time.monotonic() - start

        assert count == 1
        assert elapsed >= 0.2
