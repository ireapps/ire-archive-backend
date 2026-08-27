"""Streaming validation and transformation for approved v2 resource snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import ijson
from app.config import PUBLICATION_MAX_EXTRACTED_TEXT_CHARS, PUBLICATION_MAX_RECORD_BYTES

SCHEMA_VERSION = "2.0"
ALLOWED_CATEGORIES = frozenset({"audio", "contest entry", "dataset", "journal", "tipsheet", "webinar"})
EXTRACTED_TEXT_SOURCES = frozenset({"audio_transcript", "document_extraction"})
SNAPSHOT_KEYS = frozenset({"schema_version", "publication_id", "created_at", "record_count", "checksum", "records"})
RECORD_KEYS = frozenset(
    {
        "id",
        "public_id",
        "resource_id",
        "post_id",
        "title",
        "description",
        "category",
        "event_legacy_source_id",
        "event_name",
        "event_type",
        "year_computed",
        "published_at",
        "source_created_at",
        "published_at_verified",
        "missing_file",
        "authors",
        "affiliations",
        "authors_extracted",
        "affiliations_extracted",
        "contest_entries",
        "downloads",
    }
)
CONTEST_ENTRY_KEYS = frozenset({"contest_year", "category", "size_group", "result_status", "is_finalist", "is_winner"})
DOWNLOAD_KEYS = frozenset(
    {"id", "file", "url", "name", "filesize", "sort_order", "extracted_text", "extracted_text_source"}
)
MAX_RECORD_BYTES = PUBLICATION_MAX_RECORD_BYTES
MAX_EXTRACTED_TEXT_CHARS = PUBLICATION_MAX_EXTRACTED_TEXT_CHARS


class SnapshotValidationError(ValueError):
    """Raised when a snapshot does not meet the only supported export contract."""


def enforce_stream_limits(
    path: Path,
    *,
    max_record_bytes: int = MAX_RECORD_BYTES,
) -> None:
    """Reject oversized records before ijson materializes their string values."""
    in_string = False
    escaped = False
    token = bytearray()
    last_key: str | None = None
    current_key: str | None = None
    records_array = False
    record_depth = 0
    record_bytes = 0

    with path.open("rb") as source:
        while chunk := source.read(64 * 1024):
            for byte in chunk:
                if record_depth:
                    record_bytes += 1
                    if record_bytes > max_record_bytes:
                        raise SnapshotValidationError("Snapshot record exceeds the configured byte limit")
                if in_string:
                    if escaped:
                        escaped = False
                    elif byte == 92:
                        escaped = True
                    elif byte == 34:
                        in_string = False
                        try:
                            last_key = token.decode("utf-8")
                        except UnicodeDecodeError:
                            last_key = None
                    elif len(token) < 128:
                        token.append(byte)
                    continue
                if byte == 34:
                    in_string = True
                    escaped = False
                    token.clear()
                    continue
                if byte in b" \t\r\n":
                    continue
                if byte == 58:
                    current_key = last_key
                    last_key = None
                elif current_key == "records" and byte == 91:
                    records_array = True
                    current_key = None
                elif records_array and byte == 123:
                    record_depth += 1
                    if record_depth == 1:
                        record_bytes = 1
                elif record_depth and byte == 123:
                    record_depth += 1
                elif record_depth and byte == 125:
                    record_depth -= 1
                    if record_depth == 0:
                        current_key = None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _exact_keys(value: dict[str, Any], expected: frozenset[str], path: str, errors: list[str]) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing:
        errors.append(f"{path}: missing keys {missing}")
    if extra:
        errors.append(f"{path}: unexpected keys {extra}")


def _validate_named_values(values: Any, path: str, errors: list[str]) -> None:
    if not isinstance(values, list):
        errors.append(f"{path}: expected a list")
        return
    names: list[str] = []
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        if not isinstance(value, dict):
            errors.append(f"{item_path}: expected an object")
            continue
        _exact_keys(value, frozenset({"name"}), item_path, errors)
        name = value.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"{item_path}.name: expected a non-empty string")
        else:
            names.append(name)
    if len(names) == len(values) and names != sorted(names):
        errors.append(f"{path}: values must be ordered by name")


def _validate_contests(values: Any, path: str, errors: list[str]) -> None:
    if not isinstance(values, list):
        errors.append(f"{path}: expected a list")
        return
    previous: tuple[bool, int, str, str, str] | None = None
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        if not isinstance(value, dict):
            errors.append(f"{item_path}: expected an object")
            continue
        _exact_keys(value, CONTEST_ENTRY_KEYS, item_path, errors)
        year = value.get("contest_year")
        if year is not None and not _is_int(year):
            errors.append(f"{item_path}.contest_year: expected an integer or null")
        for field in ("category", "size_group", "result_status"):
            if not isinstance(value.get(field), str):
                errors.append(f"{item_path}.{field}: expected a string")
        for field in ("is_finalist", "is_winner"):
            if type(value.get(field)) is not bool:
                errors.append(f"{item_path}.{field}: expected a boolean")
        if year is None or _is_int(year):
            current = (
                year is None,
                year or 0,
                str(value.get("category", "")),
                str(value.get("size_group", "")),
                str(value.get("result_status", "")),
            )
            if previous is not None and current < previous:
                errors.append(f"{path}: values must use the documented deterministic order")
            previous = current


def _validate_downloads(values: Any, path: str, errors: list[str], seen_download_ids: set[str]) -> None:
    if not isinstance(values, list):
        errors.append(f"{path}: expected a list")
        return
    previous: tuple[int, str, str, str] | None = None
    for index, value in enumerate(values):
        item_path = f"{path}[{index}]"
        if not isinstance(value, dict):
            errors.append(f"{item_path}: expected an object")
            continue
        _exact_keys(value, DOWNLOAD_KEYS, item_path, errors)
        download_id = value.get("id")
        if not _is_uuid(download_id):
            errors.append(f"{item_path}.id: expected a UUID string")
        elif isinstance(download_id, str):
            if download_id in seen_download_ids:
                errors.append("Snapshot.records: download id values must be unique")
            else:
                seen_download_ids.add(download_id)
        for field in ("file", "name", "extracted_text"):
            if not isinstance(value.get(field), str):
                errors.append(f"{item_path}.{field}: expected a string")
        url = value.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append(f"{item_path}.url: expected an absolute HTTP(S) URL")
        filesize = value.get("filesize")
        if filesize is not None:
            if not isinstance(filesize, int) or isinstance(filesize, bool):
                errors.append(f"{item_path}.filesize: expected a non-negative integer or null")
            elif cast(int, filesize) < 0:
                errors.append(f"{item_path}.filesize: expected a non-negative integer or null")
        sort_order = value.get("sort_order")
        if not isinstance(sort_order, int) or isinstance(sort_order, bool) or cast(int, sort_order) < 0:
            errors.append(f"{item_path}.sort_order: expected a non-negative integer")
        text, source = value.get("extracted_text"), value.get("extracted_text_source")
        if isinstance(text, str) and len(text) > MAX_EXTRACTED_TEXT_CHARS:
            errors.append(f"{item_path}.extracted_text: exceeds the configured character limit")
        if isinstance(text, str) and text:
            if source not in EXTRACTED_TEXT_SOURCES:
                errors.append(f"{item_path}.extracted_text_source: unsupported or missing source")
        elif source is not None:
            errors.append(f"{item_path}.extracted_text_source: expected null when extracted_text is blank")
        order = sort_order if isinstance(sort_order, int) and not isinstance(sort_order, bool) else -1
        current = (
            order,
            str(download_id or ""),
            str(value.get("file", "")),
            str(url or ""),
        )
        if previous is not None and current < previous:
            errors.append(f"{path}: values must use the documented deterministic order")
        previous = current


def validate_record(record: Any, index: int, seen_download_ids: set[str]) -> list[str]:
    """Validate one record while retaining only identities needed across records."""
    path = f"records[{index}]"
    errors: list[str] = []
    if not isinstance(record, dict):
        return [f"{path}: expected an object"]
    _exact_keys(record, RECORD_KEYS, path, errors)
    for field in ("resource_id", "title", "description", "category", "authors", "affiliations"):
        if not isinstance(record.get(field), str):
            errors.append(f"{path}.{field}: expected a string")
    legacy_id = record.get("id")
    if not isinstance(legacy_id, int) or isinstance(legacy_id, bool) or legacy_id <= 0:
        errors.append(f"{path}.id: expected a positive integer")
    if not _is_uuid(record.get("public_id")):
        errors.append(f"{path}.public_id: expected a UUID string")
    if record.get("post_id") is not None and not _is_int(record.get("post_id")):
        errors.append(f"{path}.post_id: expected an integer or null")
    if record.get("category") not in ALLOWED_CATEGORIES:
        errors.append(f"{path}.category: unsupported category {record.get('category')!r}")
    for field in ("event_legacy_source_id", "event_name", "event_type"):
        if record.get(field) is not None and not isinstance(record.get(field), str):
            errors.append(f"{path}.{field}: expected a string or null")
    if record.get("year_computed") is not None and not _is_int(record.get("year_computed")):
        errors.append(f"{path}.year_computed: expected an integer or null")
    for field in ("published_at", "source_created_at"):
        if record.get(field) is not None and not _is_timestamp(record.get(field)):
            errors.append(f"{path}.{field}: expected an ISO 8601 timestamp or null")
    for field in ("published_at_verified", "missing_file"):
        if type(record.get(field)) is not bool:
            errors.append(f"{path}.{field}: expected a boolean")
    _validate_named_values(record.get("authors_extracted"), f"{path}.authors_extracted", errors)
    _validate_named_values(record.get("affiliations_extracted"), f"{path}.affiliations_extracted", errors)
    _validate_contests(record.get("contest_entries"), f"{path}.contest_entries", errors)
    _validate_downloads(record.get("downloads"), f"{path}.downloads", errors, seen_download_ids)
    return errors


def _read_envelope(path: Path) -> tuple[dict[str, Any], list[str]]:
    root_keys: list[str] = []
    metadata: dict[str, Any] = {}
    root_is_object = False
    records_is_array = False
    try:
        with path.open("rb") as source:
            for prefix, event, value in ijson.parse(source):
                if prefix == "" and event == "start_map":
                    root_is_object = True
                elif prefix == "" and event == "map_key":
                    root_keys.append(value)
                elif prefix == "records" and event == "start_array":
                    records_is_array = True
                elif prefix in SNAPSHOT_KEYS - {"records"} and event in {"string", "number", "boolean", "null"}:
                    metadata[prefix] = value
    except (OSError, ijson.JSONError) as exc:
        raise SnapshotValidationError("Snapshot cannot be read as JSON.") from exc

    errors: list[str] = []
    if not root_is_object:
        errors.append("Snapshot: expected an object")
    _exact_keys(metadata, SNAPSHOT_KEYS - {"records"}, "Snapshot", errors)
    if set(root_keys) != SNAPSHOT_KEYS:
        missing = sorted(SNAPSHOT_KEYS - set(root_keys))
        extra = sorted(set(root_keys) - SNAPSHOT_KEYS)
        if missing:
            errors.append(f"Snapshot: missing keys {missing}")
        if extra:
            errors.append(f"Snapshot: unexpected keys {extra}")
    if len(root_keys) != len(set(root_keys)):
        errors.append("Snapshot: duplicate keys are not allowed")
    if metadata.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Snapshot.schema_version: expected supported version {SCHEMA_VERSION!r}")
    if not _is_uuid(metadata.get("publication_id")):
        errors.append("Snapshot.publication_id: expected a UUID string")
    if not _is_timestamp(metadata.get("created_at")):
        errors.append("Snapshot.created_at: expected an ISO 8601 UTC timestamp")
    if not _is_int(metadata.get("record_count")) or metadata.get("record_count", -1) < 0:
        errors.append("Snapshot.record_count: expected a non-negative integer")
    checksum = metadata.get("checksum")
    if not isinstance(checksum, str) or re.fullmatch(r"[0-9a-f]{64}", checksum) is None:
        errors.append("Snapshot.checksum: expected exactly 64 lowercase hexadecimal characters")
    if not records_is_array:
        errors.append("Snapshot.records: expected a list")
    return metadata, errors


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Yield records from a snapshot without materializing the records array."""
    with path.open("rb") as source:
        try:
            yield from ijson.items(source, "records.item")
        except ijson.JSONError as exc:
            raise SnapshotValidationError("Snapshot cannot be read as JSON.") from exc


