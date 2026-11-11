import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import User


@override_settings(ACCOUNT_THROTTLE_RATES={'login_ip': None, 'login_email': None})
class LoginValidationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_json(self, body):
        return self.client.generic(
            'POST', reverse('login'), json.dumps(body), content_type='application/json',
        )

    def test_non_object_bodies_return_400_without_authentication(self):
        for body in ([], ['email@example.com'], 123, 1.5, True, None, 'text'):
            with self.subTest(body=body), patch('api.views.accounts.views.authenticate') as authenticate:
                response = self.post_json(body)
                self.assertEqual(response.status_code, 400)
                self.assertIn('errors', response.data)
                authenticate.assert_not_called()

    def test_wrong_field_types_return_400_without_authentication(self):
        for field in ('email', 'password'):
            for value in ([], {}, 123, 1.5, True, None):
                with self.subTest(field=field, value=value):
                    body = {'email': 'person@example.com', 'password': 'ExamplePass!8247', field: value}
                    with patch('api.views.accounts.views.authenticate') as authenticate:
                        response = self.post_json(body)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(field, response.data['errors'])
                    authenticate.assert_not_called()

    def test_missing_blank_and_invalid_email_fields_return_400(self):
        bodies = (
            {}, {'email': 'person@example.com'}, {'password': 'ExamplePass!8247'},
            {'email': '', 'password': 'ExamplePass!8247'},
            {'email': 'not-an-email', 'password': 'ExamplePass!8247'},
            {'email': 'person@example.com', 'password': ''},
        )
        for body in bodies:
            with self.subTest(body=body), patch('api.views.accounts.views.authenticate') as authenticate:
                self.assertEqual(self.post_json(body).status_code, 400)
                authenticate.assert_not_called()

    def test_valid_input_normalizes_email_and_preserves_password_whitespace(self):
        with patch('api.views.accounts.views.authenticate', return_value=None) as authenticate:
            response = self.post_json({'email': ' Person@Example.COM ', 'password': ' pass word '})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(authenticate.call_args.kwargs, {
            'email': 'person@example.com', 'password': ' pass word ',
        })

    def test_verified_account_still_receives_tokens(self):
        User.objects.create_user(email='person@example.com', password='ExamplePass!8247', email_verified=True)
        response = self.post_json({'email': 'PERSON@example.com', 'password': 'ExamplePass!8247'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('access_token', response.data['data'])
        self.assertIn('refresh_token', response.data['data'])
