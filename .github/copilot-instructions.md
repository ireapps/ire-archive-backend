# Copilot instructions

This repository is the FastAPI search and MemberSuite authentication backend for
`ireapps/ire-archive-frontend`. Preserve its existing HTTP, authentication, stable-link, pagination, metadata,
search-mode, CORS, and cookie contracts unless a change is coordinated across repositories.

## Data ownership

- `ireapps/ire-archive-data` Django/Postgres is the permanent editorial source of truth.
- This backend consumes validated, immutable, versioned snapshots of approved records.
- Qdrant is a disposable serving index, never a source of truth or a substitute for Django/Postgres backups.
- Draft, withdrawn, omitted, and `needs_review` records must not enter the public index.
- Code deployment and data publication are separate operations.

## Publication boundary

Future publication work must follow
[backend issue #11](https://github.com/ireapps/ire-archive-backend/issues/11):

- Use stable public/vector IDs derived from permanent exported IDs, while preserving existing live IDs for
  confidently matched records during migration.
- Accept authenticated, idempotent publication descriptors; never fetch arbitrary unvalidated URLs.
- Build and validate a new collection, atomically switch a Qdrant alias, retain the prior collection for rollback,
  explicitly remove withdrawn/omitted records, invalidate caches, and report status to Django.
- Never mutate the live collection in place for publication.

The current `--no-clear-db` option only skips recreation and is not an incremental sync mechanism. Do not extend
the legacy direct indexer as if it were the publication system.

Read [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) and [docs/API_CONTRACT.md](../docs/API_CONTRACT.md) before
changing indexing, IDs, API responses, authentication, or deployment behavior. Producer work is tracked in
`ireapps/ire-archive-data` issues
[#48](https://github.com/ireapps/ire-archive-data/issues/48),
[#70](https://github.com/ireapps/ire-archive-data/issues/70), and
[#105](https://github.com/ireapps/ire-archive-data/issues/105).
