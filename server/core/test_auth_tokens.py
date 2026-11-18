import pytest

from core.auth_tokens import credential_version_token_generator
from core.models import User


@pytest.mark.django_db
def test_password_reset_token_is_invalidated_when_credentials_change() -> None:
    user = User.objects.create(email='reset-version@example.com', is_active=True)
    user.set_password('ResetTokenPassword2026!')
    user.save()
    token = credential_version_token_generator.make_token(user)

    assert credential_version_token_generator.check_token(user, token)

    user.credential_version += 1
    user.save(update_fields=['credential_version'])
    assert not credential_version_token_generator.check_token(user, token)


@pytest.mark.django_db
def test_password_reset_token_expires_after_ten_minutes() -> None:
    from datetime import datetime, timedelta
    from unittest.mock import patch

    user = User.objects.create_user(email='reset-expiry@example.test', password='OriginalPassword2026!')
    issued = datetime(2026, 1, 1, 12, 0)
    generator = credential_version_token_generator
    with patch.object(generator, '_now', return_value=issued):
        token = generator.make_token(user)
    with patch.object(generator, '_now', return_value=issued + timedelta(seconds=599)):
        assert generator.check_token(user, token)
    with patch.object(generator, '_now', return_value=issued + timedelta(seconds=601)):
        assert not generator.check_token(user, token)
