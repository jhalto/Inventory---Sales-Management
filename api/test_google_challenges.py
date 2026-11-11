import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, connections
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase, APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from accounts.google_challenges import digest
from accounts.models import GoogleBrowserChallenge, GoogleIdentity, User
from accounts.throttles import DEFAULT_RATES


CLIENT_ID = 'test-client.apps.googleusercontent.com'


@override_settings(GOOGLE_CLIENT_ID=CLIENT_ID)
class GoogleBrowserChallengeTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)
        self.config = self.client.get(reverse('google-config')).json()
        self.claims = {'aud': CLIENT_ID, 'iss': 'https://accounts.google.com', 'sub': 'challenge-subject',
                       'email': 'owner@gmail.com', 'email_verified': True, 'nonce': self.config['nonce']}

    def post(self):
        return self.client.post(reverse('google-login'), {'credential': 'synthetic-id-token'},
            format='json', HTTP_X_CSRFTOKEN=self.config['csrf_token'])

    def test_challenge_hashes_are_session_bound_and_expire(self):
        challenge = GoogleBrowserChallenge.objects.get()
        self.assertNotEqual(challenge.nonce_digest, self.config['nonce'])
        self.assertEqual(challenge.nonce_digest, digest(self.config['nonce'], 'nonce'))
        self.assertEqual(challenge.session_digest, digest(self.client.session['google_sign_in_binding'], 'session'))
        self.assertIsNone(challenge.consumed_at)
        self.assertGreater(challenge.expires_at, timezone.now() + timedelta(minutes=9))
        self.assertLessEqual(challenge.expires_at, timezone.now() + timedelta(minutes=10))

    def test_expired_or_deleted_challenge_is_rejected_before_google_call(self):
        GoogleBrowserChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        with patch('accounts.google_service.id_token.verify_oauth2_token') as verify:
            self.assertEqual(self.post().status_code, 400)
            GoogleBrowserChallenge.objects.all().delete()
            self.assertEqual(self.post().status_code, 400)
        verify.assert_not_called()

    def test_nonce_cannot_be_transferred_to_another_session(self):
        other = APIClient(enforce_csrf_checks=True)
        config = other.get(reverse('google-config')).json()
        session = other.session
        session['google_sign_in_nonce'] = self.config['nonce']
        session.save()
        with patch('accounts.google_service.id_token.verify_oauth2_token') as verify:
            response = other.post(reverse('google-login'), {'credential': 'synthetic-id-token'},
                format='json', HTTP_X_CSRFTOKEN=config['csrf_token'])
        self.assertEqual(response.status_code, 400)
        verify.assert_not_called()

    def test_expiry_is_rechecked_after_google_verification(self):
        def verify(*args, **kwargs):
            GoogleBrowserChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
            return self.claims
        with patch('accounts.google_service.id_token.verify_oauth2_token', side_effect=verify):
            self.assertEqual(self.post().status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_replacement_is_rechecked_after_google_verification(self):
        def verify(*args, **kwargs):
            GoogleBrowserChallenge.objects.update(nonce_digest=digest('replacement', 'nonce'))
            return self.claims
        with patch('accounts.google_service.id_token.verify_oauth2_token', side_effect=verify):
            self.assertEqual(self.post().status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_token_creation_failure_rolls_back_consumption_and_account(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            with patch('api.views.accounts.google.RefreshToken.for_user', side_effect=IntegrityError('synthetic failure')):
                self.assertEqual(self.post().status_code, 409)
            self.assertIsNone(GoogleBrowserChallenge.objects.get().consumed_at)
            self.assertFalse(User.objects.exists())
            self.assertEqual(self.post().status_code, 200)
        self.assertIsNotNone(GoogleBrowserChallenge.objects.get().consumed_at)

    def test_cleanup_preserves_active_challenges(self):
        GoogleBrowserChallenge.objects.create(session_digest='expired', nonce_digest='unused', expires_at=timezone.now() - timedelta(seconds=1))
        call_command('cleanup_google_challenges', stdout=io.StringIO())
        self.assertEqual(GoogleBrowserChallenge.objects.count(), 1)
        self.assertTrue(GoogleBrowserChallenge.objects.filter(nonce_digest=digest(self.config['nonce'], 'nonce')).exists())

    def test_token_encoding_failure_also_rolls_back_consumption(self):
        class BrokenToken:
            access_token = 'synthetic access token'

            def __str__(self):
                raise ValueError('synthetic signing failure')

        self.client.raise_request_exception = False
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            with patch('api.views.accounts.google.RefreshToken.for_user', return_value=BrokenToken()):
                with self.assertLogs('django.request', level='ERROR'):
                    self.assertEqual(self.post().status_code, 500)
        self.assertIsNone(GoogleBrowserChallenge.objects.get().consumed_at)
        self.assertFalse(User.objects.exists())


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL challenge row locks')
@override_settings(GOOGLE_CLIENT_ID=CLIENT_ID, ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class ConcurrentGoogleChallengeTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        browser = APIClient(enforce_csrf_checks=True)
        self.config = browser.get(reverse('google-config')).json()
        self.cookies = '; '.join(key + '=' + value.value for key, value in browser.cookies.items())
        self.claims = {'aud': CLIENT_ID, 'iss': 'https://accounts.google.com', 'sub': 'concurrent-google',
                       'email': 'owner@gmail.com', 'email_verified': True, 'nonce': self.config['nonce']}

    def exchange_twice(self, route, bearer=None):
        barrier = Barrier(2)
        def verify(*args, **kwargs):
            barrier.wait(timeout=15)
            return self.claims
        def exchange(index):
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                client = APIClient(enforce_csrf_checks=True)
                client.cookies.load(self.cookies)
                if bearer:
                    client.credentials(HTTP_AUTHORIZATION=f'Bearer {bearer}')
                response = client.post(reverse(route), {'credential': 'synthetic-id-token'},
                    format='json', HTTP_X_CSRFTOKEN=self.config['csrf_token'])
                return response.status_code, 'access_token' in response.data.get('data', {}), pid
            finally:
                connections.close_all()
        with patch('accounts.google_service.id_token.verify_oauth2_token', side_effect=verify):
            with patch('api.views.accounts.google.RefreshToken.for_user', wraps=RefreshToken.for_user) as mint:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(exchange, range(2)))
                mint.assert_called_once()
        self.assertEqual(sorted(status for status, _, _ in results), [200, 400])
        self.assertEqual(sum(has_token for _, has_token, _ in results), 1)
        self.assertEqual(len({pid for _, _, pid in results}), 2)
        self.assertIsNotNone(GoogleBrowserChallenge.objects.get().consumed_at)

    def test_existing_identity_exchanges_once(self):
        user = User.objects.create_user(email='owner@gmail.com', email_verified=True)
        GoogleIdentity.objects.create(user=user, subject=self.claims['sub'])
        self.exchange_twice('google-login')
        self.assertEqual(User.objects.count(), 1)

    def test_new_identity_creates_one_account_and_exchanges_once(self):
        self.exchange_twice('google-login')
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(GoogleIdentity.objects.count(), 1)

    def test_authenticated_link_exchanges_once(self):
        user = User.objects.create_user(email='owner@gmail.com', email_verified=True)
        self.exchange_twice('google-link', str(AccessToken.for_user(user)))
        self.assertEqual(GoogleIdentity.objects.count(), 1)