def validate_snapshot(path: Path) -> dict[str, Any]:
    """Stream every record and verify envelope, checksum, count, and ordering."""
    enforce_stream_limits(path)
    metadata, errors = _read_envelope(path)
    if errors:
        raise SnapshotValidationError("; ".join(errors))
    digest = hashlib.sha256()
    digest.update(b"[")
    previous_id: str | None = None
    public_ids: set[str] = set()
    download_ids: set[str] = set()
    count = 0
    for record in iter_records(path):
        canonical_record = {key: value for key, value in record.items() if key != "id"}
        canonical_bytes = len(
            json.dumps(canonical_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        if canonical_bytes > MAX_RECORD_BYTES:
            raise SnapshotValidationError("Snapshot record exceeds the configured canonical byte limit")
        record_errors = validate_record(record, count, download_ids)
        if record_errors:
            raise SnapshotValidationError("; ".join(record_errors))
        public_id = record["public_id"]
        if public_id in public_ids:
            raise SnapshotValidationError("Snapshot.records: public_id values must be unique")
        if previous_id is not None and public_id < previous_id:
            raise SnapshotValidationError("Snapshot.records: records must be ordered by public_id")
        public_ids.add(public_id)
        previous_id = public_id
        stable_record = canonical_record
        if count:
            digest.update(b",")
        digest.update(
            json.dumps(
                stable_record, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
            ).encode()
        )
        count += 1
    digest.update(b"]")
    if count != metadata["record_count"]:
        raise SnapshotValidationError(f"Snapshot.record_count: expected {metadata['record_count']}, found {count}")
    if digest.hexdigest() != metadata["checksum"]:
        raise SnapshotValidationError("Snapshot.checksum: does not match the canonical records array")
    return metadata


def searchable_text(record: dict[str, Any], max_download_characters: int = 50_000) -> str:
    """Build one bounded searchable resource text while preserving download metadata."""
    parts = [f"Title: {record['title']}"]
    for label, field in (("Authors", "authors"), ("Affiliations", "affiliations"), ("Category", "category")):
        if record[field]:
            parts.append(f"{label}: {record[field]}")
    if record["description"]:
        parts.append(f"Content: {record['description']}")
    remaining = max_download_characters
    for download in record["downloads"]:
        extracted = download["extracted_text"]
        if not extracted or remaining <= 0:
            continue
        excerpt = extracted[:remaining]
        parts.append(f"Download ({download['name']}) {download['extracted_text_source']}:\n{excerpt}")
        remaining -= len(excerpt)
    return "\n\n".join(parts)


def resource_metadata(record: dict[str, Any]) -> dict[str, Any]:
    """Map v2 fields to the established frontend metadata shape without dropping downloads."""
    return {
        "id": record["id"],
        "public_id": record["public_id"],
        "resource_id": record["resource_id"],
        "post_id": record["post_id"],
        "authors": record["authors"],
        "authors_extracted": record["authors_extracted"],
        "authors_extracted_list": [item["name"] for item in record["authors_extracted"]],
        "affiliations": record["affiliations"],
        "affiliations_extracted_list": [item["name"] for item in record["affiliations_extracted"]],
        "description": record["description"],
        "category": record["category"],
        "year_computed": record["year_computed"],
        "published": record["published_at"],
        "date_created": record["source_created_at"],
        "event_legacy_source_id": record["event_legacy_source_id"],
        "event_name": record["event_name"],
        "event_type": record["event_type"],
        "published_at_verified": record["published_at_verified"],
        "missing_file": record["missing_file"],
        "contest_entries": record["contest_entries"],
        # Extraction content is indexed into the resource text. Keeping it out
        # of the response payload preserves the existing download API shape and
        # keeps individual Qdrant points safely bounded.
        "downloads": [
            {
                "id": download["id"],
                "file": download["file"],
                "url": download["url"],
                "name": download["name"],
                "filesize": download["filesize"],
                "sort_order": download["sort_order"],
            }
            for download in record["downloads"]
        ],
    }
