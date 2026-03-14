"""Production deployment tasks (argparse-based)."""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from collections.abc import Callable

from rich.table import Table

from scripts.cli_utils import (
    APP_NAME,
    FLY_BIN,
    check_fly_cli,
    confirm,
    confirm_destructive,
    console,
    error,
    info,
    run_command,
    section,
    success,
    warning,
)

SCALE_RETRY_DELAY = 60  # seconds between retry attempts when scale fails


def cmd_push(args: argparse.Namespace) -> None:
    """Code-only deployment: push app code without reindexing."""
    section("Code-Only Deployment")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        console.print("\nInstall with:")
        console.print("  curl -L https://fly.io/install.sh | sh")
        sys.exit(1)

    info(
        "Building locally and deploying..." if args.local else "Building with Fly.io remote builders (x86_64 native)..."
    )
    console.print()

    try:
        cmd = [FLY_BIN, "deploy", "--app", APP_NAME]
        if not args.local:
            cmd.append("--remote-only")

        run_command(cmd)
        console.print()
        success("Code deployment complete!")
        console.print("\n[bold cyan]Next steps:[/bold cyan]")
        console.print("  1. Check status: make prod-status")
        console.print("  2. View logs: make prod-logs")
        console.print("\n[yellow]Note: This was a code-only deploy. For full rebuild, use 'make prod-rebuild'[/yellow]")
    except Exception as exc:
        error(f"Deployment failed: {exc}")
        sys.exit(1)


def cmd_status(_args: argparse.Namespace) -> None:
    """Show production application status."""
    section("Production Status")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        sys.exit(1)

    try:
        run_command([FLY_BIN, "status", "--app", APP_NAME])
    except Exception as exc:
        error(f"Failed to get status: {exc}")
        sys.exit(1)


def cmd_logs(args: argparse.Namespace) -> None:
    """Show production application logs."""
    section("Production Logs")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        sys.exit(1)

    try:
        if args.follow:
            console.print("[yellow]Following logs (Ctrl+C to stop)...[/yellow]\n")
            run_command([FLY_BIN, "logs", "--app", APP_NAME])
        else:
            run_command([FLY_BIN, "logs", "--app", APP_NAME])
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped following logs[/yellow]")
    except Exception as exc:
        error(f"Failed to get logs: {exc}")
        sys.exit(1)


def cmd_ssh(_args: argparse.Namespace) -> None:
    """SSH into production application."""
    section("SSH to Production")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        sys.exit(1)

    info("Opening SSH console...")
    console.print()

    try:
        run_command([FLY_BIN, "ssh", "console", "--app", APP_NAME])
    except Exception as exc:
        error(f"SSH failed: {exc}")
        sys.exit(1)


def cmd_restart(_args: argparse.Namespace) -> None:
    """Restart production application."""
    if not confirm_destructive(
        "restart the production application",
        "This will briefly interrupt service while the app restarts.",
    ):
        console.print("\n[yellow]Operation cancelled[/yellow]")
        return

    section("Restarting Production")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        sys.exit(1)

    try:
        run_command([FLY_BIN, "apps", "restart", APP_NAME])
        console.print()
        success("Application restarted!")
    except Exception as exc:
        error(f"Restart failed: {exc}")
        sys.exit(1)


def cmd_clear_cache(_args: argparse.Namespace) -> None:
    """Clear all API caches in production (search, resource, similar)."""
    section("Clearing Production API Cache")

    info("Clearing all caches...")

    try:
        import httpx

        response = httpx.post(f"https://{APP_NAME}.fly.dev/admin/clear-cache", timeout=30.0)
        if response.status_code == 200:
            result = response.json()
            success(result.get("message", "Caches cleared"))
        else:
            error(f"API returned status {response.status_code}")
            sys.exit(1)

    except ImportError:
        error("httpx not installed")
        console.print("\nInstall with: uv pip install httpx")
        sys.exit(1)
    except Exception as exc:
        error(f"Failed to clear cache: {exc}")
        sys.exit(1)


