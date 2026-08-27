# Cross-Repository Architecture

## System boundaries

| Repository/service | Responsibility |
|---|---|
| [`ireapps/ire-archive-data`](https://github.com/ireapps/ire-archive-data) | Django/Postgres editorial source of truth, review state, approved export, and publication history |
| [`ireapps/ire-archive-backend`](https://github.com/ireapps/ire-archive-backend) | Production FastAPI search and MemberSuite authentication contract |
| [`ireapps/ire-archive-frontend`](https://github.com/ireapps/ire-archive-frontend) | User-facing SvelteKit application consuming the backend contract |
| Qdrant | Disposable search-serving index built from an approved snapshot |
| Redis | Server-side MemberSuite session storage |

Django/Postgres is the permanent record. Qdrant can be rebuilt and must not be treated as an editorial store,
backup, or recovery substitute for Django/Postgres.

## Publication model

Editorial work and approval happen in Django. Staff deliberately publish a complete, validated snapshot of
eligible records. The snapshot is immutable and versioned; it is not a draft feed or an incremental stream.

The producer sends an authenticated, idempotent descriptor containing the publication version, schema version,
snapshot location, checksum, and callback details. The backend verifies the descriptor and artifact rather than
fetching an arbitrary URL. The precise private API, signing, and retry rules are in
[PUBLICATION_API.md](PUBLICATION_API.md).

For each publication, the backend must:

1. Reject incompatible, duplicate, or conflicting descriptors safely.
2. Download and validate the exact immutable snapshot.
3. Build a new Qdrant collection without changing the live collection.
4. Validate record counts, schema expectations, and representative searches.
5. Switch the serving alias atomically only after validation succeeds.
6. Clear search, resource, similar-resource, and reranking caches.
7. Retain the previous collection long enough for rollback.
8. Report queued, building, succeeded, or failed status to Django.

A failed build must leave live search untouched. Because each snapshot is complete, a successful publication
must remove withdrawn and omitted records from the serving index.

## Stable identifiers and compatibility

Public `vector_id` values are part of stable resource URLs and are opaque to clients. Future publications must
derive them from permanent exported IDs. During migration, records confidently matched to the live corpus must
retain their current IDs so existing deep links continue to work.

Publication must preserve the existing frontend contract documented in [API_CONTRACT.md](API_CONTRACT.md),
including:

- endpoint request and response shapes;
- MemberSuite session behavior;
- pagination fields and limits;
- metadata field types;
- `hybrid` and `keyword` search modes;
- CORS origins and credential handling; and
- cookie scope, security, and same-site behavior.

Any intentional contract change requires coordination with the frontend and data repositories.

## Operational boundaries

Code deployment and data publication are separate events. Deploying this application must not implicitly publish
editorial changes, and publishing a snapshot must not require a code deploy.

The current direct production indexing commands predate this design. In particular, `--no-clear-db` only avoids
recreating the collection; it does not reconcile deletions and must never be described as incremental sync.
These commands do not provide the atomic build, alias switch, rollback, or callback guarantees above.

## Serving alias and migration

The API reads `SERVING_COLLECTION_ALIAS`, not a physical collection. On first startup it points that alias at the
existing `COLLECTION_NAME`, so existing callers see no change. A publication writes a distinct, named collection and
changes the alias only after the snapshot, point count, and representative query validate.

For the one-time cutover, a v2 record retains an existing vector ID only when exactly one point in the current alias
has the same legacy Django `metadata.id` **and** title. This deliberately conservative match keeps known resource URLs
working. All other records use their permanent v2 `public_id` as their Qdrant point ID and API `vector_id`. The
transitional Django ID never determines a new identity.

## Tracking

- Backend consumer and atomic indexing: [ire-archive-backend #11](https://github.com/ireapps/ire-archive-backend/issues/11)
- Approved snapshot contract: [ire-archive-data #48](https://github.com/ireapps/ire-archive-data/issues/48)
- Staff publication workflow: [ire-archive-data #70](https://github.com/ireapps/ire-archive-data/issues/70)
- Editorial publication states: [ire-archive-data #105](https://github.com/ireapps/ire-archive-data/issues/105)
