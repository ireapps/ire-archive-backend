# Approved snapshot publication API

This private API connects `ire-archive-data` to this backend without changing the frontend API. It remains disabled
until distinct secrets and narrow HTTPS URL rules are configured.

## Dispatch and polling

`POST /internal/publications` requires `Content-Type: application/json` and a body no larger than 16 KiB:

```json
{
  "publication_id": "UUID",
  "publication_version": "non-empty string, maximum 128 characters",
  "schema_version": "2.0",
  "snapshot_url": "https://trusted-storage.example/approved-snapshots/file.json",
  "checksum": "64 lowercase SHA-256 hex characters",
  "callback_url": "https://data.example/internal/search-publications/status"
}
```

Snapshot URLs must match a configured HTTPS storage path prefix. Callback URLs must match a configured exact HTTPS
endpoint or HTTPS path prefix. The response is `202` with `publication_id`, `publication_version`, `status`,
`status_url`, and `idempotent`. The same complete descriptor, sent with a fresh nonce, returns current state with
`idempotent: true`. Reusing either ID or version with any changed field returns `409 PUBLICATION_CONFLICT`.

Poll authenticated `GET /internal/publications/{publication_id}/{publication_version}`.

## Request authentication

Dispatch, polling, and rollback use `PUBLICATION_DISPATCH_SECRET`. Sign the exact raw UTF-8 body; never parse and
serialize it before signing.

| Header | Value |
| --- | --- |
| `X-IRE-Publication-Timestamp` | Unix epoch seconds |
| `X-IRE-Publication-Nonce` | New UUID for every request |
| `X-IRE-Publication-Signature` | `sha256=` plus HMAC-SHA256 of `timestamp + "." + nonce + "." + raw_body` |

The default timestamp window is five minutes. The backend durably stores used nonces until the window expires, so
reused nonces return `409 PUBLICATION_REPLAY`.

## Status callbacks

The backend sends `queued`, `building`, `succeeded`, `failed`, and `rolled_back` callbacks using the distinct
`PUBLICATION_CALLBACK_SECRET`. Each body contains stable `event_id`, publication ID/version, `schema_version`,
`checksum`, status, timestamp, and applicable collection/count or safe bounded error details. The callback uses the
same headers and raw-body signing rule.

Delivery is at-least-once. An event is stored before sending, then retried three times after transient failure. Retry
bodies keep the same `event_id`, but use a new nonce, timestamp, and signature. The data service must durably
deduplicate event IDs and tolerate delayed duplicate/out-of-order events. Legal progression is `queued -> building
-> succeeded|failed`; `succeeded` may later become `rolled_back`.

## Build and rollback

The backend streams the snapshot to temporary storage, validates the strict v2 envelope, checksums, IDs, ordering,
and counts with a streaming parser, then builds a fresh Qdrant collection. It retains one record and bounded embedding
batches at a time. Before `ijson` reads any record, the raw stream rejects a record over 2,000,000 bytes; the
validated canonical record is also limited to 2,000,000 bytes. Each `extracted_text` is limited to 1,000,000
characters, and at most 50,000 extracted-text characters contribute to one resource's searchable content. These
limits bound parser materialization and must be enforced by the producer too. The public download metadata keeps its
ID, order, URL, name, file, and size; extraction-only text is not returned to browsers.

After exact point-count and representative-query validation, one Qdrant alias update moves
`SERVING_COLLECTION_ALIAS` to the new collection. Failure never moves the alias. The prior target remains for
`POST /internal/publications/{publication_id}/{publication_version}/rollback`, which authenticates an empty signed
body and moves only the alias - no download or re-embedding. Every new point ID is the permanent resource
`public_id`; a durable, active legacy-vector-ID lookup resolves only confidently matched old resource links. Failed
build collections are removed, and successful cutovers retain only the live collection and one rollback target.
