"""Safety checks for the isolated staff-acceptance Fly configuration."""

from pathlib import Path
import tomllib


CONFIG_PATH = Path(__file__).resolve().parents[1] / "fly.acceptance.toml"


def test_acceptance_fly_config_is_isolated_from_production() -> None:
    config = tomllib.loads(CONFIG_PATH.read_text())

    assert config["app"] == "ire-archive-acceptance-search"
    assert config["primary_region"] == "ord"
    assert config["env"] == {
        "COLLECTION_NAME": "ire_archive_acceptance",
        "SERVING_COLLECTION_ALIAS": "ire_archive_acceptance_live",
        "LOG_LEVEL": "info",
        "MODEL_NAME": "all-MiniLM-L6-v2",
        "PORT": "8000",
        "PYTHONUNBUFFERED": "1",
        "QDRANT_HOST": "localhost",
        "QDRANT_PORT": "6333",
        "VECTOR_SIZE": "384",
    }

    config_text = CONFIG_PATH.read_text()
    for forbidden_value in (
        "ire-semantic-search",
        "api.archive.ire.org",
        "DATA_URL",
        "PUBLICATION_",
        "REDIS_URL",
        "MS_",
        "SESSION_SECRET",
        "FRONTEND_URL",
    ):
        assert forbidden_value not in config_text


def test_acceptance_fly_config_keeps_qdrant_private_and_persistent() -> None:
    config = tomllib.loads(CONFIG_PATH.read_text())

    service = config["services"][0]
    assert service["internal_port"] == 8000
    assert service["auto_stop_machines"] is False
    assert service["min_machines_running"] == 1
    assert {port["port"] for port in service["ports"]} == {80, 443}
    assert service["http_checks"] == [
        {
            "interval": "45s",
            "grace_period": "180s",
            "method": "get",
            "path": "/healthz",
            "protocol": "http",
            "timeout": "10s",
            "tls_skip_verify": False,
        }
    ]
    assert config["mounts"] == [
        {
            "source": "qdrant_acceptance_data",
            "destination": "/data/qdrant_storage",
            "initial_size": "3gb",
        }
    ]
    assert config["vm"] == [{"cpu_kind": "shared", "cpus": 2, "memory_mb": 4096}]
