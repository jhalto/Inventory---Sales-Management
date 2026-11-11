from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import User


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL shared-cache connections')
@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache', 'LOCATION': 'otp_rate_limits'}},
    ACCOUNT_THROTTLE_RATES={'login_email': '1/hour'},
)
class SharedCacheThrottleTransactionTests(TransactionTestCase):
    def test_authenticated_client_on_new_connection_cannot_reset_recipient_limit(self):
        call_command('createcachetable', verbosity=0)
        cache.clear()
        user = User.objects.create_user(email='caller@example.com')
        token = str(AccessToken.for_user(user))

        def attempt(authenticated):
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                client = APIClient(REMOTE_ADDR='192.0.2.2' if authenticated else '192.0.2.1')
                if authenticated:
                    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
                response = client.post(reverse('login'), {
                    'email': 'TARGET@example.com' if authenticated else 'target@example.com',
                    'password': 'wrong',
                })
                return response.status_code, response.get('Retry-After'), pid
            finally:
                connections.close_all()

        with patch('api.views.accounts.views.authenticate', return_value=None):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(attempt, False).result(timeout=20)
                second = pool.submit(attempt, True).result(timeout=20)
        self.assertEqual(first[0], 401)
        self.assertEqual(second[0], 429)
        self.assertIsNotNone(second[1])
        self.assertNotEqual(first[2], second[2])