def cmd_stats(_args: argparse.Namespace) -> None:
    """Show production collection statistics."""
    section("Production Collection Stats")

    info("Fetching collection statistics...")
    console.print()

    try:
        import httpx

        response = httpx.get(f"https://{APP_NAME}.fly.dev/stats", timeout=30.0)
        if response.status_code == 200:
            stats = response.json()

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="white")

            table.add_row("Collection", stats.get("collection") or stats.get("collection_name", "N/A"))
            table.add_row("Total Points", str(stats.get("total_points", stats.get("points_count", 0))))
            table.add_row("Vector Size", str(stats.get("vectors_size", stats.get("vector_size", 0))))
            table.add_row("Indexed Vectors", str(stats.get("indexed_vectors_count", 0)))

            console.print(table)
        else:
            error(f"API returned status {response.status_code}")
            sys.exit(1)

    except ImportError:
        error("httpx not installed")
        console.print("\nInstall with: uv pip install httpx")
        sys.exit(1)
    except Exception as exc:
        error(f"Failed to fetch stats: {exc}")
        sys.exit(1)


def _scale_vm(size: str, extra: list[str] | None = None) -> None:
    cmd = [FLY_BIN, "scale", "vm", size, "--app", APP_NAME]
    if extra:
        cmd.extend(extra)
    run_command(cmd)


