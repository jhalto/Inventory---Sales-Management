from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import GoogleIdentity, PhoneOTP, User
from accounts.otp_service import generate_otp


@override_settings(GOOGLE_CLIENT_ID='contract.apps.googleusercontent.com')
class AuthenticationContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email='contract@gmail.com', phone='+8801712345678',
            email_verified=True, phone_verified=True, password='ContractTestPass!8247',
            role=User.Role.STAFF,
        )
        GoogleIdentity.objects.create(user=self.user, subject='contract-subject')

    def assert_contract(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(set(body), {'status', 'message', 'data'})
        self.assertIs(body['status'], True)
        self.assertIsInstance(body['message'], str)
        self.assertEqual(set(body['data']), {'user', 'access_token', 'refresh_token'})
        self.assertEqual(body['data']['user'], {
            'id': self.user.pk, 'email': self.user.email, 'phone': self.user.phone, 'role': 'staff',
        })
        for name in ('access_token', 'refresh_token'):
            self.assertIsInstance(body['data'][name], str)
            self.assertTrue(body['data'][name])
        authentication = JWTAuthentication()
        access = authentication.get_validated_token(body['data']['access_token'])
        self.assertEqual(authentication.get_user(access), self.user)
        self.assertEqual(RefreshToken(body['data']['refresh_token'])['user_id'], str(self.user.pk))
        self.assertEqual(response['Cache-Control'], 'no-store')
        return body['data']

    def test_all_token_issuers_share_one_json_contract(self):
        response = APIClient().post(reverse('login'), {
            'email': self.user.email, 'password': 'ContractTestPass!8247',
        }, format='json')
        self.assert_contract(response)
        code = generate_otp(self.user.phone)
        response = APIClient().post(reverse('verify-otp'), {'phone': self.user.phone, 'otp': code}, format='json')
        self.assert_contract(response)

        identity = {'subject': 'contract-subject', 'email': self.user.email}
        with patch('api.views.accounts.google.verify_google_token', return_value=identity), \
                patch('api.views.accounts.google.verify_google_mobile_token', return_value=identity):
            for route in ('google-login', 'google-link', 'google-mobile-login', 'google-mobile-link'):
                with self.subTest(route=route):
                    client = APIClient(enforce_csrf_checks=True)
                    headers = {}
                    if 'mobile' not in route:
                        config = client.get(reverse('google-config')).json()
                        headers['HTTP_X_CSRFTOKEN'] = config['csrf_token']
                    if route.endswith('link'):
                        client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.user).access_token}')
                    response = client.post(reverse(route), {'credential': 'valid-google-credential'}, format='json', **headers)
                    tokens = self.assert_contract(response)
        refreshed = APIClient().post(reverse('token-refresh'), {'refresh': tokens['refresh_token']}, format='json')
        self.assert_contract(refreshed)

    def test_phone_token_encoding_failure_preserves_unconsumed_challenge(self):
        class BrokenToken:
            access_token = 'synthetic-access'

            def __str__(self):
                raise ValueError('Synthetic token encoding failure')

        code = generate_otp(self.user.phone)
        client = APIClient()
        with patch('api.views.accounts.views.RefreshToken.for_user', return_value=BrokenToken()):
            with self.assertRaisesMessage(ValueError, 'Synthetic token encoding failure'):
                client.post(reverse('verify-otp'), {'phone': self.user.phone, 'otp': code}, format='json')
        self.assertTrue(PhoneOTP.objects.filter(phone=self.user.phone).exists())
        response = client.post(reverse('verify-otp'), {'phone': self.user.phone, 'otp': code}, format='json')
        self.assert_contract(response)
