import json
import io
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITransactionTestCase

from accounts.models import PhoneOTP, PhoneOTPRecipient, User
from accounts.throttles import DEFAULT_RATES


SERVICE = 'VA' + 'a' * 32
VERIFICATION = 'VE' + 'b' * 32


@override_settings(
    PHONE_OTP_BACKEND='twilio_verify', TWILIO_ACCOUNT_SID='AC' + 'c' * 32,
    TWILIO_AUTH_TOKEN='test-secret-token', TWILIO_VERIFY_SERVICE_SID=SERVICE,
    ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES},
)
class TwilioVerifyTests(APITransactionTestCase):
    phone = '+8801712345678'

    def setUp(self):
        cache.clear()

    def response(self, status='pending', **extra):
        return json.dumps({'sid': VERIFICATION, 'service_sid': SERVICE, 'to': self.phone, 'status': status, **extra}).encode()

    def send(self):
        return self.client.post(reverse('send-otp'), {'phone': self.phone})

    def verify(self):
        return self.client.post(reverse('verify-otp'), {'phone': self.phone, 'otp': '000123'})

    def seed(self):
        return PhoneOTP.objects.create(
            phone=self.phone, backend='twilio_verify', verification_sid=VERIFICATION,
            service_sid=SERVICE, code_hash='', expires_at=timezone.now() + timedelta(minutes=5),
        )

    def test_send_and_approved_check_issue_tokens_without_local_code_generation(self):
        with patch('accounts.twilio_verify.urlopen') as request, patch('accounts.otp_service.secrets.randbelow') as generate:
            request.return_value.__enter__.return_value.read.side_effect = [self.response(), self.response('approved')]
            response = self.send()
            self.assertEqual(response.status_code, 200)
            self.assertFalse(User.objects.exists())
            self.assertEqual(PhoneOTP.objects.get().code_hash, '')
            start_request = request.call_args.args[0]
            self.assertTrue(start_request.full_url.endswith('/Verifications'))
            self.assertEqual(parse_qs(start_request.data.decode()), {'To': [self.phone], 'Channel': ['sms']})
            response = self.verify()
            self.assertEqual(response.status_code, 200)
            self.assertIn('access_token', response.data['data'])
            self.assertTrue(User.objects.get(phone=self.phone).phone_verified)
            self.assertFalse(PhoneOTP.objects.exists())
            check_request = request.call_args.args[0]
            self.assertTrue(check_request.full_url.endswith('/VerificationCheck'))
            self.assertEqual(parse_qs(check_request.data.decode()), {'VerificationSid': [VERIFICATION], 'Code': ['000123']})
            self.assertNotIn('000123', str(response.data))
            self.assertNotIn('test-secret-token', str(response.data))
            self.assertEqual(self.verify().status_code, 400)
            self.assertEqual(request.call_count, 2)
            generate.assert_not_called()

    def test_pending_check_never_authenticates_and_enforces_attempt_limit(self):
        self.seed()
        with patch('accounts.twilio_verify.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = self.response()
            for _ in range(5):
                self.assertEqual(self.verify().status_code, 400)
            self.assertEqual(self.verify().status_code, 400)
            self.assertEqual(request.call_count, 5)
        self.assertFalse(User.objects.exists())
        self.assertEqual(PhoneOTP.objects.get().attempts, 5)

    def test_deleted_or_expired_provider_verification_returns_400(self):
        self.seed()
        with patch('accounts.twilio_verify.urlopen', side_effect=HTTPError('https://verify.twilio.com', 404, 'Not Found', {}, None)):
            self.assertEqual(self.verify().status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_provider_failures_are_sanitized_and_reserve_attempts(self):
        for error, expected in (
            (URLError('unavailable'), 503),
            (HTTPError('https://verify.twilio.com', 401, 'Unauthorized', {}, None), 503),
            (HTTPError('https://verify.twilio.com', 429, 'Too Many Requests', {}, None), 429),
        ):
            PhoneOTP.objects.all().delete()
            record = self.seed()
            with self.subTest(status=expected), patch('accounts.twilio_verify.urlopen', side_effect=error):
                self.assertEqual(self.verify().status_code, expected)
            record.refresh_from_db()
            self.assertEqual(record.attempts, 1)
        self.assertFalse(User.objects.exists())

    def test_failed_send_retains_durable_job_and_cooldown(self):
        for error, expected in (
            (URLError('unavailable'), 503),
            (HTTPError('https://verify.twilio.com', 429, 'Too Many Requests', {}, None), 429),
        ):
            with self.subTest(status=expected), patch('accounts.twilio_verify.urlopen', side_effect=error):
                PhoneOTP.objects.all().delete()
                PhoneOTPRecipient.objects.all().delete()
                self.assertEqual(self.send().status_code, 202)
            self.assertEqual(PhoneOTP.objects.get().delivery.state, 'unknown')
            self.assertTrue(PhoneOTPRecipient.objects.exists())

    def test_approval_must_match_recipient_service_and_verification(self):
        self.seed()
        for mismatch in ({'to': '+8801812345678'}, {'service_sid': 'VA' + 'd' * 32}, {'sid': 'VE' + 'e' * 32}):
            with self.subTest(mismatch=mismatch), patch('accounts.twilio_verify.urlopen') as request:
                request.return_value.__enter__.return_value.read.return_value = self.response('approved', **mismatch)
                self.assertEqual(self.verify().status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_missing_configuration_never_falls_back_to_console(self):
        with override_settings(TWILIO_AUTH_TOKEN=''), patch('accounts.twilio_verify.urlopen') as request, patch('builtins.print') as output:
            self.assertEqual(self.send().status_code, 202)
        request.assert_not_called()
        output.assert_not_called()

    def test_backend_switch_cannot_verify_an_old_local_code(self):
        record = self.seed()
        record.backend = 'local'
        record.save(update_fields=['backend'])
        with patch('accounts.twilio_verify.urlopen') as request:
            self.assertEqual(self.verify().status_code, 400)
        request.assert_not_called()

    def test_provider_error_logs_only_numeric_diagnostics(self):
        error = HTTPError('https://verify.twilio.com', 400, 'Bad Request', {}, io.BytesIO(json.dumps({
            'code': 21608, 'message': 'private recipient and credential details',
        }).encode()))
        with self.assertLogs('accounts.twilio_verify', level='WARNING') as logs:
            with patch('accounts.twilio_verify.urlopen', side_effect=error):
                response = self.send()
        self.assertEqual(response.status_code, 202)
        logged = ' '.join(logs.output)
        self.assertIn('HTTP 400', logged)
        self.assertIn('21608', logged)
        for sensitive in (self.phone, 'test-secret-token', 'private recipient and credential details'):
            self.assertNotIn(sensitive, logged)
            self.assertNotIn(sensitive, str(response.data))
        self.assertNotIn('21608', str(response.data))
