"""Durable, alias-based publication of validated resource snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import structlog
from fastembed import SparseTextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import CreateAlias, CreateAliasOperation, DeleteAlias, DeleteAliasOperation, PointStruct
from sentence_transformers import SentenceTransformer

from app.config import (
    BATCH_SIZE,
    COLLECTION_NAME,
    PUBLICATION_CALLBACK_RETRIES,
    PUBLICATION_CALLBACK_SECRET,
    PUBLICATION_CALLBACK_URL_PREFIXES,
    PUBLICATION_CALLBACK_URLS,
    PUBLICATION_DISPATCH_SECRET,
    PUBLICATION_MAX_REQUEST_BYTES,
    PUBLICATION_MAX_SNAPSHOT_BYTES,
    PUBLICATION_MAX_UPLOAD_BYTES,
    PUBLICATION_SIGNATURE_MAX_AGE_SECONDS,
    PUBLICATION_SNAPSHOT_URL_PREFIXES,
    PUBLICATION_STATE_DB,
    PUBLICATION_WORK_DIR,
    SERVING_COLLECTION_ALIAS,
    VECTOR_SIZE,
)
from app.publication_contract import (
    SCHEMA_VERSION,
    SnapshotValidationError,
    iter_records,
    resource_metadata,
    searchable_text,
    validate_snapshot,
)
from app.services.cache_service import clear_publication_caches
from scripts.qdrant_ops import create_hybrid_collection, generate_embeddings_batch

logger = structlog.get_logger()

QUEUED = "queued"
BUILDING = "building"
SUCCEEDED = "succeeded"
FAILED = "failed"
ROLLED_BACK = "rolled_back"


class PublicationError(ValueError):
    """A safe, client-facing publication error."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class PublicationDescriptor:
    """The exact data-side dispatch payload, after FastAPI has validated it."""

    publication_id: str
    publication_version: str
    schema_version: str
    snapshot_url: str
    checksum: str
    callback_url: str

    def canonical_bytes(self) -> bytes:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()

    @property
    def descriptor_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


