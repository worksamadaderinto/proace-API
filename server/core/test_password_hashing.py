"""Real-cost password compatibility and pepper isolation regressions."""

from typing import Any
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, identify_hasher, make_password
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from core.password_pepper import parse_pepper_keys

KEYS = {"v1": "11" * 32, "v2": "22" * 32}
HASHERS = [
    "core.hashers.PepperedArgon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.Argon2PasswordHasher",
]


@pytest.fixture(autouse=True)
def password_settings() -> Any:
    with override_settings(
        PASSWORD_HASHERS=HASHERS, PASSWORD_PEPPER_ID="v1", PASSWORD_PEPPER_KEYS=KEYS
    ):
        yield


def test_argon2id_is_salted_peppered_and_fits_user_column() -> None:
    password = "Pässword\x00🔑" * 20
    encoded = make_password(password)
    assert encoded.startswith("argon2p$v1$argon2id$")
    assert len(encoded) <= 128
    assert check_password(password, encoded)
    assert not check_password("wrong", encoded)
    assert make_password(password) != encoded
    summary = identify_hasher(encoded).safe_summary(encoded)
    assert KEYS["v1"] not in str(summary)
    with override_settings(PASSWORD_PEPPER_KEYS={"v1": KEYS["v2"]}):
        assert not check_password(password, encoded)


@pytest.mark.django_db
def test_legacy_login_upgrades_without_resetting_password() -> None:
    user = get_user_model().objects.create(email="legacy-hash@example.test")
    user.password = make_password("Existing-password!", hasher="pbkdf2_sha256")
    user.save(update_fields=["password"])
    original = user.password
    assert not user.check_password("incorrect")
    user.refresh_from_db()
    assert user.password == original
    assert user.check_password("Existing-password!")
    user.refresh_from_db()
    assert user.password.startswith("argon2p$v1$argon2id$")
    assert user.check_password("Existing-password!")


@pytest.mark.parametrize("hasher", ["pbkdf2_sha1", "argon2"])
def test_other_legacy_hashes_remain_verifiable(hasher: str) -> None:
    assert check_password("Original!", make_password("Original!", hasher=hasher))


def test_pepper_rotation_rehashes_only_after_correct_password() -> None:
    encoded = make_password("Rotate-password!")
    upgraded: list[str] = []
    with override_settings(PASSWORD_PEPPER_ID="v2"):
        assert not check_password("wrong", encoded, setter=upgraded.append)
        assert not upgraded
        assert check_password(
            "Rotate-password!",
            encoded,
            setter=lambda raw: upgraded.append(make_password(raw)),
        )
        assert upgraded[0].startswith("argon2p$v2$")
        assert check_password("Rotate-password!", upgraded[0])


def test_missing_key_and_malformed_hash_fail_closed() -> None:
    encoded = make_password("Original!")
    with override_settings(PASSWORD_PEPPER_KEYS={"v2": KEYS["v2"]}):
        assert not check_password("Original!", encoded)
    assert not check_password("Original!", "argon2p$missing$invalid")
    assert not check_password("Original!", "argon2p$v1$invalid")
    assert not check_password("Original!", make_password(None))


@pytest.mark.parametrize(
    "raw,current",
    [
        ("", "v1"),
        ("[]", "v1"),
        ('{"v1":"short"}', "v1"),
        ('{"v1":"' + "11" * 32 + '"}', "missing"),
        ('{"toolongid":"' + "11" * 32 + '"}', "toolongid"),
    ],
)
def test_invalid_configuration_fails_without_secret_disclosure(
    raw: str, current: str
) -> None:
    with pytest.raises(ImproperlyConfigured) as error:
        parse_pepper_keys(raw, current)
    assert "short" not in str(error.value)


def test_keyring_validation_accepts_rotation_keys() -> None:
    import json

    assert parse_pepper_keys(json.dumps(KEYS), "v2") == KEYS


def test_longest_key_id_fits_existing_password_column() -> None:
    with override_settings(
        PASSWORD_PEPPER_ID="version2", PASSWORD_PEPPER_KEYS={"version2": KEYS["v2"]}
    ):
        encoded = make_password("Column-length!")
        assert len(encoded) <= get_user_model()._meta.get_field("password").max_length
        assert check_password("Column-length!", encoded)


@pytest.mark.parametrize(
    "raw", ["", "[]", '{"v1":"short"}', '{"v2":"' + "11" * 32 + '"}']
)
def test_deployment_rejects_invalid_keyring_before_writing_env(
    raw: str,
    tmp_path: Any,
    monkeypatch: Any,
) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "deploy/hostgator/generate_env.py"
    spec = importlib.util.spec_from_file_location("pepper_deploy_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "generate_env.py"))
    monkeypatch.setenv("PASSWORD_PEPPER_ID", "v1")
    monkeypatch.setenv("PASSWORD_PEPPER_KEYS", raw)
    with pytest.raises(SystemExit):
        module.main()
    assert not (tmp_path / ".env").exists()


def test_production_environment_writer_is_retired(tmp_path: Any, monkeypatch: Any) -> None:
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "deploy/hostgator/generate_env.py"
    spec = importlib.util.spec_from_file_location("retired_env_writer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "__file__", str(tmp_path / "generate_env.py"))
    with pytest.raises(SystemExit, match="retired"):
        module.main()
    assert not (tmp_path / ".env").exists()
