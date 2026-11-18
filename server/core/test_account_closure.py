import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from core.models import User
from notification.models import NotificationDelivery
from payment.models import Order
from store.models import Store

pytestmark = pytest.mark.django_db


def test_closure_revokes_old_tokens_preserves_financial_snapshot_and_hides_owned_stores():
    user = User.objects.create_user(email='closure-customer@example.test', password='fixture', first_name='Fixture')
    store = Store.objects.create(user=user, name='Closure store', status=Store.STATUS_ACTIVE, verified_at=timezone.now())
    order = Order.objects.create(user=user, ordered=True, total=0, tax_snapshot={'amount':'0.00','jurisdiction':'Original'}, buyer_email=user.email)
    snapshot = (order.tax_snapshot, order.buyer_email, order.total)
    token = RefreshToken.for_user(user)
    version = user.credential_version
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')
    result = client.delete('/api/v1/me/')
    assert result.status_code == 200, result.data
    user.refresh_from_db();store.refresh_from_db();order.refresh_from_db()
    assert not user.is_active and user.credential_version == version+1
    assert store.status == Store.STATUS_BLOCKED
    assert (order.tax_snapshot,order.buyer_email,order.total) == snapshot
    assert client.get('/api/v1/me/').status_code == 401
    anonymous = APIClient()
    assert anonymous.post('/api/v1/auth/refresh/',{'refresh':str(token)},format='json').status_code == 401
    assert NotificationDelivery.objects.filter(recipient_email=user.email).exists()
    assert 'deactivated' in result.data['detail']
    assert User.objects.filter(pk=user.pk).exists()  # Closure does not falsely claim deletion.


@pytest.mark.parametrize('role',[{'is_staff':True},{'is_business_admin':True},{'is_superuser':True}])
def test_operator_self_closure_is_rejected(role):
    user=User.objects.create_user(email='closure-operator@example.test',password='fixture',**role)
    client=APIClient();client.force_authenticate(user)
    assert client.delete('/api/v1/me/').status_code==403
    user.refresh_from_db()
    assert user.is_active