class PublicationStore:
    """SQLite state shared across requests, worker restarts, and callback retries."""

    def __init__(self, path: str = PUBLICATION_STATE_DB) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS publications (
                    publication_id TEXT NOT NULL,
                    publication_version TEXT NOT NULL,
                    descriptor_hash TEXT NOT NULL,
                    descriptor_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    collection_name TEXT,
                    previous_collection_name TEXT,
                    record_count INTEGER,
                    point_count INTEGER,
                    error_code TEXT,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (publication_id, publication_version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS publication_id_unique ON publications(publication_id);
                CREATE UNIQUE INDEX IF NOT EXISTS publication_version_unique ON publications(publication_version);
                CREATE TABLE IF NOT EXISTS publication_nonces (
                    nonce TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_events (
                    event_id TEXT PRIMARY KEY,
                    publication_id TEXT NOT NULL,
                    publication_version TEXT NOT NULL,
                    body TEXT NOT NULL,
                    callback_url TEXT NOT NULL,
                    delivered_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publication_build_lock (
                    lock_name TEXT PRIMARY KEY,
                    publication_id TEXT NOT NULL,
                    publication_version TEXT NOT NULL
                );
                """
            )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def consume_nonce(self, nonce: str, expires_at: int) -> bool:
        with self._connection() as connection:
            connection.execute("DELETE FROM publication_nonces WHERE expires_at < ?", (int(time.time()),))
            try:
                connection.execute(
                    "INSERT INTO publication_nonces (nonce, expires_at) VALUES (?, ?)", (nonce, expires_at)
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def enqueue(self, descriptor: PublicationDescriptor) -> tuple[dict[str, Any], bool]:
        now = datetime.now(UTC).isoformat()
        descriptor_json = descriptor.canonical_bytes().decode()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM publications WHERE publication_id = ? OR publication_version = ?",
                (descriptor.publication_id, descriptor.publication_version),
            ).fetchone()
            if existing:
                if (
                    existing["publication_id"] == descriptor.publication_id
                    and existing["publication_version"] == descriptor.publication_version
                    and hmac.compare_digest(existing["descriptor_hash"], descriptor.descriptor_hash)
                ):
                    return dict(existing), True
                raise PublicationError(
                    "PUBLICATION_CONFLICT",
                    "publication_id or publication_version is already bound to a different descriptor",
                    409,
                )
            connection.execute(
                """
                INSERT INTO publications (
                    publication_id, publication_version, descriptor_hash, descriptor_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    descriptor.publication_id,
                    descriptor.publication_version,
                    descriptor.descriptor_hash,
                    descriptor_json,
                    QUEUED,
                    now,
                    now,
                ),
            )
        return self.get(descriptor.publication_id, descriptor.publication_version), False

    def get(self, publication_id: str, publication_version: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM publications WHERE publication_id = ? AND publication_version = ?",
                (publication_id, publication_version),
            ).fetchone()
        if row is None:
            raise PublicationError("PUBLICATION_NOT_FOUND", "Publication was not found", 404)
        return dict(row)

    def update(self, descriptor: PublicationDescriptor, status: str, **values: Any) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        allowed = {
            "collection_name",
            "previous_collection_name",
            "record_count",
            "point_count",
            "error_code",
            "message",
        }
        columns = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now]
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"Unsupported publication state field: {key}")
            columns.append(f"{key} = ?")
            params.append(value)
        params.extend((descriptor.publication_id, descriptor.publication_version))
        with self._connection() as connection:
            connection.execute(
                f"UPDATE publications SET {', '.join(columns)} WHERE publication_id = ? AND publication_version = ?",
                params,
            )
        return self.get(descriptor.publication_id, descriptor.publication_version)

    def add_event(self, descriptor: PublicationDescriptor, event_id: str, body: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO publication_events (event_id, publication_id, publication_version, body, callback_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    descriptor.publication_id,
                    descriptor.publication_version,
                    body,
                    descriptor.callback_url,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return self.get_event(event_id)

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM publication_events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("Publication callback event was not found")
        return dict(row)

    def mark_event_attempt(self, event_id: str, delivered: bool) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE publication_events SET attempts = attempts + 1, delivered_at = ? WHERE event_id = ?",
                (datetime.now(UTC).isoformat() if delivered else None, event_id),
            )

    def outstanding_events(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM publication_events WHERE delivered_at IS NULL").fetchall()
        return [dict(row) for row in rows]

    def incomplete_descriptors(self) -> list[PublicationDescriptor]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT descriptor_json FROM publications WHERE status IN (?, ?)", (QUEUED, BUILDING)
            ).fetchall()
        return [PublicationDescriptor(**json.loads(row["descriptor_json"])) for row in rows]

    def acquire_build_lock(self, descriptor: PublicationDescriptor) -> bool:
        """Acquire the cross-process lock that serializes all alias changes."""
        with self._connection() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO publication_build_lock (lock_name, publication_id, publication_version)
                    VALUES ('publisher', ?, ?)
                    """,
                    (descriptor.publication_id, descriptor.publication_version),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def release_build_lock(self, descriptor: PublicationDescriptor) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM publication_build_lock
                WHERE lock_name = 'publisher' AND publication_id = ? AND publication_version = ?
                """,
                (descriptor.publication_id, descriptor.publication_version),
            )

    def reset_build_locks(self) -> None:
        """Clear a lock held by a process that ended before restart recovery."""
        with self._connection() as connection:
            connection.execute("DELETE FROM publication_build_lock")


def _safe_message(error: Exception) -> tuple[str, str]:
    if isinstance(error, SnapshotValidationError):
        return "SNAPSHOT_VALIDATION_FAILED", str(error)[:500]
    if isinstance(error, PublicationError):
        return error.code[:64], str(error)[:500]
    logger.exception("publication_failed", error_type=type(error).__name__)
    return "PUBLICATION_BUILD_FAILED", "The publication build failed; inspect backend logs with the publication ID."


def _is_allowed_https_url(value: str, exact_urls: Iterable[str], prefix_urls: Iterable[str]) -> bool:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        return False
    decoded_path = unquote(parsed.path)
    if "%" in decoded_path or "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/")):
        return False
    if value in exact_urls:
        return True
    for prefix in prefix_urls:
        prefix_parsed = urlsplit(prefix)
        if (
            prefix_parsed.scheme == "https"
            and prefix_parsed.netloc
            and prefix_parsed.path.endswith("/")
            and not prefix_parsed.query
            and not prefix_parsed.fragment
            and parsed.hostname == prefix_parsed.hostname
            and parsed.port == prefix_parsed.port
            and parsed.path.startswith(prefix_parsed.path)
        ):
            return True
    return False


