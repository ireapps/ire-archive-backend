"""Coverage for CI and production deployment path classifications."""

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml


WORKFLOW_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-cd.yml"


def _workflow() -> dict[str, Any]:
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def _filters() -> dict[str, list[str]]:
    workflow = _workflow()
    filter_step = next(step for step in workflow["jobs"]["changes"]["steps"] if step["uses"].startswith("dorny/"))
    return yaml.load(filter_step["with"]["filters"], Loader=yaml.BaseLoader)


def _matches(filters: list[str], path: str) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in filters)


def test_acceptance_changes_run_ci_without_qualifying_for_production_deploy() -> None:
    filters = _filters()

    for path in ("README.md", "fly.acceptance.toml", "tests/test_acceptance_fly_config.py"):
        assert _matches(filters["backend"], path), path
        assert not _matches(filters["production"], path), path


def test_production_runtime_changes_qualify_for_ci_and_deployment() -> None:
    filters = _filters()

    production_paths = (
        "app/main.py",
        "scripts/index.py",
        "pyproject.toml",
        "uv.lock",
        "docker/Dockerfile",
        "docker/Dockerfile.base",
        "docker/supervisord.conf",
        "docker/entrypoint.sh",
        "docker/config/qdrant.yaml",
        "fly.toml",
        ".github/workflows/ci-cd.yml",
    )

    for path in production_paths:
        assert _matches(filters["backend"], path), path
        assert _matches(filters["production"], path), path


def test_deploy_job_uses_production_path_output() -> None:
    deploy_condition = _workflow()["jobs"]["deploy-backend"]["if"]

    assert "needs.changes.outputs.production == 'true'" in deploy_condition
