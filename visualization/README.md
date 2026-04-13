# IRE Archive Embedding Explorer

Interactive 2D visualization of [IRE](https://www.ire.org/) archive document embeddings using SvelteKit 5 and D3.

Documents are projected from 384-dimensional embedding space to 2D via UMAP, then rendered as a
zoomable, pannable scatter plot — color-coded by category with hover tooltips and click-to-inspect.

![Stack: SvelteKit 5 · D3 · FastAPI · UMAP · Qdrant](https://img.shields.io/badge/stack-SvelteKit_5_%C2%B7_D3_%C2%B7_FastAPI_%C2%B7_UMAP_%C2%B7_Qdrant-blue)

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (for Qdrant)
- [Python 3.12+](https://www.python.org/)
- [Node.js 20+](https://nodejs.org/)
- A populated Qdrant collection (see [ire-archive-backend](https://github.com/ireapps/ire-archive-backend) for indexing)

## Quick Start

```bash
# 1. Start Qdrant
make start

# 2. Install Python + Node dependencies (one-time)
make install

# 3. Start the API server (terminal 1)
make api

# 4. Start the frontend dev server (terminal 2)
make frontend
```

The visualization opens at http://localhost:5173 and fetches data from the API at http://localhost:8001.

## Configuration

Copy `.env.example` to `.env` and adjust as needed:

```bash
cp .env.example .env
```

| Variable            | Default                 | Description                         |
| ------------------- | ----------------------- | ----------------------------------- |
| `VITE_API_BASE_URL` | `http://localhost:8001`  | API URL used by the SvelteKit app   |
| `QDRANT_HOST`       | `localhost`              | Qdrant host                         |
| `QDRANT_PORT`       | `6333`                  | Qdrant port                         |
| `COLLECTION_NAME`   | `nonprofit_knowledge`   | Qdrant collection name              |
| `SCROLL_BATCH_SIZE` | `100`                   | Batch size for scrolling vectors    |

## Architecture

```
├── api.py                    # FastAPI server (UMAP + Qdrant)
├── visualization_service.py  # UMAP projection pipeline
├── docker-compose.yml        # Qdrant container
├── requirements.txt          # Python dependencies
├── Makefile                  # Dev workflow commands
├── package.json              # Node dependencies
├── src/                      # SvelteKit 5 frontend
│   ├── lib/
│   │   ├── components/
│   │   │   ├── EmbeddingMap.svelte   # Canvas scatter plot + D3 zoom
│   │   │   ├── Legend.svelte         # Category toggle filter
│   │   │   ├── Tooltip.svelte        # Hover tooltip
│   │   │   └── PointDetail.svelte    # Click-to-inspect detail panel
│   │   └── types.ts                  # Shared TypeScript types
│   └── routes/+page.svelte           # Main page
└── static/                   # Static assets
```

## API

`GET /visualize/embeddings` — returns a 2D UMAP projection of all document embeddings.

| Parameter      | Default | Description                                  |
| -------------- | ------- | -------------------------------------------- |
| `sample`       | —       | Limit to N documents (faster dev iteration)  |
| `n_neighbors`  | `15`    | UMAP locality — larger = more global         |
| `min_dist`     | `0.1`   | UMAP minimum distance between points         |

Response:

```json
{
  "points": [
    { "x": 0.123, "y": -0.456, "vector_id": "...", "title": "...", "category": "tipsheet", ... }
  ],
  "meta": {
    "total": 1234,
    "elapsed_seconds": 5.2,
    "umap_params": { "n_neighbors": 15, "min_dist": 0.1 },
    "categories": { "tipsheet": 800, "contest entry": 200, ... }
  }
}
```

## Make Targets

| Target     | Description                             |
| ---------- | --------------------------------------- |
| `start`    | Start Qdrant via Docker Compose         |
| `stop`     | Stop Qdrant                             |
| `install`  | Install Python + Node dependencies      |
| `api`      | Start the FastAPI server (port 8001)    |
| `frontend` | Start the SvelteKit dev server (:5173)  |
| `check`    | Type-check the SvelteKit frontend       |
| `build`    | Build the SvelteKit frontend            |

## Stack

- **[SvelteKit 5](https://svelte.dev/)** — Svelte 5 runes + TypeScript
- **[D3](https://d3js.org/)** — zoom/pan behavior + quadtree hit detection
- **Canvas** — hardware-accelerated rendering for thousands of points
- **[FastAPI](https://fastapi.tiangolo.com/)** — lightweight API server
- **[UMAP](https://umap-learn.readthedocs.io/)** — dimensionality reduction (384-dim → 2D)
- **[Qdrant](https://qdrant.tech/)** — vector database
