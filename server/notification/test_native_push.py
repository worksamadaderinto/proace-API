from types import SimpleNamespace
from unittest.mock import patch
from firebase_admin.messaging import MulticastMessage

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core.models import User
from notification.models import NotificationDelivery, PushDevice
from notification.tasks import _deliver_push
from notification.views import create_notification


@override_settings(FCM_ENABLED=True, ROOT_URLCONF='storefront.urls')
class NativePushTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(email='native-push@test.local', password='test')

    def test_native_display_and_browser_data_payloads_are_isolated(self) -> None:
        notice = create_notification(
            recipient=self.user,
            verb='order shipped',
            description='Your order shipped.',
            data={'email': False, 'push': False},
        )
        delivery = NotificationDelivery.objects.create(
            channel=NotificationDelivery.CHANNEL_PUSH,
            notification=notice,
            recipient=self.user,
        )
        PushDevice.objects.create(user=self.user, token='browser-fixture', platform='web')
        PushDevice.objects.create(user=self.user, token='ios-fixture', platform='ios')
        other = User.objects.create_user(email='other-push@test.local', password='test')
        PushDevice.objects.create(user=other, token='other-fixture', platform='android')

        def response(message: MulticastMessage, **kwargs: object) -> SimpleNamespace:
            count = len(message.tokens)
            return SimpleNamespace(
                success_count=count,
                responses=[
                    SimpleNamespace(success=True, exception=None)
                    for _ in range(count)
                ],
            )

        with (
            patch('notification.tasks._firebase_app', return_value=object()),
            patch(
                'firebase_admin.messaging.send_each_for_multicast',
                side_effect=response,
            ) as send,
        ):
            result = _deliver_push(delivery)
        self.assertEqual(result['sent'], 2)
        self.assertEqual(send.call_count, 2)
        browser, native = [call.args[0] for call in send.call_args_list]
        self.assertEqual(browser.tokens, ['browser-fixture'])
        self.assertIsNone(browser.notification)
        self.assertEqual(native.tokens, ['ios-fixture'])
        self.assertEqual(native.notification.title, 'Order shipped')
        self.assertEqual(native.data['recipient_id'], str(self.user.pk))

    def test_device_registration_validates_platform_and_preserves_web_default(self) -> None:
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertEqual(
            client.post(
                '/api/v1/notifications/devices/',
                {'token': 'native-fixture', 'platform': 'ios'},
                format='json',
            ).status_code,
            200,
        )
        self.assertEqual(PushDevice.objects.get(token='native-fixture').platform, 'ios')
        self.assertEqual(
            client.post(
                '/api/v1/notifications/devices/',
                {'token': 'web-fixture'},
                format='json',
            ).status_code,
            200,
        )
        self.assertEqual(PushDevice.objects.get(token='web-fixture').platform, 'web')
        self.assertEqual(
            client.post(
                '/api/v1/notifications/devices/',
                {'token': 'invalid-fixture', 'platform': 'unknown'},
                format='json',
            ).status_code,
            400,
        )
