import time
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from django.core.cache import cache
from django.test import override_settings, SimpleTestCase
from django.urls import reverse
from google.auth import crypt, jwt, exceptions
from rest_framework.test import APITestCase, APIClient
from rest_framework_simplejwt.tokens import AccessToken

from accounts.google_service import InvalidGoogleToken, verify_google_token, verify_google_mobile_token
from accounts.models import GoogleIdentity, User


CLIENT_ID = 'test-client.apps.googleusercontent.com'


@override_settings(GOOGLE_CLIENT_ID=CLIENT_ID)
class GoogleSignInTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)
        config = self.client.get(reverse('google-config'))
        self.challenge = config.json()
        self.claims = {
            'sub': 'google-subject-123', 'email': 'Owner@gmail.com', 'email_verified': True,
            'nonce': self.challenge['nonce'], 'iss': 'https://accounts.google.com', 'aud': CLIENT_ID,
        }

    def post(self, route='google-login', **extra):
        return self.client.post(reverse(route), {'credential': 'google-id-token'}, format='json',
            HTTP_X_CSRFTOKEN=self.challenge['csrf_token'], **extra)

    def test_new_google_account_is_verified_and_has_no_password(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        user = User.objects.get()
        self.assertEqual(user.email, 'owner@gmail.com')
        self.assertTrue(user.email_verified)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.role, User.Role.CUSTOMER)
        self.assertEqual(GoogleIdentity.objects.get().subject, self.claims['sub'])
        self.assertIn('access_token', response.data['data'])
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertNotIn('google-id-token', str(response.data))
        self.assertNotIn('google_sign_in_nonce', self.client.session)

    def test_existing_email_requires_explicit_authenticated_link(self):
        user = User.objects.create_user(email='owner@gmail.com', password='ExistingTestPass!8247', email_verified=True)
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            response = self.post()
            self.assertEqual(response.status_code, 409)
            self.assertFalse(GoogleIdentity.objects.exists())
            self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
            response = self.post('google-link')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(GoogleIdentity.objects.get().user, user)
        user.refresh_from_db()
        self.assertTrue(user.check_password('ExistingTestPass!8247'))

    def test_unverified_existing_email_cannot_be_linked_or_hijacked(self):
        user = User.objects.create_user(email='owner@gmail.com')
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            self.assertEqual(self.post('google-link').status_code, 403)
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_returning_identity_uses_subject_and_blocks_inactive_accounts(self):
        user = User.objects.create_user(email='old@gmail.com', email_verified=True)
        GoogleIdentity.objects.create(user=user, subject=self.claims['sub'])
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['data']['user']['id'], user.pk)
        self.assertEqual(User.objects.count(), 1)
        self.challenge = self.client.get(reverse('google-config')).json()
        self.claims['nonce'] = self.challenge['nonce']
        User.objects.filter(pk=user.pk).update(is_active=False)
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims):
            self.assertEqual(self.post().status_code, 403)

    def test_invalid_claims_are_rejected(self):
        for overrides in (
            {'nonce': 'wrong'}, {'aud': 'wrong'}, {'iss': 'https://attacker.test'},
            {'email_verified': False}, {'email_verified': 'true'}, {'sub': ''},
            {'email': 'not-an-email'}, {'email': 'person@thirdparty.test'},
        ):
            with self.subTest(overrides=overrides), patch('accounts.google_service.id_token.verify_oauth2_token', return_value={**self.claims, **overrides}):
                self.assertEqual(self.post().status_code, 401)
        self.assertFalse(User.objects.exists())

    def test_workspace_address_is_supported(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value={**self.claims, 'email': 'person@company.test', 'hd': 'company.test'}):
            self.assertEqual(self.post().status_code, 200)

    def test_csrf_nonce_and_authenticated_link_are_required(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token') as verify:
            response = self.client.post(reverse('google-login'), {'credential': 'google-id-token'}, format='json')
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.post('google-link').status_code, 401)
            session = self.client.session
            session.pop('google_sign_in_nonce')
            session.save()
            self.assertEqual(self.post().status_code, 400)
        verify.assert_not_called()

    def test_success_consumes_nonce_and_prevents_replay(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', return_value=self.claims) as verify:
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.post().status_code, 400)
        verify.assert_called_once()

    def test_provider_failure_and_missing_config_return_503(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', side_effect=exceptions.TransportError('unavailable')):
            self.assertEqual(self.post().status_code, 503)
        with override_settings(GOOGLE_CLIENT_ID=''):
            self.assertEqual(self.post().status_code, 503)
            self.assertEqual(self.client.get(reverse('google-sign-in')).status_code, 503)

    def test_button_page_does_not_embed_secrets(self):
        response = self.client.get(reverse('google-sign-in'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'https://accounts.google.com/gsi/client')
        self.assertContains(response, CLIENT_ID)
        self.assertNotContains(response, 'refresh_token: "')
        self.assertIn('no-store', response['Cache-Control'])

    @override_settings(ACCOUNT_THROTTLE_RATES={'google_ip': '1/hour'})
    def test_google_endpoint_is_throttled(self):
        with patch('accounts.google_service.id_token.verify_oauth2_token', side_effect=ValueError('bad token')):
            self.assertEqual(self.post().status_code, 401)
            self.assertEqual(self.post().status_code, 429)


@override_settings(GOOGLE_CLIENT_ID=CLIENT_ID)
class GoogleSignatureTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.signer = crypt.RSASigner.from_string(private, key_id='test-key')
        cls.public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def token(self, **overrides):
        return jwt.encode(self.signer, {
            'sub': 'test-subject', 'email': 'person@gmail.com', 'email_verified': True,
            'nonce': 'nonce', 'iss': 'https://accounts.google.com', 'aud': CLIENT_ID,
            'iat': int(time.time()) - 5, 'exp': int(time.time()) + 300, **overrides,
        }).decode()

    def test_real_signature_audience_and_expiry_validation(self):
        with patch('google.oauth2.id_token._fetch_certs', return_value={'test-key': self.public}):
            self.assertEqual(verify_google_token(self.token(), 'nonce')['subject'], 'test-subject')
            for token in (self.token(aud='another-client'), self.token(exp=int(time.time()) - 60), self.token(iss='https://attacker.test')):
                with self.subTest(reason='invalid signed claims'), self.assertRaises(InvalidGoogleToken):
                    verify_google_token(token, 'nonce')

    def test_untrusted_signature_is_rejected(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = other.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        with patch('google.oauth2.id_token._fetch_certs', return_value={'test-key': public}):
            with self.assertRaises(InvalidGoogleToken):
                verify_google_token(self.token(), 'nonce')

    def test_native_verification_without_nonce_still_checks_signature_audience_and_expiry(self):
        with patch('google.oauth2.id_token._fetch_certs', return_value={'test-key': self.public}):
            self.assertEqual(verify_google_mobile_token(self.token(nonce=None))['subject'], 'test-subject')
            for token in (self.token(aud='other-app', nonce=None), self.token(exp=int(time.time()) - 60, nonce=None)):
                with self.subTest(reason='invalid native signed token'), self.assertRaises(InvalidGoogleToken):
                    verify_google_mobile_token(token)
            with self.assertRaises(InvalidGoogleToken):
                verify_google_token(self.token(nonce=None), 'nonce')
