from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import User
from accounts.serializers import UserSerializers
from accounts.throttles import DEFAULT_RATES


class EmailIdentityTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_manager_and_model_save_normalize_email(self):
        user = User.objects.create_user(email='  MixedCase@Example.COM  ')
        self.assertEqual(user.email, 'mixedcase@example.com')
        user.email = ' Changed@Example.COM '
        user.save(update_fields=['first_name'])
        user.refresh_from_db()
        self.assertEqual(user.email, 'changed@example.com')

    def test_database_rejects_case_and_space_variants_without_model_save(self):
        User.objects.create_user(email='person@example.com')
        other = User.objects.create_user(email='other@example.com')
        for email in ('PERSON@example.com', ' person@example.com '):
            with self.subTest(email=email), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('UPDATE accounts_user SET email = %s WHERE id = %s', [email, other.pk])

    def test_multiple_phone_only_accounts_allow_null_email(self):
        User.objects.create_user(phone='+8801712345678')
        User.objects.create_user(phone='+8801812345678')
        self.assertEqual(User.objects.filter(email__isnull=True).count(), 2)

    def test_manager_created_mixed_case_account_can_log_in(self):
        User.objects.create_user(email='MixedCase@example.com', password='ExampleTestPass!8247', email_verified=True)
        response = APIClient().post(reverse('login'), {
            'email': 'MIXEDCASE@example.com', 'password': 'ExampleTestPass!8247',
        })
        self.assertEqual(response.status_code, 200)

    def test_unrelated_integrity_error_is_not_reported_as_duplicate_email(self):
        with patch.object(UserSerializers, 'save', side_effect=IntegrityError('unrelated constraint')):
            with self.assertRaises(IntegrityError):
                APIClient().post(reverse('register'), {
                    'email': 'new@example.com', 'password': 'ExampleTestPass!8247',
                })


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL concurrency semantics')
@override_settings(ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class ConcurrentRegistrationTests(TransactionTestCase):
    def test_competing_registration_returns_one_created_and_one_email_error(self):
        cache.clear()
        barrier = Barrier(2)
        original = UserSerializers.is_valid

        def validate(serializer, *args, **kwargs):
            result = original(serializer, *args, **kwargs)
            if not result:
                raise AssertionError('Both concurrent requests must pass the initial email check.')
            barrier.wait(timeout=15)
            return result

        def register(email):
            try:
                response = APIClient().post(reverse('register'), {
                    'email': email, 'password': 'ConcurrentTestPass!8247',
                })
                return response.status_code, response.data
            finally:
                connections.close_all()

        with patch.object(UserSerializers, 'is_valid', validate):
            with patch('api.views.accounts.views.send_verification_email') as send:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(register, ('Race@example.com', 'race@example.com')))
                send.assert_called_once()
        self.assertEqual(sorted(status for status, _ in results), [201, 400])
        failure = next(data for status, data in results if status == 400)
        self.assertEqual(failure, {
            'status': False, 'message': 'Invalid data',
            'errors': {'email': ['This email is already registered.']},
        })
        self.assertEqual(User.objects.filter(email='race@example.com').count(), 1)