def cmd_index(args: argparse.Namespace) -> None:
    """Scale-up-in-place indexing via SSH with temporary VM resize."""
    import subprocess

    section("Production Indexing (Scale-Up-In-Place)")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        sys.exit(1)

    info("Starting production indexing with Scale-Up-In-Place approach...")
    console.print("\nThis will:")
    console.print("  1. Scale VM to 16GB RAM (performance-8x)")
    console.print("  2. Run indexer via SSH with real-time logs")
    console.print("  3. Scale back down to 4GB RAM")
    console.print("\n[yellow]Estimated time: 10-15 minutes[/yellow]")
    console.print("[yellow]Estimated cost: ~$0.10[/yellow]\n")

    if not args.yes and not confirm("Continue?", default=False):
        console.print("\n[yellow]Operation cancelled[/yellow]")
        return

    try:
        section("Step 1: Scaling VM to 16GB RAM")
        info("Scaling to performance-8x (16GB RAM, 8 CPU)...")
        try:
            _scale_vm("performance-8x")
        except Exception as exc:
            warning(f"Scale up failed ({exc}); retrying in {SCALE_RETRY_DELAY}s...")
            time.sleep(SCALE_RETRY_DELAY)
            _scale_vm("performance-8x")
        success("VM scaled to performance-8x")
        console.print()

        info("Waiting for machine to be ready...")
        time.sleep(30)

        section("Step 2: Running Indexer via SSH")
        info("Executing indexer script (this will take 10-15 minutes)...")
        console.print("[dim]Real-time logs will appear below:[/dim]\n")

        # Build environment variables to forward to the remote machine
        env_parts: list[str] = []
        if args.clear_db:
            env_parts.append("CLEAR_DB_BEFORE_INDEX=true")

        data_url = os.environ.get("DATA_URL")
        if data_url:
            env_parts.append(f"DATA_URL={shlex.quote(data_url)}")

        data_url_token = os.environ.get("DATA_URL_TOKEN")
        if data_url_token:
            env_parts.append(f"DATA_URL_TOKEN={shlex.quote(data_url_token)}")

        if env_parts:
            env_str = " ".join(env_parts)
            remote_cmd = f"sh -c '{env_str} python /app/scripts/index.py'"
        else:
            remote_cmd = "python /app/scripts/index.py"

        ssh_cmd = [
            FLY_BIN,
            "ssh",
            "console",
            "--app",
            APP_NAME,
            "--command",
            remote_cmd,
        ]

        try:
            run_command(ssh_cmd)
            success("Indexer completed successfully")
        except subprocess.CalledProcessError as exc:
            error(f"Indexer failed with exit code {exc.returncode}")
            console.print("\n[yellow]Will still scale down VM...[/yellow]")
        except Exception as exc:
            error(f"SSH command failed: {exc}")
            console.print("\n[yellow]Will still scale down VM...[/yellow]")

        console.print()

        section("Step 3: Scaling VM Back Down")
        info("Scaling to shared-cpu-2x (4GB RAM)...")
        try:
            _scale_vm("shared-cpu-2x", ["--memory", "4096"])
        except Exception as exc:
            warning(f"Scale down failed ({exc}); retrying in {SCALE_RETRY_DELAY}s...")
            time.sleep(SCALE_RETRY_DELAY)
            _scale_vm("shared-cpu-2x", ["--memory", "4096"])
        success("VM scaled back to shared-cpu-2x (4GB)")
        console.print()

        info("Waiting for machine to stabilize...")
        time.sleep(15)

        section("Step 4: Verifying Results")
        import httpx

        info("Checking database statistics...")

        max_retries = 6
        for attempt in range(max_retries):
            try:
                response = httpx.get(f"https://{APP_NAME}.fly.dev/stats", timeout=30.0)
                if response.status_code == 200:
                    stats = response.json()
                    total_points = stats.get("total_points", 0)
                    if total_points > 0:
                        console.print()
                        success(f"Indexing complete! {total_points:,} documents indexed.")
                        return
                    if attempt < max_retries - 1:
                        console.print(
                            f"[dim]Attempt {attempt + 1}/{max_retries}: Database appears empty, retrying in 10s...[/dim]"
                        )
                        time.sleep(10)
                    else:
                        error("Indexing may have failed - database is empty")
                        console.print("\nDebug steps:")
                        console.print("  1. Check logs: make prod-logs")
                        console.print("  2. SSH in: make prod-ssh")
                        console.print("  3. Verify stats: make prod-stats")
                        sys.exit(1)
                else:
                    if attempt < max_retries - 1:
                        console.print(
                            f"[dim]Attempt {attempt + 1}/{max_retries}: API returned {response.status_code}, retrying...[/dim]"
                        )
                        time.sleep(10)
                    else:
                        warning(f"Could not verify indexing (API returned {response.status_code})")
                        console.print("\nVerify manually with: make prod-stats")
            except httpx.RequestError as exc:
                if attempt < max_retries - 1:
                    console.print(
                        f"[dim]Attempt {attempt + 1}/{max_retries}: API not responding ({exc}), retrying...[/dim]"
                    )
                    time.sleep(10)
                else:
                    warning("Could not verify indexing - API not responding")
                    console.print("\nVerify manually with: make prod-stats")

    except Exception as exc:
        error(f"Indexing failed: {exc}")
        try:
            warning("Attempting to scale down VM after failure...")
            try:
                _scale_vm("shared-cpu-2x", ["--memory", "4096"])
            except Exception:
                warning(f"Scale down retrying in {SCALE_RETRY_DELAY}s...")
                time.sleep(SCALE_RETRY_DELAY)
                _scale_vm("shared-cpu-2x", ["--memory", "4096"])
        except Exception:
            warning(
                f"Could not scale down VM - do this manually: {FLY_BIN} scale vm shared-cpu-2x --memory 4096 --app {APP_NAME} --yes"
            )
        sys.exit(1)


def cmd_verify_ids(_args: argparse.Namespace) -> None:
    """Verify database is indexed and accessible."""
    section("Verifying Production Database")

    info("Checking database statistics...")
    console.print()

    try:
        import httpx

        response = httpx.get(f"https://{APP_NAME}.fly.dev/stats", timeout=30.0)
        if response.status_code == 200:
            stats = response.json()

            table = Table(show_header=True, header_style="bold cyan")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="white")

            table.add_row("Collection", stats.get("collection", "N/A"))
            table.add_row("Total Points", str(stats.get("total_points", 0)))
            table.add_row("Vector Size", str(stats.get("vectors_size", 0)))
            table.add_row("Indexed Vectors", str(stats.get("indexed_vectors_count", 0)))
            table.add_row("Status", stats.get("status", "N/A"))

            console.print(table)

            total_points = stats.get("total_points", 0)
            if total_points > 0:
                success(f"Database verified! {total_points:,} documents indexed.")
            else:
                warning("Database is empty. You may need to run 'make prod-index'")
        else:
            error(f"API returned status {response.status_code}")
            sys.exit(1)

    except ImportError:
        error("httpx not installed")
        console.print("\nInstall with: uv pip install httpx")
        sys.exit(1)
    except Exception as exc:
        error(f"Verification failed: {exc}")
        sys.exit(1)


