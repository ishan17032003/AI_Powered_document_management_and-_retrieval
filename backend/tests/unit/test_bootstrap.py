"""Fail-closed tests for the production startup configuration boundary."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEVELOPMENT_DEFAULT = "dev" + "-only-" + "change" + "-me"
PRODUCTION_PLACEHOLDER = "change" + "-me-in-production"
VALID_SIGNING_MATERIAL = "0123456789abcdef-ABCDEF-9876543210-safe"


@pytest.fixture
def configuration_modules(
    settings_env: dict[str, str],
) -> tuple[ModuleType, ModuleType]:
    config = importlib.import_module("app.config")
    bootstrap = importlib.import_module("app.bootstrap")
    return config, bootstrap


def _production_settings(
    config: ModuleType,
    **overrides: object,
):
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": VALID_SIGNING_MATERIAL,
        "database_url": "postgresql+psycopg://docvault@database/docvault",
        "storage_dir": Path("/srv/docvault/storage"),
        "okf_bundle_dir": Path("/srv/docvault/okf"),
        "cors_origins": ["https://docvault.example.test"],
        "debug": False,
        "enable_demo_seed": False,
        "access_token_minutes": 30,
        "llm_provider": "none",
        "use_qdrant": False,
    }
    values.update(overrides)
    return config.Settings(_env_file=None, **values)


def _issue_codes(bootstrap: ModuleType, configured) -> set[str]:
    return {
        issue.code for issue in bootstrap.production_configuration_issues(configured)
    }


def test_safe_production_profile_passes(
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules
    configured = _production_settings(config)

    assert bootstrap.production_configuration_issues(configured) == []
    assert bootstrap.validate_startup(configured) is configured


@pytest.mark.parametrize("environment", ["development", "test"])
def test_nonproduction_modes_do_not_claim_production_validation(
    environment: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules
    configured = config.Settings(
        _env_file=None,
        environment=environment,
        secret_key=DEVELOPMENT_DEFAULT,
        database_url="sqlite:///local.db",
        storage_dir=Path("relative-storage"),
        cors_origins=["*"],
        debug=True,
        enable_demo_seed=True,
        access_token_minutes=480,
    )

    assert bootstrap.production_configuration_issues(configured) == []


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_every_runtime_requires_injected_signing_material(
    environment: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules
    configured = config.Settings(
        _env_file=None,
        environment=environment,
        secret_key="",
    )

    with pytest.raises(bootstrap.StartupConfigurationError) as raised:
        bootstrap.validate_startup(configured)

    assert "CFG_SECRET_REQUIRED" in {issue.code for issue in raised.value.issues}


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_every_runtime_rejects_removed_automatic_provider_routing(
    environment: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules
    configured = (
        _production_settings(config, llm_provider="auto")
        if environment == "production"
        else config.Settings(
            _env_file=None,
            environment=environment,
            secret_key=VALID_SIGNING_MATERIAL,
            llm_provider="auto",
        )
    )

    issues = bootstrap.startup_configuration_issues(configured)
    assert "CFG_PROVIDER_REMOVED" in {issue.code for issue in issues}

    with pytest.raises(bootstrap.StartupConfigurationError) as raised:
        bootstrap.validate_startup(configured)

    assert "CFG_PROVIDER_REMOVED" in {issue.code for issue in raised.value.issues}


@pytest.mark.parametrize("environment", ["development", "test", "production"])
def test_every_runtime_rejects_invalid_provider_resource_limits(
    environment: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules
    configured = (
        _production_settings(config, rag_provider_max_concurrency=0)
        if environment == "production"
        else config.Settings(
            _env_file=None,
            environment=environment,
            secret_key=VALID_SIGNING_MATERIAL,
            rag_provider_max_concurrency=0,
        )
    )

    with pytest.raises(bootstrap.StartupConfigurationError) as raised:
        bootstrap.validate_startup(configured)

    assert "CFG_RAG_LIMIT" in {issue.code for issue in raised.value.issues}


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"rag_provider_connect_timeout_seconds": 0}, "CFG_RAG_LIMIT"),
        ({"rag_provider_read_timeout_seconds": 121}, "CFG_RAG_LIMIT"),
        ({"rag_provider_total_timeout_seconds": float("inf")}, "CFG_RAG_LIMIT"),
        ({"rag_provider_max_output_tokens": 0}, "CFG_RAG_LIMIT"),
        ({"rag_max_context_bytes": 1023}, "CFG_RAG_LIMIT"),
        ({"rag_provider_max_concurrency": 17}, "CFG_RAG_LIMIT"),
        (
            {
                "rag_provider_connect_timeout_seconds": 4,
                "rag_provider_total_timeout_seconds": 3,
            },
            "CFG_RAG_TIMEOUT_ORDER",
        ),
        (
            {
                "rag_provider_read_timeout_seconds": 4,
                "rag_provider_total_timeout_seconds": 3,
            },
            "CFG_RAG_TIMEOUT_ORDER",
        ),
    ],
)
def test_production_rejects_unbounded_or_incoherent_provider_resources(
    overrides: dict[str, object],
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, **overrides),
    )


def test_unmodified_code_defaults_are_rejected_for_production(
    configuration_modules: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    settings_env: dict[str, str],
) -> None:
    config, bootstrap = configuration_modules
    for name in settings_env:
        monkeypatch.delenv(name, raising=False)
    configured = config.Settings(_env_file=None, environment="production")

    codes = _issue_codes(bootstrap, configured)

    assert {
        "CFG_SECRET_LENGTH",
        "CFG_DATABASE_PROFILE",
        "CFG_ROOT_SOURCE",
        "CFG_CORS_TRANSPORT",
        "CFG_TOKEN_LIFETIME",
    } <= codes
    assert configured.secret_key == ""
    assert configured.llm_provider == "none"
    assert configured.vllm_url == ""


@pytest.mark.parametrize(
    ("secret_key", "expected_code"),
    [
        (PRODUCTION_PLACEHOLDER, "CFG_SECRET_DEFAULT"),
        ("short", "CFG_SECRET_LENGTH"),
        ("a" * 64, "CFG_SECRET_VARIETY"),
    ],
)
def test_production_rejects_unsafe_signing_secrets(
    secret_key: str,
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, secret_key=secret_key),
    )


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"database_url": "not-a-url"}, "CFG_DATABASE_URL"),
        ({"database_url": "sqlite:////data/docvault.db"}, "CFG_DATABASE_PROFILE"),
        (
            {"llm_provider": "vllm", "vllm_url": "http://public.example.test/v1"},
            "CFG_URL_TRANSPORT",
        ),
        (
            {
                "llm_provider": "vllm",
                "vllm_url": "https://user:password@provider.example.test/v1",
            },
            "CFG_URL_CREDENTIALS",
        ),
        (
            {
                "llm_provider": "ollama",
                "ollama_url": "https://provider.example.test/api?token=value",
            },
            "CFG_URL_COMPONENTS",
        ),
        ({"llm_provider": "invented"}, "CFG_PROVIDER_UNKNOWN"),
    ],
)
def test_production_rejects_invalid_database_and_provider_urls(
    overrides: dict[str, object],
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, **overrides),
    )


def test_networked_provider_requires_opt_in_and_exact_allowed_host(
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    denied = _production_settings(
        config,
        llm_provider="vllm",
        vllm_url="https://provider.example.test/v1",
    )
    assert {"CFG_EGRESS_OPT_IN", "CFG_EGRESS_HOST_DENIED"} <= _issue_codes(
        bootstrap, denied
    )

    allowed = _production_settings(
        config,
        llm_provider="vllm",
        vllm_url="https://provider.example.test/v1",
        allow_external_llm=True,
        llm_allowed_hosts=["provider.example.test"],
    )
    assert bootstrap.production_configuration_issues(allowed) == []


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"llm_provider": "auto"}, "CFG_PROVIDER_REMOVED"),
        ({"llm_provider": "VLLM"}, "CFG_PROVIDER_CANONICAL"),
        (
            {"llm_allowed_hosts": ["*.example.test"]},
            "CFG_EGRESS_HOST_INVALID",
        ),
        (
            {"llm_allowed_hosts": ["provider.test", "PROVIDER.TEST"]},
            "CFG_EGRESS_HOST_DUPLICATE",
        ),
    ],
)
def test_production_rejects_ambiguous_provider_policy(
    overrides: dict[str, object],
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, **overrides),
    )


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"storage_dir": Path("relative")}, "CFG_ROOT_ABSOLUTE"),
        ({"storage_dir": Path("/")}, "CFG_ROOT_BROAD"),
        ({"storage_dir": Path("/tmp/docvault")}, "CFG_ROOT_TEMPORARY"),
        ({"storage_dir": BACKEND_DIR / "storage"}, "CFG_ROOT_SOURCE"),
        (
            {
                "storage_dir": Path("/srv/docvault"),
                "okf_bundle_dir": Path("/srv/docvault/okf"),
            },
            "CFG_ROOT_OVERLAP",
        ),
    ],
)
def test_production_rejects_unsafe_or_overlapping_roots(
    overrides: dict[str, object],
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, **overrides),
    )


def test_production_accepts_explicit_bounded_folder_import_policy(
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules
    configured = _production_settings(
        config,
        folder_import_enabled=True,
        folder_import_roots=[Path("/srv/docvault-import/inbox")],
    )

    assert bootstrap.production_configuration_issues(configured) == []


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"folder_import_enabled": True}, "CFG_IMPORT_ROOTS_EMPTY"),
        (
            {"folder_import_roots": [Path("relative-import-root")]},
            "CFG_ROOT_ABSOLUTE",
        ),
        (
            {"folder_import_roots": [Path("/srv/docvault/storage/inbox")]},
            "CFG_IMPORT_ROOT_OVERLAP",
        ),
        (
            {
                "folder_import_roots": [
                    Path("/srv/docvault-import"),
                    Path("/srv/docvault-import"),
                ]
            },
            "CFG_IMPORT_ROOT_DUPLICATE",
        ),
        ({"folder_import_max_visited_entries": 0}, "CFG_IMPORT_LIMIT"),
        ({"folder_import_max_files": 10_001}, "CFG_IMPORT_LIMIT"),
        ({"folder_import_max_total_mb": 0}, "CFG_IMPORT_LIMIT"),
        ({"folder_import_max_depth": 101}, "CFG_IMPORT_LIMIT"),
        ({"folder_import_max_seconds": 0}, "CFG_IMPORT_LIMIT"),
    ],
)
def test_production_rejects_unsafe_folder_import_policy(
    overrides: dict[str, object],
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, **overrides),
    )


def test_production_rejects_a_symlink_data_root(
    configuration_modules: tuple[ModuleType, ModuleType],
    tmp_path: Path,
) -> None:
    config, bootstrap = configuration_modules
    symlink = tmp_path / "storage-link"
    symlink.symlink_to("/srv/docvault/real-storage")

    assert "CFG_ROOT_SYMLINK" in _issue_codes(
        bootstrap,
        _production_settings(config, storage_dir=symlink),
    )


@pytest.mark.parametrize(
    ("origins", "expected_code"),
    [
        ([], "CFG_CORS_EMPTY"),
        (["*"], "CFG_CORS_WILDCARD"),
        (["http://docvault.example.test"], "CFG_CORS_TRANSPORT"),
        (["https://docvault.example.test/path"], "CFG_CORS_EXACT"),
        (
            ["https://docvault.example.test", "https://docvault.example.test"],
            "CFG_CORS_DUPLICATE",
        ),
    ],
)
def test_production_rejects_unsafe_cors(
    origins: list[str],
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, cors_origins=origins),
    )


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"debug": True}, "CFG_DEBUG_ENABLED"),
        ({"enable_demo_seed": True}, "CFG_DEMO_SEED_ENABLED"),
        ({"access_token_minutes": 4}, "CFG_TOKEN_LIFETIME"),
        ({"access_token_minutes": 61}, "CFG_TOKEN_LIFETIME"),
    ],
)
def test_production_rejects_debug_demo_and_unsafe_token_lifetime(
    overrides: dict[str, object],
    expected_code: str,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert expected_code in _issue_codes(
        bootstrap,
        _production_settings(config, **overrides),
    )


@pytest.mark.parametrize("token_minutes", [5, 60])
def test_production_accepts_token_lifetime_boundaries(
    token_minutes: int,
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules

    assert "CFG_TOKEN_LIFETIME" not in _issue_codes(
        bootstrap,
        _production_settings(config, access_token_minutes=token_minutes),
    )


def test_failure_text_never_contains_configured_values(
    configuration_modules: tuple[ModuleType, ModuleType],
) -> None:
    config, bootstrap = configuration_modules
    configured = _production_settings(
        config,
        secret_key="canary-secret-value",
        database_url="sqlite:////tmp/canary-database.db",
        storage_dir=Path("/tmp/canary-storage"),
        cors_origins=["http://canary-browser.example.test/private"],
        llm_provider="vllm",
        vllm_url="http://canary-user:canary-password@public.example.test/v1",
    )

    with pytest.raises(bootstrap.StartupConfigurationError) as raised:
        bootstrap.validate_startup(configured)

    rendered = str(raised.value)
    assert "startup configuration rejected" in rendered
    for sensitive_value in (
        "canary-secret-value",
        "canary-database",
        "canary-storage",
        "canary-browser",
        "canary-user",
        "canary-password",
    ):
        assert sensitive_value not in rendered


def test_cli_returns_ex_config_with_redacted_message(
    configuration_modules: tuple[ModuleType, ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, bootstrap = configuration_modules
    monkeypatch.setattr(
        bootstrap,
        "settings",
        _production_settings(config, secret_key="cli-canary"),
    )

    assert bootstrap.main() == 78
    error = capsys.readouterr().err
    assert "CFG_SECRET_LENGTH" in error
    assert "cli-canary" not in error


def test_runtime_directories_are_created_only_after_validation(
    configuration_modules: tuple[ModuleType, ModuleType],
    tmp_path: Path,
) -> None:
    config, bootstrap = configuration_modules
    development_storage = tmp_path / "development" / "storage"
    development_okf = tmp_path / "development" / "okf"
    development = config.Settings(
        _env_file=None,
        environment="development",
        secret_key=DEVELOPMENT_DEFAULT,
        storage_dir=development_storage,
        okf_bundle_dir=development_okf,
    )

    bootstrap.prepare_runtime_directories(development)

    assert development_storage.is_dir()
    assert development_okf.is_dir()

    rejected_storage = tmp_path / "production" / "storage"
    rejected_okf = tmp_path / "production" / "okf"
    production = _production_settings(
        config,
        storage_dir=rejected_storage,
        okf_bundle_dir=rejected_okf,
    )
    with pytest.raises(bootstrap.StartupConfigurationError):
        bootstrap.prepare_runtime_directories(production)
    assert not rejected_storage.exists()
    assert not rejected_okf.exists()


def test_database_import_rejects_unsafe_production_before_data_side_effects(
    tmp_path: Path,
) -> None:
    database = tmp_path / "must-not-exist.db"
    storage = tmp_path / "must-not-exist-storage"
    okf = tmp_path / "must-not-exist-okf"
    environment = os.environ.copy()
    environment.update(
        {
            "DOCVAULT_ENVIRONMENT": "production",
            "DOCVAULT_SECRET_KEY": "subprocess-canary",
            "DOCVAULT_DATABASE_URL": f"sqlite:///{database}",
            "DOCVAULT_STORAGE_DIR": str(storage),
            "DOCVAULT_OKF_BUNDLE_DIR": str(okf),
            "DOCVAULT_CORS_ORIGINS": '["http://subprocess.example.test"]',
            "DOCVAULT_ACCESS_TOKEN_MINUTES": "480",
            "DOCVAULT_LLM_PROVIDER": "none",
            "DOCVAULT_USE_QDRANT": "false",
            "DOCVAULT_DEBUG": "false",
            "DOCVAULT_ENABLE_DEMO_SEED": "false",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import app.database"],
        cwd=BACKEND_DIR,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "startup configuration rejected" in result.stderr
    assert "subprocess-canary" not in result.stderr
    assert "subprocess.example.test" not in result.stderr
    assert not database.exists()
    assert not storage.exists()
    assert not okf.exists()


def test_disabled_demo_seed_does_not_create_database_or_print_credentials(
    tmp_path: Path,
) -> None:
    database = tmp_path / "must-not-be-seeded.db"
    environment = os.environ.copy()
    environment.update(
        {
            "DOCVAULT_ENVIRONMENT": "development",
            "DOCVAULT_ENABLE_DEMO_SEED": "false",
            "DOCVAULT_SECRET_KEY": "seed-skip-test-signing-material-not-production",
            "DOCVAULT_DATABASE_URL": f"sqlite:///{database}",
            "DOCVAULT_STORAGE_DIR": str(tmp_path / "storage"),
            "DOCVAULT_OKF_BUNDLE_DIR": str(tmp_path / "okf"),
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "app.seed"],
        cwd=BACKEND_DIR,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "Demo seed disabled" in result.stdout
    assert "admin" + "123" not in result.stdout
    assert not database.exists()
