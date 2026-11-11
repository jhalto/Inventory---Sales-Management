from unittest.mock import patch
import re

from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core import mail
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from accounts.serializers import UserSerializers


class RegistrationPasswordValidationTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_email_and_local_part_are_rejected_as_passwords(self):
        for password in ('distinctiveperson@example.com', 'distinctiveperson'):
            with self.subTest(password=password):
                serializer = UserSerializers(data={
                    'email': ' DistinctivePerson@Example.COM ', 'password': password,
                })
                self.assertFalse(serializer.is_valid())
                self.assertIn('password', serializer.errors)
                self.assertIn('similar', str(serializer.errors['password']).lower())
        self.assertFalse(User.objects.exists())

    def test_validator_receives_unsaved_user_with_normalized_email(self):
        serializer = UserSerializers(data={
            'email': ' NewPerson@Example.COM ', 'password': 'UnrelatedStrongPass!8247',
        })
        with patch('accounts.serializers.validate_password', wraps=validate_password) as validate:
            self.assertTrue(serializer.is_valid(), serializer.errors)
        user = validate.call_args.kwargs['user']
        self.assertIsNone(user.pk)
        self.assertTrue(user._state.adding)
        self.assertEqual(user.email, 'newperson@example.com')
        self.assertFalse(User.objects.exists())

    def test_duplicate_email_skips_password_validators(self):
        User.objects.create_user(email='existing@example.com')
        with patch('accounts.serializers.validate_password') as validate:
            serializer = UserSerializers(data={'email': 'EXISTING@example.com', 'password': 'short'})
            self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors, {'email': ['This email is already registered.']})
        validate.assert_not_called()

    def test_invalid_email_skips_password_validators(self):
        with patch('accounts.serializers.validate_password') as validate:
            serializer = UserSerializers(data={'email': 'invalid', 'password': 'short'})
            self.assertFalse(serializer.is_valid())
        self.assertIn('email', serializer.errors)
        self.assertNotIn('password', serializer.errors)
        validate.assert_not_called()

    def test_api_returns_password_error_without_creating_user_or_sending_email(self):
        with patch('api.views.accounts.views.send_verification_email') as send:
            response = APIClient().post(reverse('register'), {
                'email': 'distinctiveperson@example.com',
                'password': 'distinctiveperson@example.com',
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.data['errors'])
        self.assertFalse(User.objects.exists())
        send.assert_not_called()

    @override_settings(MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}})
    def test_registration_verification_and_login_preserve_exact_password(self):
        passwords = (
            '  UnrelatedStrongPass!8247',
            'UnrelatedStrongPass!8247  ',
            '  UnrelatedStrongPass!8247  ',
            'Unrelated Strong Pass!8247',
        )
        for index, password in enumerate(passwords):
            with self.subTest(case=index):
                cache.clear()
                client = APIClient()
                email = f'whitespace{index}@example.com'
                with patch('accounts.serializers.validate_password', wraps=validate_password) as validate:
                    response = client.post(reverse('register'), {'email': email, 'password': password})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(validate.call_args.args[0], password)
                user = User.objects.get(email=email)
                self.assertTrue(user.check_password(password))
                otp = re.search(r'\b[0-9]{6}\b', mail.outbox[-1].body).group()
                self.assertEqual(client.post(reverse('verify-email'), {'email': email, 'otp': otp}).status_code, 200)
                self.assertEqual(client.post(reverse('login'), {'email': email, 'password': password}).status_code, 200)
                if password != password.strip():
                    response = client.post(reverse('login'), {'email': email, 'password': password.strip()})
                    self.assertEqual(response.status_code, 401)

    def test_whitespace_only_password_remains_invalid(self):
        for password in ('        ', '\t' * 8):
            with self.subTest(kind='whitespace only'):
                serializer = UserSerializers(data={'email': 'new@example.com', 'password': password})
                self.assertFalse(serializer.is_valid())
                self.assertIn('password', serializer.errors)
