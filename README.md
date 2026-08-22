# IRE Archive Backend

[![CI](https://github.com/ireapps/ire-archive-backend/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/ireapps/ire-archive-backend/actions/workflows/ci-cd.yml)

A FastAPI backend for searching IRE's archive of journalism resources — tipsheets, contest entries, transcripts, datasets, and training materials from decades of investigative reporting conferences.

**Live API:** [api.archive.ire.org](https://api.archive.ire.org)

---

## Tech Stack

- **FastAPI** — Web framework
- **Qdrant** — Vector database (hybrid dense + sparse search)
- **sentence-transformers** — Dense embeddings (all-MiniLM-L6-v2)
- **fastembed** — Sparse BM25 embeddings
- **Redis** — Session storage for SSO authentication
- **uv** — Python package manager
- **Fly.io** — Production hosting
- **pytest** — Testing

---

## Architecture

The archive spans three repositories:

- [`ireapps/ire-archive-data`](https://github.com/ireapps/ire-archive-data) is the Django/Postgres editorial source
  of truth.
- This repository serves the search and MemberSuite authentication API.
- [`ireapps/ire-archive-frontend`](https://github.com/ireapps/ire-archive-frontend) consumes that API.

Qdrant is a disposable serving index, not a source of truth or backup. The agreed publication design uses
validated, immutable, versioned snapshots of approved Django records and replaces the serving collection
atomically. It is tracked in [issue #11](https://github.com/ireapps/ire-archive-backend/issues/11) and documented
in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Code deployment and data publication are separate operations.

---

## Prerequisites

- Python 3.12
- uv package manager
- Docker (for local Qdrant + Redis)

---

## Quick Start

```bash
git clone https://github.com/ireapps/ire-archive-backend.git
cd ire-archive-backend
cp .env.example .env
uv sync --all-extras
make dev-start           # Start Qdrant + Redis
make dev-index           # Index documents (requires data file — see below)
```

The API starts at http://localhost:8000. Qdrant dashboard at http://localhost:6333/dashboard.

---

## Data Files

The local source JSON data (`data/ire-archive-data.json`) contains an IRE resource catalog and is **not included**
in this repository. It is development input, not the permanent editorial record.

### Local Development

Place the file manually:

```
data/ire-archive-data.json
```

Contact the IRE team to obtain a validated development snapshot. The legacy indexer can also download a trusted
snapshot through `DATA_URL` as described below.

### Legacy production indexing

The current production indexer can read `DATA_URL`, but it predates the publication architecture and must only be
given a trusted, validated snapshot by an operator. It does not yet authenticate a publication descriptor or
perform an atomic collection switch. Do not configure it to fetch arbitrary URLs.

```bash
# Required: URL to the data file
fly secrets set DATA_URL="https://example.com/path/to/ire-archive-data.json" --app your-fly-app

# Optional: Bearer token for authenticated URLs
fly secrets set DATA_URL_TOKEN="your-token-here" --app your-fly-app
```

---

## Development

```bash
make dev-start           # Start Qdrant + Redis
make dev-index           # Index documents
make dev-status          # Check service status
make dev-logs ARGS="--follow"  # View logs (pass extra args via ARGS)
make dev-test            # Test the API
make dev-stop            # Stop services
make dev-clear-db        # Clear the index
make dev-rebuild         # Nuclear option: stop + delete + restart + re-index
```

### Running Tests

```bash
uv run pytest tests/ -v                    # All tests
uv run pytest tests/ -v --cov=app          # With coverage
uv run pytest tests/test_api/ -v           # API tests only
uv run pytest tests/ -k test_search -v     # Specific tests
```

### Linting

```bash
uv run ruff check .              # Lint
uv run ruff format --check .     # Format check
```

---

## Frontend

The frontend lives in a separate repository: [ireapps/ire-archive-frontend](https://github.com/ireapps/ire-archive-frontend)

To connect a custom frontend to this backend, set `ADDITIONAL_ALLOWED_ORIGINS`:

```bash
# In .env (local) or as a Fly secret (production)
ADDITIONAL_ALLOWED_ORIGINS='["https://your-frontend.example.com"]'
```

The API contract the frontend expects is documented in [docs/API_CONTRACT.md](docs/API_CONTRACT.md).

---

## Deployment

### Fly.io (Automated)

Pushes to main automatically deploy code via GitHub Actions after tests pass. This does not publish data.

### Fly.io (Manual)

```bash
make prod-push            # Deploy code
make prod-index           # Run the legacy Qdrant indexer
make prod-status          # Check status
make prod-logs            # View logs
make prod-rebuild         # Legacy code deploy + destructive reindex
```

These are legacy operator commands, not the target publication workflow. `--no-clear-db` only skips collection
recreation; it is not incremental synchronization and does not remove omitted or withdrawn records. Production
publication must eventually follow the atomic process in
[backend issue #11](https://github.com/ireapps/ire-archive-backend/issues/11).

### Environment Variables

See [.env.example](.env.example) for all configuration options. Key variables:

| Variable                         | Description                   |
| -------------------------------- | ----------------------------- |
| QDRANT_HOST / QDRANT_PORT        | Qdrant connection             |
| REDIS_URL                        | Redis for sessions            |
| SESSION_SECRET                   | Random 32+ char hex string    |
| MS_TENANT_ID / MS_ASSOCIATION_ID | MemberSuite SSO credentials   |
| FRONTEND_URL                     | Frontend origin for redirects |
| DATA_URL / DATA_URL_TOKEN        | Legacy trusted snapshot input |
| ADDITIONAL_ALLOWED_ORIGINS       | Extra CORS origins            |

### Updating ML Dependencies

```bash
# 1. Edit docker/Dockerfile.base with new package versions
# 2. Build and push the base image
make prod-build-base ARGS="--tag v1.1.0"
# 3. Update docker/Dockerfile to reference the new tag
# 4. Deploy
make prod-push
```

---

## Project Structure

```
├── app/                    # FastAPI application
│   ├── main.py            # Routes, middleware, CORS
│   ├── config.py          # Environment-based configuration
│   ├── models.py          # Pydantic request/response models
│   ├── dependencies.py    # Dependency injection, lifespan
│   ├── auth/              # MemberSuite SSO authentication
│   └── services/          # Search, filter, rerank, cache
├── scripts/               # Task scripts (argparse-based)
│   ├── dev_tasks.py       # Local dev commands
│   ├── prod_tasks.py      # Production commands
│   ├── setup_tasks.py     # Setup commands
│   └── index.py           # Indexing logic
├── Makefile               # make dev-*/prod-*/setup-* targets
├── tests/                  # pytest test suite
├── config/                # Qdrant configuration
├── data/                  # Tracked fixtures + local data storage
├── docs/                  # API contract, setup guides
├── docker/               # Container and compose assets
│   ├── Dockerfile        # Multi-stage production build
│   ├── Dockerfile.base   # ML dependencies base image
│   ├── docker-compose.yml # Local Qdrant + Redis
│   ├── entrypoint.sh     # Container entrypoint
│   ├── supervisord.conf  # Supervisor config
│   └── config/qdrant.yaml # Qdrant config used in container
├── fly.toml               # Fly.io configuration
└── pyproject.toml         # Python project + dependencies
```

---

## Contributing

See CONTRIBUTING.md for development guidelines, code style, and how to submit pull requests.

---

## License

MIT — see LICENSE for details.

Note: The IRE name and brand assets are the property of Investigative Reporters & Editors and are not covered by the MIT license. See the LICENSE file for details.

---

## About IRE

Investigative Reporters & Editors (IRE) is a grassroots nonprofit organization dedicated to improving the quality of investigative reporting. IRE provides resources, training, and support to journalists worldwide.
