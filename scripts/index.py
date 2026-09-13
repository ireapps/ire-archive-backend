#!/usr/bin/env python3
"""Unified indexing script for IRE resources.

This script handles both local development and production indexing using
SEQUENTIAL batch processing to ensure correct text/metadata alignment.

Usage:
    Local:  ire dev index   (or: python scripts/index.py)
    Prod:   ire prod index  (runs on Fly.io with production optimizations)

The script auto-detects the environment via FLY_APP_NAME environment variable.
"""

import os
import sys
import time
from pathlib import Path

# Add parent directory to path for app imports
sys.path.append(str(Path(__file__).parent.parent))

from rich.console import Console
from rich.panel import Panel

from app.config import BATCH_SIZE, COLLECTION_NAME, VECTOR_SIZE
from scripts.data_io import find_data_file, read_resources
from scripts.models import load_dense_model, load_sparse_model
from scripts.qdrant_ops import (
    apply_production_optimizations,
    connect_qdrant,
    create_hybrid_collection,
    display_indexing_summary,
    finalize_production_indexing,
    get_batch_list,
    index_batches,
    validate_collection,
    wait_for_indexed,
)
from scripts.transforms import prepare_points, transform_documents

console = Console()


def get_memory_info() -> tuple[float, float]:
    """Get memory usage info if psutil is available.

    Returns:
        Tuple of (used_gb, percent) or (0.0, 0.0) if psutil not available
    """
    try:
        import psutil  # type: ignore[import-untyped]

        mem = psutil.virtual_memory()
        return mem.used / (1024**3), mem.percent
    except ImportError:
        return 0.0, 0.0


def display_system_info(is_production: bool) -> None:
    """Display system information panel."""
    try:
        import psutil  # type: ignore[import-untyped]

        cpu_count = psutil.cpu_count()
        total_ram = psutil.virtual_memory().total / (1024**3)
        available_ram = psutil.virtual_memory().available / (1024**3)

        mode = "PRODUCTION (Fly.io)" if is_production else "LOCAL DEVELOPMENT"
        console.print(
            Panel.fit(
                f"[bold cyan]{mode}[/bold cyan]\n"
                f"CPU Cores: {cpu_count} | "
                f"RAM: {total_ram:.1f} GB total, {available_ram:.1f} GB available | "
                f"Batch Size: {BATCH_SIZE}",
                title="System Configuration",
            )
        )

        # Memory warning for production
        if is_production and available_ram < 4:
            console.print(
                "\n[WARNING] Less than 4GB RAM available. Consider scaling up.",
                style="yellow",
            )
    except ImportError:
        mode = "PRODUCTION" if is_production else "LOCAL"
        console.print(f"\n[bold cyan]Running in {mode} mode[/bold cyan]")


