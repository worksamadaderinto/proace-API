from datetime import timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.core import signing
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from core.models import User
from core.onboarding import activate_staff, invite_staff, resend_staff_verification
from notification.models import NotificationDelivery
from store.models import Store, StoreTeamMember
from store.services import batch_store_user_roles, get_store_user_role, link_pending_store_team_invitations

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize('age, valid', [(6 * 86400, True), (7 * 86400 + 1, False)])
def test_staff_invitation_seven_day_expiry(age: int, valid: bool) -> None:
    user = User.objects.create_staffuser(email='staff-expiry@example.test', password='fixture-only', is_active=False, staff_email_verified=False)
    issued = timezone.now().timestamp()
    with patch('django.core.signing.time.time', return_value=issued):
        token = signing.dumps({'user': user.pk, 'version': user.credential_version}, salt='staff-activation')
    with patch('django.core.signing.time.time', return_value=issued + age):
        if valid:
            activate_staff(token)
            with pytest.raises(ValidationError):
                activate_staff(token)
        else:
            with pytest.raises(ValidationError):
                activate_staff(token)
    user.refresh_from_db()
    assert user.is_active is valid


def test_staff_resend_invalidates_old_invitation_and_email_states_seven_days() -> None:
    user = invite_staff({'email': 'staff-resend-expiry@example.test', 'first_name': 'Fixture', 'last_name': 'Staff'})
    assert '7 days' in NotificationDelivery.objects.filter(recipient=user).latest('pk').body
    token = signing.dumps({'user': user.pk, 'version': user.credential_version}, salt='staff-activation')
    resend_staff_verification(user)
    with pytest.raises(ValidationError):
        activate_staff(token)
    assert settings.PASSWORD_RESET_TIMEOUT == 600


@pytest.mark.parametrize('age, valid', [(6 * 86400, True), (7 * 86400, False), (8 * 86400, False)])
@pytest.mark.parametrize('legacy', [True, False])
def test_store_invitation_expiry_blocks_automatic_membership(age: int, valid: bool, legacy: bool) -> None:
    now = timezone.now()
    owner = User.objects.create_user(email='invite-owner@example.test', password='fixture-only')
    user = User.objects.create_user(email='invite-recipient@example.test', password='fixture-only')
    store = Store.objects.create(user=owner, name='Fixture store')
    sent = now - timedelta(seconds=age)
    member = StoreTeamMember.objects.create(store=store, invited_email=user.email, status='pending', invitation_sent_at=None if legacy else sent)
    StoreTeamMember.objects.filter(pk=member.pk).update(created=sent)
    with patch('store.services.timezone.now', return_value=now):
        assert get_store_user_role(store, user) is None
        assert batch_store_user_roles([store], user) == {store.pk: ''}
        assert link_pending_store_team_invitations(user) == int(valid)
        assert get_store_user_role(store, user) == (member.role if valid else None)
        assert link_pending_store_team_invitations(user) == 0
    member.refresh_from_db()
    assert member.status == ('active' if valid else 'pending')
    assert member.user_id == (user.pk if valid else None)