def cmd_rebuild(args: argparse.Namespace) -> None:
    """Full rebuild: deploy code + clear database + reindex from scratch."""
    section("Complete Rebuild Workflow")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        sys.exit(1)

    console.print("\n[bold cyan]This will execute:[/bold cyan]")
    console.print("  1. Deploy app code (API server)")
    console.print("  2. Spin up ephemeral machine (16GB RAM)")
    console.print("     - Starts Qdrant + indexer in same container")
    console.print("     - Clears database and reindexes from scratch")
    console.print("     - Auto-destroys when complete")
    console.print("\n[yellow]Estimated time: 15-20 minutes[/yellow]")
    console.print("[yellow]Estimated cost: ~$0.10[/yellow]\n")

    if not args.yes and not confirm("Continue with complete rebuild?", default=False):
        console.print("\n[yellow]Operation cancelled[/yellow]")
        return

    try:
        section("Deploying Application Code")
        run_command(
            [
                FLY_BIN,
                "deploy",
                "--app",
                APP_NAME,
                "--remote-only",
            ]
        )
        success("App deployed!")
        console.print()

        section("Clearing Database and Reindexing")
        info("Indexer will clear database and rebuild from scratch...")
        cmd_index(argparse.Namespace(yes=True, clear_db=True))

        console.print()
        success("Complete rebuild finished!")
        console.print("\n[bold cyan]Application is ready:[/bold cyan]")
        console.print(f"  https://{APP_NAME}.fly.dev")
        console.print("\nVerify with:")
        console.print("  make prod-stats")

    except Exception as exc:
        error(f"Rebuild failed: {exc}")
        sys.exit(1)


def cmd_scale(_args: argparse.Namespace) -> None:
    """Show VM scaling options."""
    section("VM Scaling")

    if not check_fly_cli():
        error("Fly CLI is not installed")
        sys.exit(1)

    console.print("[bold cyan]Current VM Configuration:[/bold cyan]\n")

    try:
        run_command([FLY_BIN, "scale", "show", "--app", APP_NAME])

        console.print("\n[bold cyan]Available VM Sizes:[/bold cyan]")
        console.print("  shared-cpu-1x:    256MB RAM, 1 CPU   (~$2/month)")
        console.print("  shared-cpu-2x:    2GB RAM, 2 CPU    (~$11/month)")
        console.print("  performance-8x:   16GB RAM, 8 CPU   (~$200/month)")
        console.print("\n[yellow]To change VM size:[/yellow]")
        console.print(f"  fly scale vm <size> --app {APP_NAME} --yes")

    except Exception as exc:
        error(f"Failed to show scaling: {exc}")
        sys.exit(1)


