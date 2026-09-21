.PHONY: start stop api frontend install check build

# Start Qdrant via Docker
start:
	docker compose up -d

# Stop Qdrant
stop:
	docker compose down

# Install Python + Node dependencies
install:
	pip install -r requirements.txt
	npm install

# Start the FastAPI visualization server (port 8001)
api:
	uvicorn api:app --port 8001 --reload

# Start the SvelteKit dev server (port 5173)
frontend:
	npm run dev

# Type-check the SvelteKit frontend
check:
	npm run check

# Build the SvelteKit frontend for production
build:
	npm run build
