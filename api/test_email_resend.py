import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import User


@override_settings(ACCOUNT_THROTTLE_RATES={
    'email_resend_ip': None, 'email_send': None, 'email_cooldown': None,
})
class EmailResendValidationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def post_json(self, body):
        return self.client.generic('POST', reverse('resend-verification-email'),
            json.dumps(body), content_type='application/json')

    def test_non_object_bodies_return_400_without_delivery(self):
        for body in ([], [{'email': 'person@example.com'}], None, 123, 1.5, True, 'person@example.com'):
            with self.subTest(body=body), patch('api.views.accounts.views.send_verification_email') as send:
                self.assertEqual(self.post_json(body).status_code, 400)
                send.assert_not_called()

    def test_missing_invalid_and_wrong_email_types_return_400_without_delivery(self):
        bodies = [{}] + [{'email': value} for value in (
            None, [], {}, 123, 1.5, True, '', '   ', 'invalid', 'person@',
        )]
        for body in bodies:
            with self.subTest(body=body), patch('api.views.accounts.views.send_verification_email') as send:
                response = self.post_json(body)
                self.assertEqual(response.status_code, 400)
                self.assertIn('email', response.data)
                send.assert_not_called()

    def test_malformed_json_returns_400_without_delivery(self):
        with patch('api.views.accounts.views.send_verification_email') as send:
            response = self.client.generic('POST', reverse('resend-verification-email'),
                '{"email":', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        send.assert_not_called()

    def test_valid_email_is_normalized_and_sent_once(self):
        user = User.objects.create_user(email='person@example.com')
        with patch('api.views.accounts.views.send_verification_email') as send:
            response = self.post_json({'email': ' PERSON@Example.COM '})
        self.assertEqual(response.status_code, 200)
        send.assert_called_once_with(user)

    def test_valid_unknown_and_verified_emails_do_not_send(self):
        User.objects.create_user(email='verified@example.com', email_verified=True)
        for email in ('unknown@example.com', 'VERIFIED@example.com'):
            with self.subTest(email=email), patch('api.views.accounts.views.send_verification_email') as send:
                self.assertEqual(self.post_json({'email': email}).status_code, 200)
                send.assert_not_called()
