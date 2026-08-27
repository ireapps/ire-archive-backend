"""Focused coverage for snapshot publication safety and public API compatibility."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.dependencies import get_publication_service
from app.main import app
from app.publication_contract import SnapshotValidationError, searchable_text, validate_snapshot
from app.publication_service import (
    FAILED,
    QUEUED,
    SUCCEEDED,
    PublicationDescriptor,
    PublicationError,
    PublicationService,
    PublicationStore,
    _is_allowed_https_url,
    verify_request_signature,
)


def _record(number: int = 1, *, extracted_text: str = "Transcript text") -> dict:
    return {
        "id": number,
        "public_id": f"00000000-0000-4000-8000-{number:012d}",
        "resource_id": f"legacy-{number}",
        "post_id": None,
        "title": f"Resource {number}",
        "description": "Description",
        "category": "tipsheet",
        "event_legacy_source_id": None,
        "event_name": None,
        "event_type": None,
        "year_computed": 2024,
        "published_at": None,
        "source_created_at": None,
        "published_at_verified": False,
        "missing_file": False,
        "authors": "Jane Doe",
        "affiliations": "IRE",
        "authors_extracted": [{"name": "Jane Doe"}],
        "affiliations_extracted": [{"name": "IRE"}],
        "contest_entries": [],
        "downloads": [
            {
                "id": f"10000000-0000-4000-8000-{number:012d}",
                "file": "resource.pdf",
                "url": "https://resources.ire.org/resource.pdf",
                "name": "Resource PDF",
                "filesize": 1,
                "sort_order": 0,
                "extracted_text": extracted_text,
                "extracted_text_source": "document_extraction" if extracted_text else None,
            }
        ],
    }


def _snapshot(records: list[dict]) -> dict:
    stable = [{key: value for key, value in record.items() if key != "id"} for record in records]
    checksum = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": "2.0",
        "publication_id": "6f5db2b1-4f4f-4f5d-8b2e-3dcce9fd44d3",
        "created_at": "2026-08-26T00:00:00Z",
        "record_count": len(records),
        "checksum": checksum,
        "records": records,
    }


def _write_snapshot(tmp_path: Path, snapshot: dict) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def _descriptor() -> PublicationDescriptor:
    return PublicationDescriptor(
        publication_id="6f5db2b1-4f4f-4f5d-8b2e-3dcce9fd44d3",
        publication_version="2026.08.26.1",
        schema_version="2.0",
        snapshot_url="https://storage.example/approved/snapshot.json",
        checksum="a" * 64,
        callback_url="https://data.example/status",
    )


class TestStreamingSnapshotValidation:
    def test_accepts_v2_and_includes_extracted_text(self, tmp_path: Path):
        path = _write_snapshot(tmp_path, _snapshot([_record()]))

        metadata = validate_snapshot(path)

        assert metadata["record_count"] == 1
        assert "Transcript text" in searchable_text(_record())

    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            (lambda snapshot: snapshot.update(schema_version="1.0"), "supported version"),
            (lambda snapshot: snapshot.update(record_count=2), "record_count"),
            (lambda snapshot: snapshot.update(checksum="0" * 64), "checksum"),
            (lambda snapshot: snapshot.update(records=[_record(2), _record(1)]), "ordered"),
        ],
    )
    def test_rejects_invalid_envelope(self, tmp_path: Path, change, expected: str):
        snapshot = _snapshot([_record()])
        change(snapshot)
        with pytest.raises(SnapshotValidationError, match=expected):
            validate_snapshot(_write_snapshot(tmp_path, snapshot))

    def test_checksum_ignores_transitional_django_id(self, tmp_path: Path):
        snapshot = _snapshot([_record()])
        snapshot["records"][0]["id"] = 999

        assert validate_snapshot(_write_snapshot(tmp_path, snapshot))["record_count"] == 1

    def test_rejects_extracted_text_at_configured_boundary(self, tmp_path: Path, monkeypatch):
        import app.publication_contract as publication_contract

        monkeypatch.setattr(publication_contract, "MAX_EXTRACTED_TEXT_CHARS", 5_000_000)
        snapshot = _snapshot([_record(extracted_text="x" * 5_000_001)])

        with pytest.raises(SnapshotValidationError, match="character limit"):
            validate_snapshot(_write_snapshot(tmp_path, snapshot))

    def test_accepts_extracted_text_at_configured_boundary(self, tmp_path: Path, monkeypatch):
        import app.publication_contract as publication_contract

        monkeypatch.setattr(publication_contract, "MAX_EXTRACTED_TEXT_CHARS", 5_000_000)

        assert (
            validate_snapshot(_write_snapshot(tmp_path, _snapshot([_record(extracted_text="x" * 5_000_000)])))[
                "record_count"
            ]
            == 1
        )

    def test_rejects_oversized_single_record_before_ijson_parse(self, tmp_path: Path, monkeypatch):
        import app.publication_contract as publication_contract

        monkeypatch.setattr(publication_contract, "MAX_RECORD_BYTES", 100)
        snapshot = _snapshot([_record(extracted_text="x" * 200)])

        with pytest.raises(SnapshotValidationError, match="byte limit"):
            validate_snapshot(_write_snapshot(tmp_path, snapshot))

    def test_rejects_duplicate_public_and_download_ids(self, tmp_path: Path):
        first = _record(1)
        duplicate_public = _record(2)
        duplicate_public["public_id"] = first["public_id"]
        with pytest.raises(SnapshotValidationError, match="unique"):
            validate_snapshot(_write_snapshot(tmp_path, _snapshot([first, duplicate_public])))

        duplicate_download = _record(2)
        duplicate_download["downloads"][0]["id"] = first["downloads"][0]["id"]
        with pytest.raises(SnapshotValidationError, match="unique"):
            validate_snapshot(_write_snapshot(tmp_path, _snapshot([first, duplicate_download])))


class TestPublicationAuthenticationAndState:
    def test_signature_uses_exact_body_and_nonce_cannot_replay(self, tmp_path: Path, monkeypatch):
        import app.publication_service as publication_service

        monkeypatch.setattr(publication_service, "PUBLICATION_DISPATCH_SECRET", "dispatch-secret")
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        body = b'{"a": 1}'
        timestamp = str(int(time.time()))
        nonce = str(uuid.uuid4())
        signed = f"{timestamp}.{nonce}.".encode() + body
        signature = hmac.new(b"dispatch-secret", signed, hashlib.sha256).hexdigest()
        headers = {
            "X-IRE-Publication-Timestamp": timestamp,
            "X-IRE-Publication-Nonce": nonce,
            "X-IRE-Publication-Signature": f"sha256={signature}",
        }

        verify_request_signature(headers, body, store)
        with pytest.raises(PublicationError, match="already been used"):
            verify_request_signature(headers, body, store)

    def test_exact_replay_is_idempotent_and_changed_version_conflicts(self, tmp_path: Path):
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        descriptor = _descriptor()

        state, idempotent = store.enqueue(descriptor)
        replay, replay_idempotent = store.enqueue(descriptor)
        changed = PublicationDescriptor(**{**descriptor.__dict__, "checksum": "b" * 64})

        assert state["status"] == QUEUED
        assert not idempotent
        assert replay["publication_id"] == descriptor.publication_id
        assert replay_idempotent
        with pytest.raises(PublicationError) as error:
            store.enqueue(changed)
        assert error.value.status_code == 409


class _FakeQdrant:
    def __init__(self) -> None:
        self.aliases = [SimpleNamespace(alias_name="nonprofit_knowledge_live", collection_name="legacy")]
        self.legacy_points = [
            SimpleNamespace(
                id="old-resource-id",
                payload={"title": "Resource 1", "metadata": {"id": 1}},
            )
        ]
        self.points_count = 0
        self.alias_operations: list[object] = []
        self.last_points: list[Any] = []
        self.upload_batches = 0
        self.deleted: list[str] = []
        self.collection_names = ["legacy"]

    def get_aliases(self):
        return SimpleNamespace(aliases=self.aliases)

    def scroll(self, **kwargs):
        return self.legacy_points, None

    def upsert(self, **kwargs):
        self.last_points = kwargs["points"]
        self.points_count += len(kwargs["points"])
        self.upload_batches += 1

    def get_collection(self, _name):
        return SimpleNamespace(points_count=self.points_count)

    def query_points(self, **kwargs):
        return SimpleNamespace(points=[object()])

    def update_collection_aliases(self, *, change_aliases_operations):
        self.alias_operations = change_aliases_operations
        create = change_aliases_operations[-1].create_alias
        self.aliases = [SimpleNamespace(alias_name=create.alias_name, collection_name=create.collection_name)]

    def delete_collection(self, collection_name):
        self.deleted.append(collection_name)

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name=name) for name in self.collection_names])


class TestCollectionPublication:
    def test_success_switches_alias_only_after_build_and_maps_confident_legacy_id(self, tmp_path: Path, monkeypatch):
        import app.publication_service as publication_service

        snapshot = _snapshot([_record()])
        path = _write_snapshot(tmp_path, snapshot)
        descriptor = PublicationDescriptor(**{**_descriptor().__dict__, "checksum": snapshot["checksum"]})
        qdrant = _FakeQdrant()
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        store.enqueue(descriptor)
        sparse = SimpleNamespace(as_object=lambda: {"indices": [], "values": []})
        service = PublicationService(
            store,
            cast(Any, qdrant),
            cast(Any, SimpleNamespace(encode=lambda _: SimpleNamespace(tolist=lambda: [0.1]))),
            cast(Any, None),
        )
        monkeypatch.setattr(publication_service, "create_hybrid_collection", lambda *args, **kwargs: True)
        monkeypatch.setattr(
            publication_service, "generate_embeddings_batch", lambda *args, **kwargs: ([[0.1]], [sparse])
        )
        monkeypatch.setattr(publication_service, "PUBLICATION_CALLBACK_SECRET", None)
        monkeypatch.setattr(service, "_download", lambda _: path)

        service.run(descriptor)

        state = store.get(descriptor.publication_id, descriptor.publication_version)
        assert state["status"] == SUCCEEDED
        assert qdrant.aliases[0].collection_name == state["collection_name"]
        assert qdrant.alias_operations
        assert qdrant.points_count == 1
        assert qdrant.last_points[0].id == snapshot["records"][0]["public_id"]
        assert store.active_public_id("old-resource-id") == snapshot["records"][0]["public_id"]

        qdrant.aliases = [SimpleNamespace(alias_name="nonprofit_knowledge_live", collection_name="newer-publication")]
        with pytest.raises(PublicationError, match="currently serving"):
            service.rollback(descriptor)
        qdrant.aliases = [
            SimpleNamespace(alias_name="nonprofit_knowledge_live", collection_name=state["collection_name"])
        ]
        rolled_back = service.rollback(descriptor)

        assert rolled_back["status"] == "rolled_back"
        assert qdrant.aliases[0].collection_name == "legacy"

    def test_uploads_are_bounded_by_payload_bytes(self, tmp_path: Path, monkeypatch):
        import app.publication_service as publication_service

        snapshot = _snapshot([_record(1, extracted_text="x" * 500), _record(2, extracted_text="x" * 500)])
        path = _write_snapshot(tmp_path, snapshot)
        qdrant = _FakeQdrant()
        service = PublicationService(
            PublicationStore(str(tmp_path / "state.sqlite")),
            cast(Any, qdrant),
            cast(Any, SimpleNamespace()),
            cast(Any, None),
        )
        service.max_upload_bytes = 2_000
        sparse = SimpleNamespace(as_object=lambda: {"indices": [], "values": []})
        monkeypatch.setattr(
            publication_service, "generate_embeddings_batch", lambda *args, **kwargs: ([[0.1]], [sparse])
        )

        assert service._upload_records(path, "new-collection", {})[0] == 2
        assert qdrant.upload_batches == 2

    def test_failed_build_keeps_live_alias_unchanged(self, tmp_path: Path, monkeypatch):
        import app.publication_service as publication_service

        snapshot = _snapshot([_record()])
        path = _write_snapshot(tmp_path, snapshot)
        descriptor = PublicationDescriptor(**{**_descriptor().__dict__, "checksum": snapshot["checksum"]})
        qdrant = _FakeQdrant()
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        store.enqueue(descriptor)
        service = PublicationService(store, cast(Any, qdrant), cast(Any, SimpleNamespace()), cast(Any, None))
        monkeypatch.setattr(publication_service, "create_hybrid_collection", lambda *args, **kwargs: True)
        monkeypatch.setattr(publication_service, "PUBLICATION_CALLBACK_SECRET", None)
        monkeypatch.setattr(service, "_download", lambda _: path)
        monkeypatch.setattr(
            service, "_upload_records", lambda *args: (_ for _ in ()).throw(PublicationError("TEST", "failed", 422))
        )

        service.run(descriptor)

        assert store.get(descriptor.publication_id, descriptor.publication_version)["status"] == FAILED
        assert qdrant.aliases[0].collection_name == "legacy"
        assert qdrant.deleted

    def test_terminal_transition_persists_outbox_in_same_commit(self, tmp_path: Path):
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        descriptor = _descriptor()
        store.enqueue(descriptor)

        state, event = store.transition_with_event(descriptor, SUCCEEDED, collection_name="published")

        assert state["status"] == SUCCEEDED
        restarted_store = PublicationStore(str(tmp_path / "state.sqlite"))
        assert event["event_id"] in {item["event_id"] for item in restarted_store.outstanding_events()}

    def test_recovery_finds_legacy_terminal_state_without_callback(self, tmp_path: Path):
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        descriptor = _descriptor()
        store.enqueue(descriptor)
        store.update(descriptor, FAILED, error_code="TEST", message="interrupted before callback")

        missing = store.terminal_states_without_events()

        assert [(item.publication_id, state["status"]) for item, state in missing] == [
            (descriptor.publication_id, FAILED)
        ]

    def test_prune_retains_live_and_one_rollback_collection(self, tmp_path: Path):
        qdrant = _FakeQdrant()
        qdrant.aliases = [SimpleNamespace(alias_name="nonprofit_knowledge_live", collection_name="new")]
        qdrant.collection_names = ["legacy", "new", "nonprofit_knowledge__publication__obsolete"]
        service = PublicationService(
            PublicationStore(str(tmp_path / "state.sqlite")),
            cast(Any, qdrant),
            cast(Any, SimpleNamespace()),
            cast(Any, None),
        )

        service._prune_collections("new", "legacy")

        assert qdrant.deleted == ["nonprofit_knowledge__publication__obsolete"]

    def test_rollback_serializes_with_build_lock(self, tmp_path: Path):
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        descriptor = _descriptor()
        store.enqueue(descriptor)
        assert store.acquire_build_lock(descriptor)
        assert not store.acquire_build_lock(_descriptor())
        store.release_build_lock(descriptor)

    def test_rejects_path_traversal_in_allowlisted_url(self):
        assert not _is_allowed_https_url(
            "https://storage.example/approved/../private/snapshot.json",
            (),
            ("https://storage.example/approved/",),
        )


class TestPublicationRoutes:
    def test_dispatch_and_authenticated_status(self, client, tmp_path: Path, monkeypatch):
        import app.publication_service as publication_service

        monkeypatch.setattr(publication_service, "PUBLICATION_DISPATCH_SECRET", "dispatch-secret")
        monkeypatch.setattr(publication_service, "PUBLICATION_CALLBACK_SECRET", "callback-secret")
        monkeypatch.setattr(
            publication_service, "PUBLICATION_SNAPSHOT_URL_PREFIXES", ("https://storage.example/approved/",)
        )
        monkeypatch.setattr(publication_service, "PUBLICATION_CALLBACK_URLS", ("https://data.example/status",))
        store = PublicationStore(str(tmp_path / "state.sqlite"))
        fake_service = SimpleNamespace(
            store=store,
            max_request_bytes=16384,
            report=lambda descriptor, state: None,
            run=lambda descriptor: None,
        )
        app.dependency_overrides[get_publication_service] = lambda: fake_service
        descriptor = {**_descriptor().__dict__, "snapshot_url": "https://storage.example/approved/snapshot.json"}
        body = json.dumps(descriptor, separators=(",", ":")).encode()

        def headers(body: bytes) -> dict[str, str]:
            timestamp = str(int(time.time()))
            nonce = str(uuid.uuid4())
            signature = hmac.new(
                b"dispatch-secret", f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256
            ).hexdigest()
            return {
                "Content-Type": "application/json",
                "X-IRE-Publication-Timestamp": timestamp,
                "X-IRE-Publication-Nonce": nonce,
                "X-IRE-Publication-Signature": f"sha256={signature}",
            }

        response = client.post("/internal/publications", content=body, headers=headers(body))
        status = client.get(
            f"/internal/publications/{descriptor['publication_id']}/{descriptor['publication_version']}",
            headers={key: value for key, value in headers(b"").items() if key != "Content-Type"},
        )

        assert response.status_code == 202
        assert response.json()["status"] == QUEUED
        assert status.status_code == 200
        assert status.json()["status"] == QUEUED