def validate_descriptor_targets(descriptor: PublicationDescriptor) -> None:
    """Reject unconfigured snapshot and callback targets before any work is queued."""
    if not _is_allowed_https_url(descriptor.snapshot_url, (), PUBLICATION_SNAPSHOT_URL_PREFIXES):
        raise PublicationError("SNAPSHOT_URL_NOT_ALLOWED", "Snapshot URL does not match configured storage rules", 422)
    if not _is_allowed_https_url(descriptor.callback_url, PUBLICATION_CALLBACK_URLS, PUBLICATION_CALLBACK_URL_PREFIXES):
        raise PublicationError("CALLBACK_URL_NOT_ALLOWED", "Callback URL does not match configured callback rules", 422)
    if not PUBLICATION_CALLBACK_SECRET:
        raise PublicationError("PUBLICATION_DISABLED", "Publication callback signing is not configured", 503)


def verify_request_signature(headers: Any, body: bytes, store: PublicationStore) -> None:
    """Verify exact raw-body HMAC and persist the one-time nonce before parsing JSON."""
    if not PUBLICATION_DISPATCH_SECRET:
        raise PublicationError("PUBLICATION_DISABLED", "Publication dispatch is not configured", 503)
    timestamp = headers.get("X-IRE-Publication-Timestamp")
    nonce = headers.get("X-IRE-Publication-Nonce")
    signature = headers.get("X-IRE-Publication-Signature")
    if not timestamp or not nonce or not signature:
        raise PublicationError("PUBLICATION_AUTH_REQUIRED", "Missing publication authentication headers", 401)
    try:
        timestamp_value = int(timestamp)
        uuid.UUID(nonce)
    except (TypeError, ValueError):
        raise PublicationError("PUBLICATION_AUTH_INVALID", "Invalid publication authentication headers", 401) from None
    if abs(int(time.time()) - timestamp_value) > PUBLICATION_SIGNATURE_MAX_AGE_SECONDS:
        raise PublicationError(
            "PUBLICATION_AUTH_EXPIRED", "Publication signature timestamp is outside the allowed window", 401
        )
    signed = f"{timestamp}.{nonce}.".encode() + body
    expected = "sha256=" + hmac.new(PUBLICATION_DISPATCH_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PublicationError("PUBLICATION_AUTH_INVALID", "Invalid publication signature", 401)
    if not store.consume_nonce(nonce, timestamp_value + PUBLICATION_SIGNATURE_MAX_AGE_SECONDS):
        raise PublicationError("PUBLICATION_REPLAY", "Publication request nonce has already been used", 409)


class PublicationService:
    """Coordinates a complete snapshot build without touching the serving collection."""

    def __init__(
        self,
        store: PublicationStore,
        qdrant_client: QdrantClient,
        dense_model: SentenceTransformer,
        sparse_model: SparseTextEmbedding,
    ) -> None:
        self.store = store
        self.qdrant_client = qdrant_client
        self.dense_model = dense_model
        self.sparse_model = sparse_model
        self.max_request_bytes = PUBLICATION_MAX_REQUEST_BYTES
        self.max_upload_bytes = PUBLICATION_MAX_UPLOAD_BYTES

    def _callback_body(self, state: dict[str, Any]) -> tuple[str, str]:
        descriptor = PublicationDescriptor(**json.loads(state["descriptor_json"]))
        event_id = str(uuid.uuid4())
        body = {
            "event_id": event_id,
            "publication_id": descriptor.publication_id,
            "publication_version": descriptor.publication_version,
            "schema_version": descriptor.schema_version,
            "checksum": descriptor.checksum,
            "status": state["status"],
            "occurred_at": state["updated_at"],
        }
        for key in ("collection_name", "record_count", "point_count", "error_code", "message"):
            if state.get(key) is not None:
                body[key] = state[key]
        return event_id, json.dumps(body, sort_keys=True, separators=(",", ":"))

    def report(self, descriptor: PublicationDescriptor, state: dict[str, Any]) -> None:
        event_id, body = self._callback_body(state)
        event = self.store.add_event(descriptor, event_id, body)
        self.deliver_event(event)

    def deliver_event(self, event: dict[str, Any]) -> None:
        if not PUBLICATION_CALLBACK_SECRET:
            logger.error("publication_callback_not_configured", event_id=event["event_id"])
            return
        body = event["body"].encode()
        response: httpx.Response | None = None
        for attempt in range(PUBLICATION_CALLBACK_RETRIES):
            timestamp = str(int(time.time()))
            nonce = str(uuid.uuid4())
            signature = hmac.new(
                PUBLICATION_CALLBACK_SECRET.encode(),
                f"{timestamp}.{nonce}.".encode() + body,
                hashlib.sha256,
            ).hexdigest()
            try:
                response = httpx.post(
                    event["callback_url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-IRE-Publication-Timestamp": timestamp,
                        "X-IRE-Publication-Nonce": nonce,
                        "X-IRE-Publication-Signature": f"sha256={signature}",
                    },
                    timeout=20,
                )
                delivered = 200 <= response.status_code < 300
            except httpx.HTTPError:
                delivered = False
            self.store.mark_event_attempt(event["event_id"], delivered)
            if delivered:
                return
            if response is not None and response.status_code < 500:
                logger.warning(
                    "publication_callback_rejected",
                    event_id=event["event_id"],
                    status_code=response.status_code,
                )
                return
            if attempt < PUBLICATION_CALLBACK_RETRIES - 1:
                time.sleep(2**attempt)
        logger.warning("publication_callback_delivery_failed", event_id=event["event_id"])

    def _legacy_id_map(self) -> dict[int, tuple[str, str]]:
        """Keep an old deep-link only for a unique ID/title match in the live collection."""
        aliases = self.qdrant_client.get_aliases().aliases
        live_collection = next(
            (alias.collection_name for alias in aliases if alias.alias_name == SERVING_COLLECTION_ALIAS), None
        )
        if live_collection is None:
            return {}
        candidates: dict[int, list[tuple[str, str]]] = {}
        offset: Any = None
        while True:
            points, offset = self.qdrant_client.scroll(
                collection_name=live_collection, offset=offset, limit=1_000, with_payload=True, with_vectors=False
            )
            for point in points:
                payload = point.payload or {}
                metadata = payload.get("metadata", {})
                legacy_id = metadata.get("id")
                title = payload.get("title")
                if isinstance(legacy_id, int) and isinstance(title, str):
                    candidates.setdefault(legacy_id, []).append((str(point.id), title))
            if offset is None:
                break
        return {key: values[0] for key, values in candidates.items() if len(values) == 1}

    def _collection_name(self, descriptor: PublicationDescriptor) -> str:
        return f"{COLLECTION_NAME}__publication__{descriptor.publication_id.replace('-', '')[:16]}__{descriptor.checksum[:12]}"

    def _upload_records(self, path: Path, collection_name: str, old_ids: dict[int, tuple[str, str]]) -> tuple[int, str]:
        point_count = 0
        batch: list[tuple[str, str, dict[str, Any]]] = []
        batch_bytes = 0
        validation_text = ""
        for record in iter_records(path):
            text = searchable_text(record)
            # A matching title makes the legacy Django ID a conservative cutover match.
            old_id = old_ids.get(record["id"])
            point_id = old_id[0] if old_id and old_id[1] == record["title"] else record["public_id"]
            item = (
                point_id,
                text,
                {"title": record["title"], "doc_type": "ire_resource", "metadata": resource_metadata(record)},
            )
            item_bytes = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode())
            if item_bytes > self.max_upload_bytes:
                raise PublicationError("POINT_TOO_LARGE", "A resource exceeds the configured Qdrant upload limit", 422)
            if batch and (len(batch) >= BATCH_SIZE or batch_bytes + item_bytes > self.max_upload_bytes):
                point_count += self._upload_batch(collection_name, batch)
                batch.clear()
                batch_bytes = 0
            batch.append(item)
            batch_bytes += item_bytes
            if not validation_text and text.strip():
                validation_text = text[:1_000]
            if len(batch) >= BATCH_SIZE:
                point_count += self._upload_batch(collection_name, batch)
                batch.clear()
        if batch:
            point_count += self._upload_batch(collection_name, batch)
        return point_count, validation_text

    def _upload_batch(self, collection_name: str, batch: list[tuple[str, str, dict[str, Any]]]) -> int:
        texts = [item[1] for item in batch]
        dense, sparse = generate_embeddings_batch(self.dense_model, self.sparse_model, texts)
        points = [
            PointStruct(
                id=item[0],
                vector={"dense": dense[index], "sparse": sparse[index].as_object()},
                payload={"text": item[1], **item[2]},
            )
            for index, item in enumerate(batch)
        ]
        self.qdrant_client.upsert(collection_name=collection_name, points=points, wait=True)
        return len(points)

    def _switch_alias(self, collection_name: str) -> str | None:
        aliases = self.qdrant_client.get_aliases().aliases
        previous = next(
            (alias.collection_name for alias in aliases if alias.alias_name == SERVING_COLLECTION_ALIAS), None
        )
        operations: list[Any] = []
        if previous is not None:
            operations.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=SERVING_COLLECTION_ALIAS)))
        operations.append(
            CreateAliasOperation(
                create_alias=CreateAlias(collection_name=collection_name, alias_name=SERVING_COLLECTION_ALIAS)
            )
        )
        self.qdrant_client.update_collection_aliases(change_aliases_operations=operations)
        return previous

    def _current_alias_target(self) -> str | None:
        aliases = self.qdrant_client.get_aliases().aliases
        return next((alias.collection_name for alias in aliases if alias.alias_name == SERVING_COLLECTION_ALIAS), None)

    def run(self, descriptor: PublicationDescriptor) -> None:
        while not self.store.acquire_build_lock(descriptor):
            # Keep later work queued until the current build completes. The lock
            # is durable, so separate API workers cannot race alias changes.
            time.sleep(1)
        state = self.store.update(descriptor, BUILDING)
        self.report(descriptor, state)
        path: Path | None = None
        try:
            collection_name = self._collection_name(descriptor)
            # Never recreate a collection that became live before a process stopped.
            if self._current_alias_target() == collection_name:
                info = self.qdrant_client.get_collection(collection_name)
                clear_publication_caches()
                state = self.store.update(
                    descriptor,
                    SUCCEEDED,
                    collection_name=collection_name,
                    record_count=info.points_count,
                    point_count=info.points_count,
                    error_code=None,
                    message="Publication is serving through the Qdrant alias",
                )
                self.report(descriptor, state)
                return
            path = self._download(descriptor)
            metadata = validate_snapshot(path)
            if metadata["publication_id"] != descriptor.publication_id:
                raise PublicationError("SNAPSHOT_ID_MISMATCH", "Snapshot publication_id does not match descriptor", 422)
            if metadata["checksum"] != descriptor.checksum:
                raise PublicationError("SNAPSHOT_CHECKSUM_MISMATCH", "Snapshot checksum does not match descriptor", 422)
            # Save both targets before the atomic alias switch so recovery can
            # recognize a stopped worker without putting live data at risk.
            previous = self._current_alias_target()
            self.store.update(
                descriptor,
                BUILDING,
                collection_name=collection_name,
                previous_collection_name=previous,
            )
            create_hybrid_collection(self.qdrant_client, collection_name, VECTOR_SIZE, recreate=True)
            point_count, validation_text = self._upload_records(path, collection_name, self._legacy_id_map())
            if point_count != metadata["record_count"]:
                raise PublicationError(
                    "POINT_COUNT_MISMATCH", "Built point count does not match the snapshot record count", 422
                )
            info = self.qdrant_client.get_collection(collection_name)
            if info.points_count != metadata["record_count"]:
                raise PublicationError(
                    "QDRANT_COUNT_MISMATCH", "Qdrant point count does not match the snapshot record count", 422
                )
            if metadata["record_count"] and not validation_text:
                raise PublicationError("EMPTY_SEARCH_CONTENT", "Published records have no searchable content", 422)
            if validation_text:
                query = self.dense_model.encode(validation_text).tolist()
                if not self.qdrant_client.query_points(
                    collection_name=collection_name, query=query, using="dense", limit=1
                ).points:
                    raise PublicationError(
                        "SEARCH_VALIDATION_FAILED", "New collection failed representative search validation", 422
                    )
            self._switch_alias(collection_name)
            clear_publication_caches()
            state = self.store.update(
                descriptor,
                SUCCEEDED,
                collection_name=collection_name,
                previous_collection_name=previous,
                record_count=metadata["record_count"],
                point_count=point_count,
                error_code=None,
                message="Publication is serving through the Qdrant alias",
            )
            self.report(descriptor, state)
        except Exception as exc:  # Errors are recorded and reported without changing the serving alias.
            code, message = _safe_message(exc)
            state = self.store.update(descriptor, FAILED, error_code=code, message=message)
            self.report(descriptor, state)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
            self.store.release_build_lock(descriptor)

    def _download(self, descriptor: PublicationDescriptor) -> Path:
        Path(PUBLICATION_WORK_DIR).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=PUBLICATION_WORK_DIR, suffix=".json", delete=False) as target:
            path = Path(target.name)
            total = 0
            try:
                with httpx.stream(
                    "GET", descriptor.snapshot_url, timeout=httpx.Timeout(60, read=300), follow_redirects=False
                ) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > PUBLICATION_MAX_SNAPSHOT_BYTES:
                            raise PublicationError(
                                "SNAPSHOT_TOO_LARGE", "Snapshot exceeds the configured maximum size", 422
                            )
                        target.write(chunk)
            except Exception:
                path.unlink(missing_ok=True)
                raise
        return path

    def rollback(self, descriptor: PublicationDescriptor) -> dict[str, Any]:
        state = self.store.get(descriptor.publication_id, descriptor.publication_version)
        if state["status"] != SUCCEEDED or not state["previous_collection_name"]:
            raise PublicationError("ROLLBACK_UNAVAILABLE", "This publication has no retained prior collection", 409)
        if self._current_alias_target() != state["collection_name"]:
            raise PublicationError(
                "ROLLBACK_NOT_CURRENT",
                "Only the publication currently serving through the alias can be rolled back",
                409,
            )
        self._switch_alias(state["previous_collection_name"])
        clear_publication_caches()
        state = self.store.update(
            descriptor, ROLLED_BACK, message="Serving alias restored to the retained prior collection"
        )
        self.report(descriptor, state)
        return state

    def recover(self) -> None:
        """Resume interrupted builds and retry callbacks after a process restart."""
        self.store.reset_build_locks()
        for event in self.store.outstanding_events():
            self.deliver_event(event)
        for descriptor in self.store.incomplete_descriptors():
            self.run(descriptor)
