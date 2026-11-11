from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient, APITransactionTestCase

from accounts.models import PhoneOTP, PhoneOTPRecipient
from accounts.otp_service import hash_otp
from accounts.serializers import SendOTPSerializer
from accounts.sms_service import SMSDeliveryError
from accounts.throttles import DEFAULT_RATES


NO_CACHE_LIMITS = {scope: None for scope in DEFAULT_RATES}


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locking')
@override_settings(ACCOUNT_THROTTLE_RATES=NO_CACHE_LIMITS)
class ConcurrentPhoneIssuanceTests(TransactionTestCase):
    def test_concurrent_first_sends_submit_one_message(self):
        cache.clear()
        self.assertFalse(PhoneOTPRecipient.objects.exists())
        barrier = Barrier(2)
        original = SendOTPSerializer.is_valid

        def validate(serializer, *args, **kwargs):
            result = original(serializer, *args, **kwargs)
            barrier.wait(timeout=15)
            return result

        def send(phone):
            try:
                response = APIClient().post(reverse('send-otp'), {'phone': phone})
                return response.status_code, response.get('Retry-After')
            finally:
                connections.close_all()

        with patch.object(SendOTPSerializer, 'is_valid', validate):
            with patch('accounts.outbox.send_sms') as submit:
                with patch('accounts.otp_service.secrets.randbelow', return_value=123):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(send, ('01712345678', '+8801712345678')))
                submit.assert_called_once()
        self.assertEqual(sorted(status for status, _ in results), [200, 429])
        self.assertEqual(next(retry for status, retry in results if status == 429), '60')
        self.assertEqual(PhoneOTPRecipient.objects.count(), 1)
        self.assertIsNotNone(PhoneOTPRecipient.objects.get().last_sent_at)
        self.assertEqual(PhoneOTP.objects.count(), 1)
        self.assertEqual(PhoneOTP.objects.get().code_hash, hash_otp('+8801712345678', '000123'))


@override_settings(ACCOUNT_THROTTLE_RATES=NO_CACHE_LIMITS)
class PhoneIssuanceLockTests(APITransactionTestCase):
    phone = '+8801712345678'

    def test_consuming_otp_retains_database_cooldown(self):
        with patch('accounts.outbox.send_sms') as submit:
            with patch('accounts.otp_service.secrets.randbelow', return_value=123):
                self.assertEqual(self.client.post(reverse('send-otp'), {'phone': self.phone}).status_code, 200)
            self.assertEqual(self.client.post(reverse('verify-otp'), {'phone': self.phone, 'otp': '000123'}).status_code, 200)
            self.assertFalse(PhoneOTP.objects.exists())
            self.assertEqual(self.client.post(reverse('send-otp'), {'phone': self.phone}).status_code, 429)
            submit.assert_called_once()
        self.assertEqual(PhoneOTPRecipient.objects.count(), 1)

    def test_failed_first_send_retains_recipient_code_and_recovery_job(self):
        with patch('accounts.outbox.send_sms', side_effect=SMSDeliveryError('Unavailable')):
            self.assertEqual(self.client.post(reverse('send-otp'), {'phone': self.phone}).status_code, 202)
        self.assertEqual(PhoneOTP.objects.get().delivery.state, 'unknown')
        self.assertTrue(PhoneOTPRecipient.objects.exists())
