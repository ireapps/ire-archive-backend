# AGENTS.md

Guide for AI agents working on the IRE Archive Backend project.

## Project Overview

This is the backend API for the IRE (Investigative Reporters & Editors) archive search. It enables journalists to search thousands of tipsheets, transcripts, and training materials using AI-powered semantic search.

**Key Technologies:**

- Backend: FastAPI + Qdrant vector database + sentence-transformers
- Session Store: Redis for MemberSuite SSO session management
- Authentication: MemberSuite SSO integration with server-side sessions
- Frontend: Separate repo — https://github.com/ireapps/ire-archive-frontend (SvelteKit 5 / Svelte 5 runes + TypeScript + SCSS)
- Deployment: Backend on Fly.io, Frontend on Vercel (from frontend repo)
- Tooling: uv package manager, pytest

**Production URLs:**

- Frontend: https://archive.ire.org
- Backend API: https://api.archive.ire.org

## Cross-Repository Architecture

- `ireapps/ire-archive-data` is the permanent editorial system of record in Django/Postgres.
- This repository serves the production search and MemberSuite authentication API consumed by
  `ireapps/ire-archive-frontend`.
- Qdrant is a disposable serving index. It is not an editorial database, a backup, or a source of truth.
- Data reaches this service only as a validated, immutable, versioned snapshot of approved records through
  deliberate batch publication. Draft, withdrawn, omitted, or `needs_review` records must not be published.
- Code deployment and data publication are separate operations.

The target publication implementation is tracked in
[backend issue #11](https://github.com/ireapps/ire-archive-backend/issues/11). Producer-side work is tracked in
[data #48](https://github.com/ireapps/ire-archive-data/issues/48),
[data #70](https://github.com/ireapps/ire-archive-data/issues/70), and
[data #105](https://github.com/ireapps/ire-archive-data/issues/105). See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the required invariants.

### Rules for future publication work

- Preserve all frontend API and authentication shapes, stable resource URLs, pagination, metadata types, search
  modes, and CORS/cookie behavior unless a coordinated cross-repository change is approved.
- Derive stable public/vector IDs from permanent exported IDs. Preserve existing live vector IDs for confidently
  matched records during migration.
- Accept only authenticated, idempotent publication descriptors. Do not fetch arbitrary unvalidated URLs.
- Build and validate a new collection, switch a Qdrant alias atomically, retain the prior collection for rollback,
  invalidate caches, and report status to Django.
- Never mutate the live collection in place for publication. A successful full snapshot must remove records that
  are withdrawn or omitted.
- `--no-clear-db` only skips collection recreation; it is not incremental synchronization.
- Qdrant recovery does not replace Django/Postgres backup and restore procedures.

## Repository Structure

```
.
├── app/                          # FastAPI backend application
│   ├── main.py                  # Main FastAPI app with routes
│   ├── config.py                # Configuration from environment variables
│   ├── models.py                # Pydantic models for request/response
│   ├── dependencies.py          # Dependency injection (DB, models, lifespan)
│   ├── validators.py            # Input validation functions
│   ├── rate_limit.py            # Rate limiting with slowapi
│   ├── exceptions.py            # Custom exception classes
│   ├── diagnostics.py           # Health check and diagnostics
│   ├── auth/                    # Authentication (MemberSuite SSO)
│   │   ├── config.py            # Auth settings (tenant, association IDs)
│   │   ├── dependencies.py      # require_member dependency injection
│   │   ├── exceptions.py        # Auth-specific error classes
│   │   ├── membersuite_client.py # MemberSuite SSO API client
│   │   ├── redirect_validator.py # OAuth redirect URL validation
│   │   ├── routes.py            # Login/logout/callback endpoints
│   │   └── session.py           # Redis-backed session management
│   └── services/                # Business logic services
│       ├── search_service.py    # Semantic/keyword/hybrid search
│       ├── filter_service.py    # Qdrant filter building
│       ├── recommendation_service.py  # Similar resources
│       ├── reranking_service.py # Cross-encoder reranking
│       └── cache_service.py     # In-memory LRU caching
│
├── scripts/                      # Task scripts (argparse-based)
│   ├── dev_tasks.py             # Local dev commands (start, stop, rebuild)
│   ├── prod_tasks.py            # Production commands (push, index, status)
│   ├── setup_tasks.py           # Initial setup commands
│   └── index.py                 # Document indexing (local + prod)
│
├── Makefile                    # make dev-*, make prod-*, make setup-*
│
├── data/                         # Data files
│   ├── fixtures.json            # Tracked E2E fixtures
│   └── qdrant_storage/          # Local Qdrant database (gitignored)
│
├── tests/                        # Backend pytest tests
│   ├── test_api/                # API endpoint tests
│   ├── test_services/           # Service layer tests
│   └── conftest.py              # Pytest fixtures
│
├── pyproject.toml               # Python dependencies (uv managed)
├── docker/                      # Container and compose assets
│   ├── Dockerfile               # Backend container
│   ├── Dockerfile.base          # ML dependencies base image
│   ├── docker-compose.yml       # Local Qdrant + Redis
│   ├── entrypoint.sh            # Container entrypoint
│   ├── supervisord.conf         # Supervisor config
│   └── config/qdrant.yaml       # Qdrant config used in container
├── fly.toml                     # Fly.io configuration
└── README.md                    # Documentation
```

## Development Workflow

### Prerequisites

All commands use the uv package manager:

```bash
uv sync --all-extras
make dev-status  # or: uv run python -m scripts.dev_tasks status
```

### Backend Development

```bash
make dev-start
make dev-index
make dev-status
make dev-logs
make dev-test-backend
make dev-stop
make dev-rebuild
```

Backend runs on http://localhost:8000
Qdrant dashboard: http://localhost:6333/dashboard

### Frontend Development

Frontend repo: https://github.com/ireapps/ire-archive-frontend
API contract: docs/API_CONTRACT.md
Allow custom frontend by setting ADDITIONAL_ALLOWED_ORIGINS (see app/config.py).

### Testing

```bash
uv run pytest tests/ -v
uv run pytest tests/ --cov=app
	# Fixtures are tracked; pull from the repo if missing.
```

Frontend tests live in the frontend repo.

## Code Style & Architecture

- Dependency Injection via lifespan in app/dependencies.py
- Service layer in app/services/
- Rate limiting with slowapi; bypass token available via env
- Structured logging with structlog
- Use @pytest.mark.asyncio for async tests

## Deployment

Automated code deploys to Fly.io on push to main via `.github/workflows/ci-cd.yml`. Manual commands live in
`scripts/prod_tasks.py` (`make prod-...`). Production API: https://api.archive.ire.org.

Do not describe a code deploy as a data publication. The existing production indexing commands are legacy
operator tools and do not yet implement the atomic publication design in backend issue #11.

## Key Configuration

Environment variables documented in app/config.py and .env.example. Rate limit bypass token has no default. Fly app name defaults to ire-semantic-search but can be overridden via IRE_APP_NAME.

## Brand and Contact

Maintain IRE branding only for official deployments. For forks, replace branding and update app names. Contact: help@ire.org.
