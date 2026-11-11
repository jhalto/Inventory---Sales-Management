import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from google.auth import exceptions
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import GoogleIdentity, User


CLIENT_ID = 'test-client.apps.googleusercontent.com'


@override_settings(GOOGLE_CLIENT_ID=CLIENT_ID)
class GoogleMobileTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(enforce_csrf_checks=True)
        self.claims = {
            'sub': 'mobile-google-subject', 'email': 'Mobile@gmail.com',
            'email_verified': True, 'iss': 'https://accounts.google.com', 'aud': CLIENT_ID,
        }

    def post(self, route='google-mobile-login', **headers):
        return self.client.post(reverse(route), {'credential': 'native-google-id-token'}, format='json', **headers)

    def test_native_login_without_browser_state_creates_verified_account(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims) as verify:
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(verify.call_args.kwargs['audience'], CLIENT_ID)
        self.assertIn('access_token', response.data['data'])
        self.assertIn('refresh_token', response.data['data'])
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn('native-google-id-token', str(response.data))
        self.assertFalse(response.cookies)
        user = User.objects.get()
        self.assertEqual(user.email, 'mobile@gmail.com')
        self.assertTrue(user.email_verified)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(GoogleIdentity.objects.get().user, user)

    def test_returning_native_login_needs_no_nonce_reinitialization(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            first = self.post()
            second = self.post()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.data['data']['user']['id'], second.data['data']['user']['id'])
        self.assertEqual(User.objects.count(), 1)

    def test_existing_account_requires_link_and_valid_app_bearer(self):
        user = User.objects.create_user(email='mobile@gmail.com', email_verified=True, password='ExistingTestPass!8247')
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            self.assertEqual(self.post().status_code, 409)
            self.assertEqual(self.post('google-mobile-link').status_code, 401)
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
            self.assertEqual(self.post('google-mobile-link').status_code, 200)
        self.assertEqual(GoogleIdentity.objects.get().user, user)
        user.refresh_from_db()
        self.assertTrue(user.check_password('ExistingTestPass!8247'))

    def test_link_rejects_wrong_email_and_unverified_local_account(self):
        user = User.objects.create_user(email='different@gmail.com', email_verified=True)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            self.assertEqual(self.post('google-mobile-link').status_code, 403)
            user.email = 'mobile@gmail.com'
            user.email_verified = False
            user.save()
            self.assertEqual(self.post('google-mobile-link').status_code, 403)
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_native_route_rejects_browser_origins_and_ambient_cookies(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token') as verify:
            self.assertEqual(self.post(HTTP_ORIGIN='https://app.example.test').status_code, 403)
            self.assertEqual(self.post(HTTP_COOKIE='sessionid=browser-session').status_code, 403)
        verify.assert_not_called()

    def test_browser_endpoint_still_requires_csrf_and_nonce(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token') as verify:
            self.assertEqual(self.post('google-login').status_code, 403)
        verify.assert_not_called()

    def test_native_tokens_require_expected_issuer_audience_and_verified_email(self):
        for changes in ({'aud': 'other-app'}, {'iss': 'https://attacker.test'}, {'email_verified': False}, {'sub': ''}):
            with self.subTest(changes=changes), patch('accounts.google_service.id_token.verify_oauth2_token', return_value={**self.claims, **changes}):
                self.assertEqual(self.post().status_code, 401)
        self.assertFalse(User.objects.exists())

    def test_invalid_request_data_returns_400_without_google_call(self):
        for body in ([], None, 123, {}, {'credential': []}, {'credential': 123}, {'credential': None}):
            with self.subTest(body=body), patch('accounts.google_service.id_token.verify_oauth2_token') as verify:
                response = self.client.generic('POST', reverse('google-mobile-login'), json.dumps(body), content_type='application/json')
                self.assertEqual(response.status_code, 400)
                verify.assert_not_called()

    def test_google_outage_and_missing_config_return_503(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', side_effect=exceptions.TransportError('offline')):
            self.assertEqual(self.post().status_code, 503)
        with override_settings(GOOGLE_CLIENT_ID=''):
            self.assertEqual(self.post().status_code, 503)

    @override_settings(ACCOUNT_THROTTLE_RATES={'google_ip': '1/hour'})
    def test_web_and_mobile_share_ip_budget(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', side_effect=ValueError('invalid token')):
            self.assertEqual(self.post().status_code, 401)
            self.assertEqual(self.post('google-login').status_code, 429)

    @override_settings(ALLOWED_HOSTS=['10.0.2.2'])
    def test_android_emulator_host_can_call_native_endpoint(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            self.assertEqual(self.post(HTTP_HOST='10.0.2.2').status_code, 200)
