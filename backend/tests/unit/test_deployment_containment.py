"""Static regression checks for image and Compose containment."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = BACKEND_DIR.parent


def test_signing_material_has_no_application_image_or_compose_default() -> None:
    from app.config import Settings

    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    configured = Settings(_env_file=None)  # type: ignore[call-arg]

    assert configured.secret_key == ""
    assert "DOCVAULT_SECRET_KEY" not in dockerfile
    assert "DOCVAULT_SECRET_KEY" not in compose


def test_production_qdrant_is_private_and_dev_publication_is_explicit() -> None:
    production = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    development = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )

    assert "ports" not in production["services"]["qdrant"]
    assert development["services"]["qdrant"]["ports"] == [
        "6333:6333",
        "6334:6334",
    ]


def test_container_defaults_are_no_egress_and_use_split_probes() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    production = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    backend = production["services"]["backend"]

    assert backend["environment"]["DOCVAULT_LLM_PROVIDER"] == (
        "${DOCVAULT_LLM_PROVIDER:-none}"
    )
    assert backend["environment"]["DOCVAULT_ALLOW_EXTERNAL_LLM"] == (
        "${DOCVAULT_ALLOW_EXTERNAL_LLM:-false}"
    )
    assert backend["healthcheck"]["test"] == [
        "CMD",
        "python",
        "-m",
        "app.container_healthcheck",
        "--ready",
    ]
    assert "app.container_healthcheck" in dockerfile
    assert "/api/v1/live" in (
        BACKEND_DIR / "app" / "container_healthcheck.py"
    ).read_text(encoding="utf-8")
    assert "snapshot_download" not in (BACKEND_DIR / "entrypoint.sh").read_text(
        encoding="utf-8"
    )


def test_runtime_image_uses_stdlib_probe_and_one_pdf_stack() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert 'CMD ["python", "-m", "app.container_healthcheck"]' in dockerfile
    assert "curl" not in dockerfile
    assert "poppler-utils" not in dockerfile
    assert "tesseract-ocr" in dockerfile
    assert "libgl1" in dockerfile
    assert "libglib2.0-0" in dockerfile


def test_container_provider_resources_are_explicitly_bounded() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    production = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    environment = production["services"]["backend"]["environment"]
    expected = {
        "DOCVAULT_RAG_PROVIDER_CONNECT_TIMEOUT_SECONDS": "3",
        "DOCVAULT_RAG_PROVIDER_READ_TIMEOUT_SECONDS": "20",
        "DOCVAULT_RAG_PROVIDER_TOTAL_TIMEOUT_SECONDS": "30",
        "DOCVAULT_RAG_PROVIDER_MAX_OUTPUT_TOKENS": "512",
        "DOCVAULT_RAG_MAX_CONTEXT_BYTES": "32768",
        "DOCVAULT_RAG_PROVIDER_MAX_CONCURRENCY": "2",
    }

    for field, default in expected.items():
        assert environment[field] == f"${{{field}:-{default}}}"
        assert f'{field}="{default}"' in dockerfile


def test_container_folder_import_is_disabled_and_resource_bounded() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    production = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    environment = production["services"]["backend"]["environment"]

    assert environment["DOCVAULT_FOLDER_IMPORT_ENABLED"] == (
        "${DOCVAULT_FOLDER_IMPORT_ENABLED:-false}"
    )
    assert environment["DOCVAULT_FOLDER_IMPORT_ROOTS"] == (
        "${DOCVAULT_FOLDER_IMPORT_ROOTS:-[]}"
    )
    assert environment["DOCVAULT_FOLDER_IMPORT_MAX_VISITED_ENTRIES"].endswith(":-5000}")
    assert environment["DOCVAULT_FOLDER_IMPORT_MAX_FILES"].endswith(":-500}")
    assert environment["DOCVAULT_FOLDER_IMPORT_MAX_TOTAL_MB"].endswith(":-500}")
    assert environment["DOCVAULT_FOLDER_IMPORT_MAX_DEPTH"].endswith(":-10}")
    assert environment["DOCVAULT_FOLDER_IMPORT_MAX_SECONDS"].endswith(":-30}")
    assert 'DOCVAULT_FOLDER_IMPORT_ENABLED="false"' in dockerfile


def test_initial_administrator_has_no_automatic_or_environment_password() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    compose = (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    entrypoint = (BACKEND_DIR / "entrypoint.sh").read_text(encoding="utf-8")

    for surface in (dockerfile, compose, entrypoint):
        assert "INITIAL_ADMIN_PASSWORD" not in surface
        assert "provision_admin" not in surface


def test_backend_uses_fixed_non_root_identity_and_drops_capabilities() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    production = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    backend = production["services"]["backend"]

    assert "USER 10001:10001" in dockerfile
    assert "--shell /usr/sbin/nologin" in dockerfile
    assert backend["user"] == "10001:10001"
    assert backend["cap_drop"] == ["ALL"]
    assert backend["security_opt"] == ["no-new-privileges:true"]


def test_backend_and_qdrant_use_private_runtime_networks() -> None:
    production = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    development = yaml.safe_load(
        (REPOSITORY_ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    services = production["services"]

    assert "ports" not in services["backend"]
    assert services["backend"]["expose"] == ["8000"]
    assert development["services"]["backend"]["ports"] == ["8000:8000"]
    assert services["backend"]["networks"] == [
        "docvault-net",
        "docvault-data-net",
    ]
    assert services["frontend"]["networks"] == ["docvault-net"]
    assert services["qdrant"]["networks"] == ["docvault-data-net"]
    assert production["networks"]["docvault-data-net"]["internal"] is True


def test_runtime_paths_are_owned_and_preflighted_for_non_root() -> None:
    dockerfile = (BACKEND_DIR / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (BACKEND_DIR / "entrypoint.sh").read_text(encoding="utf-8")

    assert "chown -R 10001:10001" in dockerfile
    assert "/data" in dockerfile
    assert "/hf_cache" in dockerfile
    assert 'HOME="/home/docvault"' in dockerfile
    assert 'if [ "$(id -u)" -eq 0 ]' in entrypoint
    assert "ensure_writable_directory" in entrypoint
    assert "SQLite directory" in entrypoint
    assert "Model cache directory" in entrypoint


def test_entrypoint_starts_with_writable_isolated_runtime_paths(
    tmp_path: Path,
) -> None:
    command_dir = tmp_path / "commands"
    command_dir.mkdir()
    invocation_log = tmp_path / "invocations.log"

    for command in ("python", "gunicorn"):
        executable = command_dir / command
        executable.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' '{command}' >> \"$DOCVAULT_TEST_INVOCATIONS\"\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)

    runtime_root = tmp_path / "runtime"
    environment = {
        **os.environ,
        "PATH": f"{command_dir}:{os.environ['PATH']}",
        "DOCVAULT_TEST_INVOCATIONS": str(invocation_log),
        "DOCVAULT_ENVIRONMENT": "production",
        "DOCVAULT_SECRET_KEY": "test-only-entrypoint-secret-material",
        "DOCVAULT_DATABASE_URL": f"sqlite:///{runtime_root / 'db' / 'docvault.db'}",
        "DOCVAULT_STORAGE_DIR": str(runtime_root / "storage"),
        "DOCVAULT_OKF_BUNDLE_DIR": str(runtime_root / "okf"),
        "HF_HOME": str(runtime_root / "cache"),
        "TRANSFORMERS_CACHE": str(runtime_root / "cache"),
        "HF_DATASETS_CACHE": str(runtime_root / "cache" / "datasets"),
        "SENTENCE_TRANSFORMERS_HOME": str(
            runtime_root / "cache" / "sentence-transformers"
        ),
        "TORCH_HOME": str(runtime_root / "cache" / "torch"),
        "XDG_CACHE_HOME": str(runtime_root / "cache" / "xdg"),
    }

    result = subprocess.run(
        [str(BACKEND_DIR / "entrypoint.sh")],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert invocation_log.read_text(encoding="utf-8").splitlines() == [
        "python",
        "python",
        "python",
        "gunicorn",
    ]
    for expected in (
        runtime_root / "db",
        runtime_root / "storage",
        runtime_root / "okf",
        runtime_root / "cache",
        runtime_root / "cache" / "datasets",
        runtime_root / "cache" / "sentence-transformers",
        runtime_root / "cache" / "torch",
        runtime_root / "cache" / "xdg",
    ):
        assert expected.is_dir()
        assert os.access(expected, os.W_OK | os.X_OK)
