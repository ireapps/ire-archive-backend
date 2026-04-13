# IRE Archive Embedding Explorer

Interactive 2D visualization of IRE archive document embeddings using SvelteKit 5 and D3.

Documents are projected from 384-dimensional embedding space to 2D via UMAP, then rendered as a
zoomable, pannable scatter plot — color-coded by category with hover tooltips and click-to-inspect.

> **Decoupled from production** — the visualization has its own lightweight API server
> (`api.py`) so that `umap-learn` and its heavy dependencies never ship to Fly.io.

## Quick Start

```bash
# From the repo root, start Qdrant and index data
make dev-start
make dev-index

# Install the visualization's Python dependencies (one-time)
pip install umap-learn fastapi uvicorn qdrant-client numpy structlog python-dotenv

# Start the standalone visualization API (port 8001)
cd visualization
uvicorn api:app --port 8001

# In a second terminal, start the SvelteKit frontend (port 5173)
cd visualization
npm install
npm run dev
```

The visualization opens at http://localhost:5173 and fetches data from the
standalone API at http://localhost:8001.

## Configuration

| Variable            | Default                 | Description                     |
| ------------------- | ----------------------- | ------------------------------- |
| `VITE_API_BASE_URL` | `http://localhost:8001` | Visualization API base URL      |
| `QDRANT_HOST`       | `localhost`             | Qdrant host (for `api.py`)      |
| `QDRANT_PORT`       | `6333`                  | Qdrant port (for `api.py`)      |
| `COLLECTION_NAME`   | `nonprofit_knowledge`   | Qdrant collection (for `api.py`)|

## Architecture

```
visualization/
├── api.py                    # Standalone FastAPI server (UMAP + Qdrant)
├── visualization_service.py  # UMAP projection pipeline
├── src/                      # SvelteKit 5 frontend
│   ├── lib/components/       # D3 canvas map, legend, tooltip, detail panel
│   └── routes/+page.svelte   # Main page
└── package.json
```

The `api.py` server connects directly to Qdrant, runs UMAP, and serves the
2D projection. It is **not** part of the main `app/` package and does **not**
deploy to Fly.io.

## API Endpoint

`GET /visualize/embeddings` on the standalone server (default port 8001):

1. Scrolls all dense vectors from Qdrant
2. Runs UMAP dimensionality reduction (384-dim → 2D)
3. Returns normalized coordinates + document metadata
4. Caches the result for 1 hour

Query parameters:

- `sample` — limit to N documents (faster dev iteration)
- `n_neighbors` — UMAP locality (default 15)
- `min_dist` — UMAP min distance (default 0.1)

## Stack

- **SvelteKit 5** — Svelte 5 runes + TypeScript
- **D3** — zoom/pan behavior + quadtree hit detection
- **Canvas** — hardware-accelerated rendering for thousands of points
- **FastAPI** — standalone lightweight API (not part of the main app)
