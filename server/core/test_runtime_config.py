"""Production file configuration must fail closed without exporting secrets."""

import json
import os
from pathlib import Path
from collections.abc import Iterator

import pytest
from django.core.exceptions import ImproperlyConfigured

from core.runtime_config import RuntimeConfig


@pytest.fixture
def vault_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    RuntimeConfig._read.cache_clear()
    public = tmp_path / "config.json"
    secret = tmp_path / "runtime.json"
    public.write_text(json.dumps({"DJANGO_DEBUG": False, "POSTGRES_USER": "commerce"}))
    secret.write_text(
        json.dumps(
            {
                "SECRET_KEY": "test-only-key",
                "PASSWORD_PEPPER_KEYS": {"v1": "a" * 64},
                "POSTGRES_PASSWORD": "test-only-password",
                "REDIS_PASSWORD": "redis-test-only",
            }
        )
    )
    secret.chmod(0o640)
    monkeypatch.setenv("PROACE_CONFIG_MODE", "vault")
    monkeypatch.setenv("PROACE_CONFIG_FILE", str(public))
    monkeypatch.setenv("PROACE_SECRETS_FILE", str(secret))
    yield public, secret
    RuntimeConfig._read.cache_clear()


def test_local_mode_preserves_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROACE_CONFIG_MODE", "local")
    monkeypatch.setenv("SECRET_KEY", "local-value")
    assert RuntimeConfig().get("SECRET_KEY") == "local-value"


def test_vault_ignores_environment_and_never_exports(
    vault_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_KEY", "poisoned")
    before = dict(os.environ)
    config = RuntimeConfig()
    assert config.get("SECRET_KEY") == "test-only-key"
    assert config.get("DJANGO_DEBUG") == "false"
    assert json.loads(config.get("PASSWORD_PEPPER_KEYS"))["v1"] == "a" * 64
    assert config.get("UNKNOWN", "fallback") == "fallback"
    assert dict(os.environ) == before


@pytest.mark.parametrize("permissions", [0o644, 0o666, 0o750])
def test_unsafe_secret_permissions_fail(
    vault_files: tuple[Path, Path], permissions: int
) -> None:
    vault_files[1].chmod(permissions)
    with pytest.raises(ImproperlyConfigured, match="Runtime configuration"):
        RuntimeConfig().get("SECRET_KEY")


@pytest.mark.parametrize(
    "content", ["bad-test-secret", "[]", '{"bad-key": "secret"}', "x" * 1048577]
)
def test_invalid_secret_is_sanitized(
    vault_files: tuple[Path, Path], content: str
) -> None:
    vault_files[1].write_text(content)
    with pytest.raises(ImproperlyConfigured) as error:
        RuntimeConfig().get("SECRET_KEY")
    assert "bad-test-secret" not in str(error.value)
    assert str(vault_files[1]) not in str(error.value)


def test_symlink_rejected(vault_files: tuple[Path, Path]) -> None:
    secret = vault_files[1]
    target = secret.with_name("target.json")
    secret.rename(target)
    secret.symlink_to(target)
    with pytest.raises(ImproperlyConfigured):
        RuntimeConfig().get("SECRET_KEY")


@pytest.mark.parametrize(
    "public",
    [{"DJANGO_DEBUG": True}, {"DJANGO_DEBUG": False, "STRIPE_SECRET": "unsafe"}],
)
def test_public_configuration_guard(
    vault_files: tuple[Path, Path], public: dict[str, object]
) -> None:
    vault_files[0].write_text(json.dumps(public))
    with pytest.raises(ImproperlyConfigured):
        RuntimeConfig().get("SECRET_KEY")


def test_missing_required_secret_fails(vault_files: tuple[Path, Path]) -> None:
    vault_files[1].write_text("{}")
    with pytest.raises(ImproperlyConfigured, match="Required production secrets"):
        RuntimeConfig().get("SECRET_KEY")


def test_secrets_are_snapshot_until_restart(vault_files: tuple[Path, Path]) -> None:
    config = RuntimeConfig()
    assert config.get("SECRET_KEY") == "test-only-key"
    document = json.loads(vault_files[1].read_text())
    document["SECRET_KEY"] = "rotated-test-value"
    vault_files[1].write_text(json.dumps(document))
    assert config.get("SECRET_KEY") == "test-only-key"
    RuntimeConfig._read.cache_clear()
    assert config.get("SECRET_KEY") == "rotated-test-value"


def test_public_urls_cannot_contain_credentials(vault_files: tuple[Path, Path]) -> None:
    vault_files[0].write_text(
        json.dumps({"DJANGO_DEBUG": False, "CACHE_URL": "redis://user:secret@redis/0"})
    )
    with pytest.raises(ImproperlyConfigured, match="embedded connection credentials"):
        RuntimeConfig().get("CACHE_URL")


def test_secret_document_cannot_enable_debug(vault_files: tuple[Path, Path]) -> None:
    document = json.loads(vault_files[1].read_text())
    document["DJANGO_DEBUG"] = True
    vault_files[1].write_text(json.dumps(document))
    with pytest.raises(ImproperlyConfigured, match="debug disabled"):
        RuntimeConfig().get("DJANGO_DEBUG")


def test_redis_credentials_are_derived_and_escaped(
    vault_files: tuple[Path, Path],
) -> None:
    document = json.loads(vault_files[1].read_text())
    document["REDIS_PASSWORD"] = "test@pass:/?#"
    vault_files[1].write_text(json.dumps(document))
    config = RuntimeConfig()
    assert config.get("CACHE_URL") == "redis://:test%40pass%3A%2F%3F%23@redis:6379/0"
    assert config.get("CELERY_BROKER_URL").endswith("@redis:6379/1")
