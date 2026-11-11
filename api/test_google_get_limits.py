import io
from datetime import timedelta

from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import GoogleBrowserChallenge, User


@override_settings(GOOGLE_CLIENT_ID='get-limits.apps.googleusercontent.com')
class GoogleGetLimitsTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_repeated_config_and_page_reuse_nonce_without_extending_expiry(self):
        client = APIClient(enforce_csrf_checks=True)
        first = client.get(reverse('google-config'))
        nonce = first.json()['nonce']
        record = GoogleBrowserChallenge.objects.get()
        challenge_expiry = record.expires_at
        session = Session.objects.get()
        first_data, first_expiry = session.session_data, session.expire_date
        self.assertEqual(client.get(reverse('google-sign-in')).status_code, 200)
        again = client.get(reverse('google-config'))
        self.assertEqual(again.json()['nonce'], nonce)
        self.assertIn('no-store', again['Cache-Control'])
        record.refresh_from_db()
        session.refresh_from_db()
        self.assertEqual(record.expires_at, challenge_expiry)
        self.assertEqual(session.session_data, first_data)
        self.assertEqual(session.expire_date, first_expiry)
        self.assertLessEqual(session.expire_date, timezone.now() + timedelta(minutes=10))
        self.assertEqual(GoogleBrowserChallenge.objects.count(), 1)
        self.assertEqual(Session.objects.count(), 1)

    @override_settings(ACCOUNT_THROTTLE_RATES={'google_challenge_ip': '2/hour'})
    def test_cookie_free_requests_share_limit_before_session_creation(self):
        for route in ('google-config', 'google-sign-in'):
            self.assertEqual(APIClient().get(reverse(route)).status_code, 200)
        for route in ('google-config', 'google-sign-in'):
            denied = APIClient().get(reverse(route))
            self.assertEqual(denied.status_code, 429)
            self.assertGreater(int(denied['Retry-After']), 0)
            self.assertIn('no-store', denied['Cache-Control'])
            self.assertFalse(denied.cookies)
        self.assertEqual(Session.objects.count(), 2)
        self.assertEqual(GoogleBrowserChallenge.objects.count(), 2)

    @override_settings(ACCOUNT_THROTTLE_RATES={'google_challenge_ip': '1/hour'})
    def test_authenticated_session_does_not_bypass_get_limit(self):
        user = User.objects.create_user(email='get-limit@example.com')
        client = APIClient()
        client.force_login(user)
        before = Session.objects.get().expire_date
        self.assertEqual(client.get(reverse('google-config')).status_code, 200)
        after = Session.objects.get().expire_date
        self.assertGreater(after, timezone.now() + timedelta(days=1))
        self.assertGreaterEqual(after, before)
        self.assertEqual(client.get(reverse('google-sign-in')).status_code, 429)
        self.assertEqual(GoogleBrowserChallenge.objects.count(), 1)

    def test_expired_consumed_and_missing_challenges_are_replaced(self):
        client = APIClient()
        nonce = client.get(reverse('google-config')).json()['nonce']
        for operation in ('expire', 'consume', 'delete'):
            if operation == 'expire':
                GoogleBrowserChallenge.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
            elif operation == 'consume':
                GoogleBrowserChallenge.objects.update(consumed_at=timezone.now())
            else:
                GoogleBrowserChallenge.objects.all().delete()
            new_nonce = client.get(reverse('google-config')).json()['nonce']
            self.assertNotEqual(new_nonce, nonce)
            nonce = new_nonce
            self.assertEqual(GoogleBrowserChallenge.objects.count(), 1)
            self.assertIsNone(GoogleBrowserChallenge.objects.get().consumed_at)

    @override_settings(GOOGLE_CLIENT_ID='')
    def test_unconfigured_gets_do_not_create_sessions_or_challenges(self):
        for route in ('google-config', 'google-sign-in'):
            self.assertEqual(APIClient().get(reverse(route)).status_code, 503)
        self.assertFalse(Session.objects.exists())
        self.assertFalse(GoogleBrowserChallenge.objects.exists())

    def test_combined_cleanup_preserves_active_sessions_challenges_and_tokens(self):
        client = APIClient()
        client.get(reverse('google-config'))
        active_session = Session.objects.get().pk
        expired = timezone.now() - timedelta(seconds=1)
        Session.objects.create(session_key='expired', session_data='', expire_date=expired)
        GoogleBrowserChallenge.objects.create(session_digest='expired', nonce_digest='expired', expires_at=expired)
        user = User.objects.create_user(email='cleanup-test@example.com')
        active_token, expired_token = RefreshToken.for_user(user), RefreshToken.for_user(user)
        OutstandingToken.objects.filter(jti=expired_token['jti']).update(expires_at=expired)
        call_command('cleanup_auth_artifacts', stdout=io.StringIO())
        self.assertEqual(list(Session.objects.values_list('pk', flat=True)), [active_session])
        self.assertEqual(GoogleBrowserChallenge.objects.count(), 1)
        self.assertFalse(OutstandingToken.objects.filter(jti=expired_token['jti']).exists())
        self.assertTrue(OutstandingToken.objects.filter(jti=active_token['jti']).exists())
