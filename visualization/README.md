# IRE Archive Embedding Explorer

Interactive 2D visualization of IRE archive document embeddings using SvelteKit 5 and D3.

Documents are projected from 384-dimensional embedding space to 2D via UMAP, then rendered as a
zoomable, pannable scatter plot — color-coded by category with hover tooltips and click-to-inspect.

## Quick Start

```bash
# From the repo root, start the backend + Qdrant
make dev-start
make dev-index

# Start the visualization dev server
cd visualization
npm install
npm run dev
```

The visualization opens at http://localhost:5173 and fetches data from the backend at http://localhost:8000.

## Configuration

| Variable            | Default                 | Description          |
| ------------------- | ----------------------- | -------------------- |
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend API base URL |

Set via `.env` in this directory:

```
VITE_API_BASE_URL=https://api.archive.ire.org
```

## Backend Endpoint

The visualization consumes `GET /visualize/embeddings` on the backend, which:

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
