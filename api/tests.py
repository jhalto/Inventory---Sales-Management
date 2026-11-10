from unittest.mock import patch
import re
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import EmailOTP, User
from accounts.models import PhoneOTP
from accounts.otp_service import hash_otp
from accounts.sms_service import SMSDeliveryError


@override_settings(
    MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}},
)
class EmailVerificationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.credentials = {'email': 'new@example.com', 'password': 'StrongTestPass!7359'}

    def register(self):
        return self.client.post(reverse('register'), self.credentials)

    def emailed_otp(self):
        return re.search(r'\b[0-9]{6}\b', mail.outbox[-1].body).group()

    def verify(self, otp):
        return self.client.post(reverse('verify-email'), {
            'email': self.credentials['email'], 'otp': otp,
        })

    def test_registration_sends_verifiable_email_and_requires_verification(self):
        response = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertNotIn('access_token', response.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ['new@example.com'])
        self.assertFalse(User.objects.get().email_verified)
        self.assertEqual(self.client.post(reverse('login'), self.credentials).status_code, 403)
        otp = self.emailed_otp()
        self.assertNotIn(otp, str(response.data))
        self.assertNotEqual(EmailOTP.objects.get().code_hash, otp)
        self.assertEqual(self.verify(otp).status_code, 200)
        self.assertTrue(User.objects.get().email_verified)
        self.assertFalse(EmailOTP.objects.exists())
        self.assertEqual(self.verify(otp).status_code, 400)
        self.assertEqual(self.client.post(reverse('login'), self.credentials).status_code, 200)

    def test_invalid_and_expired_codes_are_rejected(self):
        self.register()
        self.assertEqual(self.verify('bad').status_code, 400)
        EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.verify(self.emailed_otp()).status_code, 400)
        self.assertFalse(User.objects.get().email_verified)

    def test_email_change_invalidates_code(self):
        self.register()
        User.objects.update(email='changed@example.com')
        self.credentials['email'] = 'changed@example.com'
        self.assertEqual(self.verify(self.emailed_otp()).status_code, 400)

    def test_resend_supports_existing_unverified_accounts(self):
        self.register()
        response = self.client.post(reverse('resend-verification-email'), {'email': 'new@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self.verify(self.emailed_otp()).status_code, 200)
        self.client.post(reverse('resend-verification-email'), {'email': 'new@example.com'})
        self.assertEqual(len(mail.outbox), 2)

    def test_delivery_failure_rolls_back_registration(self):
        with patch('accounts.email_service.send_mail', side_effect=OSError('SMTP unavailable')):
            response = self.register()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(User.objects.exists())
        self.assertFalse(EmailOTP.objects.exists())

    def test_code_is_locked_after_five_wrong_attempts(self):
        self.register()
        otp = self.emailed_otp()
        wrong = '000000' if otp != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self.verify(wrong).status_code, 400)
        self.assertEqual(EmailOTP.objects.get().attempts, 5)
        self.assertEqual(self.verify(otp).status_code, 400)
        self.assertFalse(User.objects.get().email_verified)

    def test_resend_replaces_code_and_preserves_old_code_on_delivery_failure(self):
        with patch('accounts.email_service.secrets.randbelow', return_value=123):
            self.register()
        self.assertEqual(self.emailed_otp(), '000123')
        with patch('accounts.email_service.send_mail', side_effect=OSError('SMTP unavailable')):
            response = self.client.post(reverse('resend-verification-email'), {'email': self.credentials['email']})
        self.assertEqual(response.status_code, 503)
        from accounts.email_service import hash_email_otp
        record = EmailOTP.objects.get()
        self.assertEqual(record.code_hash, hash_email_otp(record.user_id, record.email, '000123'))
        with patch('accounts.email_service.secrets.randbelow', return_value=456):
            response = self.client.post(reverse('resend-verification-email'), {'email': self.credentials['email']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.verify('000123').status_code, 400)
        self.assertEqual(self.verify('000456').status_code, 200)

    def test_resend_does_not_expose_unknown_accounts(self):
        response = self.client.post(reverse('resend-verification-email'), {'email': 'missing@example.com'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_duplicate_email_skips_password_validation(self):
        User.objects.create_user(email='new@example.com')
        response = self.client.post(
            reverse('register'), {'email': 'NEW@example.com', 'password': 'short'}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['errors'], {'email': ['This email is already registered.']})

    def test_new_email_still_requires_strong_password(self):
        response = self.client.post(
            reverse('register'), {'email': 'new@example.com', 'password': 'short'}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('password', response.data['errors'])
        self.assertNotIn('email', response.data['errors'])


@override_settings(DEBUG=True, SMS_BACKEND='accounts.sms_service.console_send_sms')
class PhoneOTPTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.phone = '+8801712345678'

    def send(self):
        return self.client.post(reverse('send-otp'), {'phone': self.phone})

    def verify(self, otp):
        return self.client.post(reverse('verify-otp'), {'phone': self.phone, 'otp': otp})

    def test_console_code_can_verify_and_cannot_be_reused(self):
        with patch('accounts.otp_service.secrets.randbelow', return_value=123), patch('builtins.print') as output:
            response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertIn('000123', output.call_args.args[0])
        self.assertNotIn('000123', str(response.data))
        self.assertEqual(PhoneOTP.objects.get().code_hash, hash_otp(self.phone, '000123'))
        self.assertEqual(self.verify('000123').status_code, 200)
        self.assertTrue(User.objects.get(phone=self.phone).phone_verified)
        self.assertEqual(self.verify('000123').status_code, 400)

    def test_resend_cooldown(self):
        with patch('builtins.print'):
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send().status_code, 429)

    def test_failed_delivery_preserves_previous_code(self):
        from accounts.otp_service import generate_otp
        generate_otp(self.phone)
        PhoneOTP.objects.update(expires_at=timezone.now() + timedelta(minutes=3))
        original = PhoneOTP.objects.get().code_hash
        with patch('api.views.accounts.views.send_sms', side_effect=SMSDeliveryError('Failed')):
            self.assertEqual(self.send().status_code, 503)
        self.assertEqual(PhoneOTP.objects.get().code_hash, original)

    @override_settings(DEBUG=False)
    def test_console_cannot_print_in_production(self):
        with patch('builtins.print') as output:
            self.assertEqual(self.send().status_code, 503)
        output.assert_not_called()
        self.assertFalse(PhoneOTP.objects.exists())

    def test_attempt_limit_and_expiry(self):
        with patch('accounts.otp_service.secrets.randbelow', return_value=123), patch('builtins.print'):
            self.send()
        for _ in range(5):
            self.assertEqual(self.verify('999999').status_code, 400)
        self.assertEqual(self.verify('000123').status_code, 400)
        PhoneOTP.objects.update(attempts=0, expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.verify('000123').status_code, 400)

    @override_settings(
        DEBUG=False, SMS_BACKEND='accounts.sms_service.twilio_send_sms',
        TWILIO_ACCOUNT_SID='ACtest', TWILIO_AUTH_TOKEN='test-token',
        TWILIO_FROM_NUMBER='+15005550006', TWILIO_MESSAGING_SERVICE_SID='',
    )
    def test_twilio_submission_and_rejection(self):
        from urllib.error import URLError
        from urllib.parse import parse_qs
        with patch('accounts.sms_service.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"sid":"SMtest","status":"queued"}'
            self.assertEqual(self.send().status_code, 200)
            payload = parse_qs(request.call_args.args[0].data.decode())
            self.assertEqual(payload['To'], [self.phone])
            self.assertEqual(payload['From'], ['+15005550006'])
            self.assertEqual(request.call_args.kwargs['timeout'], 10)
        PhoneOTP.objects.all().delete()
        with patch('accounts.sms_service.urlopen', side_effect=URLError('unavailable')):
            self.assertEqual(self.send().status_code, 503)
        self.assertFalse(PhoneOTP.objects.exists())