def cmd_build_base(args: argparse.Namespace) -> None:
    """Build and optionally push ML dependencies base image."""
    section(f"Building ML Base Image: {args.tag}")

    if not args.tag.startswith("v"):
        error("Tag must start with 'v' (e.g., v1.0.0)")
        sys.exit(1)

    image_name = f"ghcr.io/ireapps/ire-ml-base:{args.tag}"

    info(f"Building base image: {image_name}")
    console.print("\n[yellow]This will take 10-15 minutes...[/yellow]\n")

    try:
        info("Building image with Docker...")
        run_command(
            [
                "docker",
                "build",
                "-f",
                "docker/Dockerfile.base",
                "-t",
                image_name,
                "--platform",
                "linux/amd64",
                ".",
            ]
        )
        console.print()
        success(f"Built image: {image_name}")

        if args.push:
            console.print()
            info("Pushing image to GitHub Container Registry...")
            console.print("\n[yellow]Note: Ensure you're logged in to GHCR with:[/yellow]")
            console.print("  docker login ghcr.io -u ireapps\n")

            run_command(["docker", "push", image_name])
            console.print()
            success(f"Pushed image: {image_name}")

            console.print("\n[bold cyan]Next steps:[/bold cyan]")
            console.print(f"  1. Update docker/Dockerfile to use {args.tag}")
            console.print("  2. Commit changes: git add docker/Dockerfile.base docker/Dockerfile")
            console.print(f'  3. Commit: git commit -m "Update ML base image to {args.tag}"')
            console.print("  4. Deploy: make prod-push")
        else:
            console.print("\n[yellow]Image built but not pushed (use --push to push)[/yellow]")
            console.print("\n[bold cyan]To push later:[/bold cyan]")
            console.print(f"  docker push {image_name}")

    except Exception as exc:
        error(f"Failed to build base image: {exc}")
        sys.exit(1)


COMMANDS: dict[str, tuple[str, Callable[[argparse.Namespace], None]]]
COMMANDS = {
    "push": ("Code-only deployment", cmd_push),
    "status": ("Show production application status", cmd_status),
    "logs": ("Show production application logs", cmd_logs),
    "ssh": ("SSH into production application", cmd_ssh),
    "restart": ("Restart production application", cmd_restart),
    "clear-cache": ("Clear production API caches", cmd_clear_cache),
    "stats": ("Show production collection statistics", cmd_stats),
    "index": ("Production indexing (scale-up-in-place)", cmd_index),
    "verify-ids": ("Verify production database is indexed", cmd_verify_ids),
    "rebuild": ("Full rebuild: deploy + clear DB + index", cmd_rebuild),
    "scale": ("Show VM scaling options", cmd_scale),
    "build-base": ("Build ML base image", cmd_build_base),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Production deployment tasks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    push_parser = subparsers.add_parser("push", help=COMMANDS["push"][0])
    push_parser.add_argument("--local", action="store_true", help="Build locally instead of using remote builders")

    subparsers.add_parser("status", help=COMMANDS["status"][0])

    logs_parser = subparsers.add_parser("logs", help=COMMANDS["logs"][0])
    logs_parser.add_argument("--follow", "-f", action="store_true", help="Follow log output")

    subparsers.add_parser("ssh", help=COMMANDS["ssh"][0])
    subparsers.add_parser("restart", help=COMMANDS["restart"][0])
    subparsers.add_parser("clear-cache", help=COMMANDS["clear-cache"][0])
    subparsers.add_parser("stats", help=COMMANDS["stats"][0])

    index_parser = subparsers.add_parser("index", help=COMMANDS["index"][0])
    index_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    index_parser.add_argument(
        "--clear-db",
        dest="clear_db",
        action="store_true",
        default=True,
        help="Clear database before indexing (default: true)",
    )
    index_parser.add_argument(
        "--no-clear-db",
        dest="clear_db",
        action="store_false",
        help="Do not clear database before indexing",
    )

    subparsers.add_parser("verify-ids", help=COMMANDS["verify-ids"][0])

    rebuild_parser = subparsers.add_parser("rebuild", help=COMMANDS["rebuild"][0])
    rebuild_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    subparsers.add_parser("scale", help=COMMANDS["scale"][0])

    build_base_parser = subparsers.add_parser("build-base", help=COMMANDS["build-base"][0])
    build_base_parser.add_argument("--tag", required=True, help="Version tag for base image (e.g., v1.0.0)")
    build_base_parser.add_argument(
        "--push", dest="push", action="store_true", default=True, help="Push image after building"
    )
    build_base_parser.add_argument("--no-push", dest="push", action="store_false", help="Build only, do not push")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    _, handler = COMMANDS[args.command]
    handler(args)


if __name__ == "__main__":
    main()
