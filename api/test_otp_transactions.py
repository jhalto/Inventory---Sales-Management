from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.email_service import hash_email_otp
from accounts.models import EmailOTP, PhoneOTP, User
from accounts.otp_service import hash_otp
from accounts.serializers import VerifyEmailOTPSerializer, VerifyOTPSerializer
from accounts.throttles import DEFAULT_RATES


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL transactions and row locks')
@override_settings(ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class OTPTransactionTests(TransactionTestCase):
    phone = '+8801712345678'
    email = 'transaction@example.com'

    def setUp(self):
        cache.clear()

    def seed_phone(self, **extra):
        return PhoneOTP.objects.create(
            phone=self.phone, code_hash=hash_otp(self.phone, '000123'),
            expires_at=timezone.now() + timedelta(minutes=5), **extra,
        )

    def seed_email(self):
        user = User.objects.create_user(email=self.email)
        return EmailOTP.objects.create(
            user=user, email=user.email, code_hash=hash_email_otp(user.pk, user.email, '000123'),
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def parallel_checks(self, route, identity, code, count, serializer_type):
        barrier = Barrier(count)
        original = serializer_type.is_valid

        def validate(serializer, *args, **kwargs):
            result = original(serializer, *args, **kwargs)
            barrier.wait(timeout=20)
            return result

        def check(index):
            try:
                # Independent backend connections are essential for a real lock test.
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                response = APIClient(REMOTE_ADDR=f'192.0.2.{index + 1}').post(
                    reverse(route), {**identity, 'otp': code},
                )
                return response.status_code, response.data, pid
            finally:
                connections.close_all()

        with patch.object(serializer_type, 'is_valid', validate):
            with ThreadPoolExecutor(max_workers=count) as pool:
                results = list(pool.map(check, range(count)))
        self.assertEqual(len({pid for _, _, pid in results}), count)
        return [(status, data) for status, data, _ in results]

    def test_phone_wrong_attempts_are_not_lost_and_stop_at_five(self):
        record = self.seed_phone()
        results = self.parallel_checks('verify-otp', {'phone': self.phone}, '999999', 7, VerifyOTPSerializer)
        self.assertEqual([status for status, _ in results], [400] * 7)
        record.refresh_from_db()
        self.assertEqual(record.attempts, 5)
        self.assertFalse(User.objects.exists())

    def test_email_wrong_attempts_are_not_lost_and_stop_at_five(self):
        record = self.seed_email()
        results = self.parallel_checks('verify-email', {'email': self.email}, '999999', 7, VerifyEmailOTPSerializer)
        self.assertEqual([status for status, _ in results], [400] * 7)
        record.refresh_from_db()
        self.assertEqual(record.attempts, 5)
        self.assertFalse(User.objects.get().email_verified)

    def test_phone_code_issues_tokens_once_under_concurrent_correct_checks(self):
        self.seed_phone()
        results = self.parallel_checks('verify-otp', {'phone': self.phone}, '000123', 2, VerifyOTPSerializer)
        self.assertEqual(sorted(status for status, _ in results), [200, 400])
        self.assertEqual(sum('access_token' in data.get('data', {}) for _, data in results), 1)
        self.assertEqual(User.objects.filter(phone=self.phone, phone_verified=True).count(), 1)
        self.assertFalse(PhoneOTP.objects.exists())

    def test_email_code_is_consumed_once_under_concurrent_correct_checks(self):
        self.seed_email()
        results = self.parallel_checks('verify-email', {'email': self.email}, '000123', 2, VerifyEmailOTPSerializer)
        self.assertEqual(sorted(status for status, _ in results), [200, 400])
        self.assertTrue(User.objects.get().email_verified)
        self.assertFalse(EmailOTP.objects.exists())

    @override_settings(PHONE_OTP_BACKEND='twilio_verify', TWILIO_VERIFY_SERVICE_SID='VA' + 'a' * 32)
    def test_twilio_approval_is_checked_once_and_issues_one_token_pair(self):
        self.seed_phone(backend='twilio_verify', service_sid='VA' + 'a' * 32, verification_sid='VE' + 'b' * 32)
        with patch('accounts.remote_checks.check_verification', return_value=True) as check:
            results = self.parallel_checks('verify-otp', {'phone': self.phone}, '000123', 2, VerifyOTPSerializer)
        self.assertEqual(sum(status == 200 for status, _ in results), 1)
        self.assertTrue(all(status in (200, 400, 503) for status, _ in results))
        self.assertEqual(sum('access_token' in data.get('data', {}) for _, data in results), 1)
        check.assert_called_once()
        self.assertFalse(PhoneOTP.objects.exists())
