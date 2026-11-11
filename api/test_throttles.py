from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.cache import caches
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITransactionTestCase, APIClient
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import User, ThrottleBucket
from datetime import timedelta
from django.utils import timezone
from accounts.throttles import DEFAULT_RATES


@override_settings(
    MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}},
)
class AccountThrottleTests(APITransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(email='caller@example.com')

    def client_for(self, address, authenticated=False):
        client = APIClient(REMOTE_ADDR=address)
        if authenticated:
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(self.user)}')
        return client

    def test_all_endpoints_limit_anonymous_and_authenticated_callers(self):
        rates = {scope: '1/hour' for scope in DEFAULT_RATES}
        with override_settings(ACCOUNT_THROTTLE_RATES=rates):
            for authenticated in (False, True):
                for route in ('register', 'login', 'verify-email', 'resend-verification-email', 'send-otp', 'verify-otp'):
                    with self.subTest(route=route, authenticated=authenticated):
                        cache.clear()
                        ThrottleBucket.objects.all().delete()
                        client = self.client_for('192.0.2.1', authenticated)
                        self.assertEqual(client.post(reverse(route), {}).status_code, 400)
                        response = client.post(reverse(route), {})
                        self.assertEqual(response.status_code, 429)
                        self.assertIn('Retry-After', response)

    @override_settings(ACCOUNT_THROTTLE_RATES={'login_email': '2/hour'})
    def test_login_account_limit_survives_changing_ip_and_authentication(self):
        with patch('api.views.accounts.views.authenticate', return_value=None):
            for i in range(3):
                client = self.client_for(f'192.0.2.{i + 1}', authenticated=i == 2)
                email = 'TARGET@example.com' if i == 2 else 'target@example.com'
                response = client.post(reverse('login'), {'email': email, 'password': 'wrong'})
                self.assertEqual(response.status_code, 429 if i == 2 else 401)

    def test_registration_and_resend_share_recipient_cooldown(self):
        now = timezone.now()
        with patch('accounts.throttles.AccountThrottle.timer', return_value=1000), patch('accounts.delivery_limits.database_now', return_value=now):
            response = self.client_for('192.0.2.1').post(reverse('register'), {
                'email': 'new@example.com', 'password': 'StrongTestPassword!8246',
            })
            self.assertEqual(response.status_code, 201)
            response = self.client_for('192.0.2.2', True).post(
                reverse('resend-verification-email'), {'email': 'NEW@example.com'},
            )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(len(mail.outbox), 1)
        with patch('accounts.throttles.AccountThrottle.timer', return_value=1061), patch('accounts.delivery_limits.database_now', return_value=now + timedelta(seconds=61)):
            response = self.client_for('192.0.2.2', True).post(
                reverse('resend-verification-email'), {'email': 'new@example.com'},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(mail.outbox), 2)

    @override_settings(ACCOUNT_THROTTLE_RATES={'email_send': '2/hour', 'email_cooldown': None})
    def test_email_hourly_limit_survives_separate_clients(self):
        for i in range(3):
            response = self.client_for(f'192.0.2.{i + 1}', i == 2).post(
                reverse('resend-verification-email'), {'email': 'unknown@example.com'},
            )
            self.assertEqual(response.status_code, 429 if i == 2 else 200)

    def test_phone_cooldown_normalizes_recipient_across_clients(self):
        with patch('accounts.outbox.send_sms') as send:
            first = self.client_for('192.0.2.1').post(reverse('send-otp'), {'phone': '01712345678'})
            second = self.client_for('192.0.2.2', True).post(reverse('send-otp'), {'phone': '+8801712345678'})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        send.assert_called_once()

    def test_verification_limits_follow_recipient_across_clients(self):
        rates = {'email_verify_recipient': '1/hour', 'phone_verify_recipient': '1/hour'}
        with override_settings(ACCOUNT_THROTTLE_RATES=rates):
            for route, identity in (
                ('verify-email', {'email': 'unknown@example.com'}),
                ('verify-otp', {'phone': '+8801712345678'}),
            ):
                with self.subTest(route=route):
                    first = self.client_for('192.0.2.1').post(reverse(route), {**identity, 'otp': '123456'})
                    second = self.client_for('192.0.2.2', True).post(reverse(route), {**identity, 'otp': '123456'})
                    self.assertEqual(first.status_code, 400)
                    self.assertEqual(second.status_code, 429)

    @override_settings(CACHES={'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'test_account_throttle_cache',
    }})
    def test_database_limiter_survives_cache_reconnections(self):
        call_command('createcachetable', verbosity=0)
        first = self.client_for('192.0.2.1').post(
            reverse('resend-verification-email'), {'email': 'unknown@example.com'},
        )
        self.assertEqual(first.status_code, 200)
        caches.close_all()
        second = self.client_for('192.0.2.2', True).post(
            reverse('resend-verification-email'), {'email': 'UNKNOWN@example.com'},
        )
        self.assertEqual(second.status_code, 429)
