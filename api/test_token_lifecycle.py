from concurrent.futures import ThreadPoolExecutor
import json
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.serializers import TokenRefreshSerializer

from accounts.models import User


class TokenLifecycleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email='tokens@example.com', password='TokenTestPass!8247')
        self.refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()

    def post(self, name, token=None):
        return self.client.post(reverse(name), {'refresh': str(token or self.refresh)}, format='json')

    def test_rotation_returns_new_pair_and_rejects_reuse(self):
        response = self.post('token-refresh')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data), {'status', 'message', 'data'})
        self.assertEqual(set(response.data['data']), {'user', 'access_token', 'refresh_token'})
        self.assertNotEqual(response.data['data']['refresh_token'], str(self.refresh))
        self.assertEqual(JWTAuthentication().get_user(
            JWTAuthentication().get_validated_token(response.data['data']['access_token'])), self.user)
        self.assertEqual(self.post('token-refresh').status_code, 401)
        self.assertEqual(self.post('token-refresh', response.data['data']['refresh_token']).status_code, 200)

    def test_logout_revokes_only_submitted_refresh_and_retains_access_until_expiry(self):
        other = RefreshToken.for_user(self.user)
        access = self.refresh.access_token
        self.assertEqual(self.post('logout').status_code, 204)
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=self.refresh['jti']).exists())
        self.assertEqual(self.post('token-refresh').status_code, 401)
        self.assertEqual(self.post('logout').status_code, 401)
        self.assertEqual(self.post('token-refresh', other).status_code, 200)
        self.assertEqual(JWTAuthentication().get_user(access), self.user)

    def test_password_change_invalidates_access_and_refresh(self):
        access = self.refresh.access_token
        self.user.set_password('ChangedTokenPass!8247')
        self.user.save(update_fields=['password'])
        with self.assertRaises(AuthenticationFailed):
            JWTAuthentication().get_user(access)
        self.assertEqual(self.post('token-refresh').status_code, 401)
        self.assertEqual(self.post('token-refresh', RefreshToken.for_user(self.user)).status_code, 200)

    def test_inactive_or_deleted_account_cannot_refresh(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertEqual(self.post('token-refresh').status_code, 401)
        self.user.delete()
        self.assertEqual(self.post('token-refresh').status_code, 401)

    def test_legacy_tokens_without_password_claim_require_new_signin(self):
        del self.refresh[api_settings.REVOKE_TOKEN_CLAIM]
        self.assertEqual(self.post('token-refresh').status_code, 401)
        with self.assertRaises(AuthenticationFailed):
            JWTAuthentication().get_user(self.refresh.access_token)

    def test_expired_malformed_and_wrong_type_tokens_are_rejected(self):
        expired = RefreshToken.for_user(self.user)
        expired.set_exp(lifetime=timedelta(seconds=-1))
        for token in ('not-a-token', str(expired), str(self.refresh.access_token)):
            for name in ('token-refresh', 'logout'):
                with self.subTest(name=name, token=token):
                    self.assertEqual(self.post(name, token).status_code, 401)

    def test_invalid_bodies_return_400(self):
        for body in ([], None, 1, {}, {'refresh': None}, {'refresh': 123}, {'refresh': ''}):
            for name in ('token-refresh', 'logout'):
                with self.subTest(name=name, body=body):
                    response = self.client.generic('POST', reverse(name), json.dumps(body), content_type='application/json')
                    self.assertEqual(response.status_code, 400, response.data)

    def test_expired_authorization_header_does_not_block_refresh(self):
        self.client.credentials(HTTP_AUTHORIZATION='Bearer expired-access-token')
        self.assertEqual(self.post('token-refresh').status_code, 200)

    def test_failed_exchange_rolls_back_rotation(self):
        original = TokenRefreshSerializer.validate

        def fail_after_rotation(serializer, attrs):
            original(serializer, attrs)
            raise RuntimeError('Simulated exchange failure')

        with patch.object(TokenRefreshSerializer, 'validate', fail_after_rotation):
            with self.assertRaisesMessage(RuntimeError, 'Simulated exchange failure'):
                self.post('token-refresh')
        self.assertFalse(BlacklistedToken.objects.filter(token__jti=self.refresh['jti']).exists())
        self.assertEqual(self.post('token-refresh').status_code, 200)

    @override_settings(ACCOUNT_THROTTLE_RATES={'token_ip': '1/hour'})
    def test_endpoints_share_ip_throttle(self):
        self.assertEqual(self.post('token-refresh').status_code, 200)
        self.assertEqual(self.post('logout').status_code, 429)


class ConcurrentTokenTests(TransactionTestCase):
    def test_parallel_refresh_consumes_token_once(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locking.')
        cache.clear()
        user = User.objects.create_user(email='parallel-tokens@example.com')
        token = str(RefreshToken.for_user(user))
        barrier = Barrier(2)

        def refresh():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = APIClient().post(reverse('token-refresh'), {'refresh': token}, format='json')
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: refresh(), range(2)))
        self.assertEqual(sorted(results), [200, 401])
