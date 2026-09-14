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


def test_docs_only_changes_run_ci_without_qualifying_for_staging_deploy() -> None:
    filters = _filters()

    for path in (
        "README.md",
        "tests/test_acceptance_fly_config.py",
        ".github/workflows/ci-cd.yml",
    ):
        assert _matches(filters["backend"], path), path
        assert not _matches(filters["deploy"], path), path


def test_runtime_changes_qualify_for_ci_and_staging_deploy() -> None:
    filters = _filters()

    deploy_paths = (
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
        # The staging deploy job uses this file as its Fly config, so changes
        # to it must also trigger an automatic staging redeploy.
        "fly.acceptance.toml",
    )

    for path in deploy_paths:
        assert _matches(filters["backend"], path), path
        assert _matches(filters["deploy"], path), path


def test_staging_deploy_job_uses_deploy_path_output() -> None:
    deploy_condition = _workflow()["jobs"]["deploy-staging"]["if"]

    assert "needs.changes.outputs.deploy == 'true'" in deploy_condition


def test_staging_deploy_job_never_targets_production() -> None:
    deploy_job = _workflow()["jobs"]["deploy-staging"]

    for step in deploy_job["steps"]:
        run = step.get("run", "")
        assert "ire-semantic-search" not in run
        assert "fly.toml" not in run or "fly.acceptance.toml" in run

    assert deploy_job["environment"]["name"] == "staging-backend"


def test_promote_production_workflow_requires_manual_dispatch() -> None:
    promote_path = WORKFLOW_PATH.parent / "promote-production.yml"
    workflow = yaml.load(promote_path.read_text(), Loader=yaml.BaseLoader)

    assert "workflow_dispatch" in workflow["on"]
    assert "push" not in workflow["on"]
    assert "pull_request" not in workflow["on"]

    promote_job = workflow["jobs"]["promote-production"]
    assert "ire-semantic-search" in " ".join(step.get("run", "") for step in promote_job["steps"])
    assert promote_job["environment"]["name"] == "production-backend"