def index_resources(is_production: bool = False, skip_recreate: bool = False, test_data: bool = False) -> None:
    """Main indexing function using sequential batch processing.

    This function processes documents in sequential batches to ensure
    correct alignment between text content and metadata. The previous
    parallel processing approach had a race condition where futures
    completed in non-deterministic order, causing misalignment.

    Args:
        is_production: Whether running in production environment
        skip_recreate: If True, don't delete existing collection (use when DB was manually cleared)
        test_data: If True, use test fixtures instead of full production data
    """
    start_time = time.time()
    errors: list[str] = []

    # Display system info
    display_system_info(is_production)

    # Find data file
    source_path = find_data_file(is_production=is_production, use_test_data=test_data)

    if source_path is None or not source_path.exists():
        console.print(f"[ERROR] Data file not found: {source_path}", style="red")
        console.print("\nExpected locations:", style="yellow")
        console.print("  Production: /app/data/ire-archive-data.json")
        console.print("  Local: data/ire-archive-data.json")
        sys.exit(1)

    # Load and prepare data
    try:
        resources = read_resources(source_path, test_data)
    except Exception as e:  # noqa: BLE001
        console.print(f"[ERROR] Failed to load JSON: {e}", style="red")
        sys.exit(1)

    documents, transform_errors = transform_documents(resources, errors)

    # Load embedding models
    console.print("\n[bold cyan]Loading embedding models...[/bold cyan]")

    try:
        dense_model = load_dense_model()
        console.print("   Dense model (all-MiniLM-L6-v2) loaded", style="green")
    except Exception as e:  # noqa: BLE001
        console.print(f"[ERROR] Failed to load dense model: {e}", style="red")
        sys.exit(1)

    sparse_model = load_sparse_model()
    console.print("   Sparse model (BM25) loaded", style="green")

    # Connect to Qdrant
    try:
        client = connect_qdrant(is_production)
    except Exception:  # noqa: BLE001
        sys.exit(1)

    # Create collection with hybrid vectors
    # Skip delete when DB was manually cleared (avoids Qdrant .deleted directory error)
    console.print(f"\n[bold cyan]Creating collection '{COLLECTION_NAME}'...[/bold cyan]")
    create_hybrid_collection(client, COLLECTION_NAME, VECTOR_SIZE, recreate=not skip_recreate)
    console.print(f"[SUCCESS] Collection '{COLLECTION_NAME}' created with hybrid vectors", style="green")

    # Apply production optimizations if needed
    if is_production:
        apply_production_optimizations(client)

    # Points already in the collection before this run (0 for a freshly recreated
    # collection). Used below to compute the expected count after indexing.
    try:
        initial_point_count = client.get_collection(COLLECTION_NAME).points_count or 0
    except Exception:  # noqa: BLE001
        initial_point_count = 0

    # Prepare documents for indexing
    console.print(f"\n[bold cyan]Preparing {len(documents):,} documents for indexing...[/bold cyan]")

    all_points = prepare_points(documents)

    console.print(f"[SUCCESS] Prepared {len(all_points):,} documents", style="green")

    # Process documents in batches, overlapping each batch's embedding with the
    # previous batch's (wait=False) upload instead of blocking on it.
    console.print(
        f"\n[bold cyan]Indexing {len(all_points):,} documents (batches of {BATCH_SIZE}, overlapped)...[/bold cyan]\n"
    )

    batches = get_batch_list(all_points, BATCH_SIZE)
    try:
        successful_points, failed_points = index_batches(
            client=client,
            batches=batches,
            dense_model=dense_model,
            sparse_model=sparse_model,
            errors=errors,
        )

    finally:
        # Clean up sparse model
        del sparse_model
        console.print("\n[SUCCESS] Sparse model cleaned up", style="green")

    # Batches upload with wait=False, so confirm Qdrant has actually caught up
    # with a single poll here instead of blocking on every batch's durability.
    console.print("\n[bold cyan]Verifying indexed point count...[/bold cyan]")
    expected_point_count = initial_point_count + successful_points
    total_vectors = wait_for_indexed(client, COLLECTION_NAME, expected_point_count)
    if total_vectors < expected_point_count:
        message = (
            f"Verification timed out: Qdrant reports {total_vectors:,} points, "
            f"expected at least {expected_point_count:,}"
        )
        errors.append(message)
        console.print(f"[ERROR] {message}", style="red")
    else:
        console.print(f"[SUCCESS] Qdrant reports {total_vectors:,} points", style="green")

    # Finalize production indexing
    if is_production:
        finalize_production_indexing(client)

    # Get final collection stats
    total_time = time.time() - start_time

    # Get memory info for summary
    memory_gb, _ = get_memory_info()

    # Display summary
    display_indexing_summary(
        total_documents=len(resources),
        total_chunks=len(all_points),
        total_vectors=total_vectors,
        successful_points=successful_points,
        failed_points=failed_points,
        total_time=total_time,
        errors=errors if errors else None,
        memory_gb=memory_gb if is_production else None,
        parallel_mode=False,  # Single process; batches overlap embedding with upload, not concurrent uploads.
    )

    # Run validation tests
    console.print("\n[bold cyan]Running validation tests...[/bold cyan]")
    validation_passed = validate_collection(
        client=client,
        collection_name=COLLECTION_NAME,
        dense_model=dense_model,
        test_queries=[
            "investigative reporting techniques",
            "data journalism",
            "FOIA requests",
            "source protection",
            "fact checking",
        ],
    )

    if validation_passed:
        console.print("\n[SUCCESS] Validation passed!", style="green")
    else:
        console.print("\n[WARNING] Some validation tests failed", style="yellow")

    # Final status
    if successful_points == len(all_points) and total_vectors >= expected_point_count:
        console.print("\n[bold green]Indexing completed successfully![/bold green]")
    elif successful_points == len(all_points):
        console.print(
            f"\n[yellow]Indexing completed but point-count verification did not converge "
            f"({total_vectors:,}/{expected_point_count:,} points)[/yellow]"
        )
    else:
        console.print(f"\n[yellow]Indexing completed with {failed_points} failures[/yellow]")


def main(test_data: bool = False):
    """Main entry point.

    Args:
        test_data: If True, use test fixtures instead of full production data
    """
    # Detect environment
    is_production = os.getenv("FLY_APP_NAME") is not None

    # Check if we should recreate collection (controlled by env var)
    # When true, the collection will be deleted via Qdrant API and recreated
    recreate_collection = os.getenv("CLEAR_DB_BEFORE_INDEX", "false").lower() == "true"

    console.print("\n[bold cyan]IRE Resources Unified Indexing[/bold cyan]")
    console.print("=" * 60)

    if not is_production:
        if test_data:
            console.print("\n[bold yellow]Using TEST FIXTURES for local E2E testing.[/bold yellow]")
        else:
            console.print("\n[bold yellow]WARNING: This will index all IRE resources into Qdrant.[/bold yellow]")
        console.print("This may take several minutes depending on the dataset size.")
        console.print("\nEnsure Qdrant is running: [cyan]ire dev start[/cyan]\n")

    if recreate_collection:
        console.print(
            "\n[bold yellow]CLEAR_DB_BEFORE_INDEX=true - Collection will be recreated via API[/bold yellow]\n"
        )

    # skip_recreate=False means collection WILL be recreated (deleted + created fresh)
    # skip_recreate=True means collection will be reused if it exists
    index_resources(is_production=is_production, skip_recreate=not recreate_collection, test_data=test_data)


if __name__ == "__main__":
    main()
