from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import io
from threading import Barrier
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import AccountChallenge, AccountChallengeRecipient, GoogleIdentity, PhoneOTP, User
from accounts.sms_service import SMSDeliveryError
from accounts.throttles import DEFAULT_RATES


@override_settings(ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class SelfServiceTests(TransactionTestCase):
    password = 'OriginalSelfService!8247'
    new_password = ' ReplacementSelfService!9358 '

    def setUp(self):
        cache.clear()
        self.codes = patch('accounts.self_service.secrets.randbelow', return_value=123456)
        self.codes.start()
        self.addCleanup(self.codes.stop)
        self.sms = patch('accounts.outbox.send_sms')
        self.sms_mock = self.sms.start()
        self.addCleanup(self.sms.stop)
        self.user = User.objects.create_user(email='owner@example.com', password=self.password, email_verified=True)
        self.client = APIClient()
        self.authenticate(self.user)

    def authenticate(self, user):
        self.tokens = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.tokens.access_token}')

    def post(self, name, data):
        return self.client.post(reverse(name), data, format='json')

    def request_contact(self, channel='email', target='new@example.com'):
        response = self.post(f'account-{channel}-request', {channel: target})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['challenge_id']

    def test_password_change_requires_current_password_and_revokes_old_tokens(self):
        self.assertEqual(self.post('password-change', {'current_password': 'wrong', 'new_password': self.new_password}).status_code, 400)
        response = self.post('password-change', {'current_password': self.password, 'new_password': self.new_password})
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))
        self.assertFalse(self.user.check_password(self.new_password.strip()))
        self.assertEqual(self.post('reauth-request', {'channel': 'email'}).status_code, 401)
        self.assertEqual(self.post('token-refresh', {'refresh': str(self.tokens)}).status_code, 401)

    def test_contextual_password_validation_precedes_change(self):
        for password in ('short', '   ', 'owner@example.com'):
            with self.subTest(password=password):
                response = self.post('password-change', {'current_password': self.password, 'new_password': password})
                self.assertEqual(response.status_code, 400)
                self.assertIn('new_password', response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.password))

    def test_email_add_or_change_requires_target_code_and_existing_password(self):
        challenge = self.request_contact(target=' New@Example.COM ')
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'owner@example.com')
        self.assertIn('123456', mail.outbox[-1].body)
        self.assertEqual(mail.outbox[-1].to, ['new@example.com'])
        data = {'challenge_id': challenge, 'otp': '123456', 'current_password': 'wrong'}
        self.assertEqual(self.post('account-email-confirm', data).status_code, 400)
        data['current_password'] = self.password
        self.assertEqual(self.post('account-email-confirm', data).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'new@example.com')
        self.assertTrue(self.user.email_verified)
        self.assertEqual(self.post('account-email-confirm', data).status_code, 400)

    def test_phone_only_user_can_attach_verified_email_using_current_phone_proof(self):
        user = User.objects.create_user(phone='+8801712345678', phone_verified=True, is_staff=True)
        self.authenticate(user)
        challenge = self.request_contact()
        reauth = self.post('reauth-request', {'channel': 'phone'})
        self.assertEqual(reauth.status_code, 200, reauth.data)
        self.sms_mock.assert_called_once()
        response = self.post('account-email-confirm', {
            'challenge_id': challenge, 'otp': '123456',
            'reauth_challenge_id': reauth.data['challenge_id'], 'reauth_otp': '123456',
        })
        self.assertEqual(response.status_code, 200, response.data)
        user.refresh_from_db()
        self.assertEqual(user.email, 'new@example.com')
        self.assertTrue(user.email_verified)
        self.assertTrue(user.is_staff)
        self.assertEqual(User.objects.count(), 2)
        identity = {'subject': 'linked-phone-user', 'email': user.email}
        with patch('api.views.accounts.google.verify_google_mobile_token', return_value=identity):
            linked = self.post('google-mobile-link', {'credential': 'valid-google-token'})
        self.assertEqual(linked.status_code, 200, linked.data)
        self.assertEqual(GoogleIdentity.objects.get(subject=identity['subject']).user_id, user.pk)

    def test_phone_link_verifies_new_contact_and_preserves_account(self):
        challenge = self.request_contact('phone', '01712345678')
        response = self.post('account-phone-confirm', {
            'challenge_id': challenge, 'otp': '123456', 'current_password': self.password,
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone, '+8801712345678')
        self.assertTrue(self.user.phone_verified)
        self.assertTrue(self.user.email_verified)
        self.assertEqual(User.objects.count(), 1)

    def test_identity_owned_by_another_user_is_never_merged(self):
        other = User.objects.create_user(email='taken@example.com', phone='+8801712345678')
        for channel, target in (('email', other.email), ('phone', other.phone)):
            self.assertEqual(self.post(f'account-{channel}-request', {channel: target}).status_code, 409)
        self.assertFalse(AccountChallenge.objects.exists())
        self.sms_mock.assert_not_called()

    def test_identity_claimed_after_request_returns_conflict(self):
        challenge = self.request_contact()
        User.objects.create_user(email='new@example.com')
        response = self.post('account-email-confirm', {'challenge_id': challenge, 'otp': '123456', 'current_password': self.password})
        self.assertEqual(response.status_code, 409)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'owner@example.com')

    def test_challenges_are_bound_to_user_purpose_and_current_identity(self):
        challenge = self.request_contact()
        other = User.objects.create_user(email='other@example.com', password=self.password)
        self.authenticate(other)
        data = {'challenge_id': challenge, 'otp': '123456', 'current_password': self.password}
        self.assertEqual(self.post('account-email-confirm', data).status_code, 400)
        self.authenticate(self.user)
        self.assertEqual(self.post('password-reset-confirm', {'challenge_id': challenge, 'otp': '123456', 'new_password': self.new_password}).status_code, 400)
        self.user.email = 'changed@example.com'
        self.user.save(update_fields=['email'])
        self.assertFalse(AccountChallenge.objects.exists())
        self.assertEqual(self.post('account-email-confirm', data).status_code, 400)

    def test_wrong_otp_attempts_commit_and_lock_out_correct_code(self):
        challenge = self.request_contact()
        data = {'challenge_id': challenge, 'otp': '999999', 'current_password': self.password}
        for _ in range(5):
            self.assertEqual(self.post('account-email-confirm', data).status_code, 400)
        self.assertEqual(AccountChallenge.objects.get(pk=challenge).attempts, 5)
        data['otp'] = '123456'
        self.assertEqual(self.post('account-email-confirm', data).status_code, 400)

    def test_expired_proof_is_rejected(self):
        challenge = self.request_contact()
        AccountChallenge.objects.filter(pk=challenge).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.post('account-email-confirm', {'challenge_id': challenge, 'otp': '123456', 'current_password': self.password}).status_code, 400)

    def test_reauth_wrong_attempts_are_persistent_and_proofs_cannot_cross_purposes(self):
        sent = self.post('reauth-request', {'channel': 'email'})
        data = {'reauth_challenge_id': sent.data['challenge_id'], 'reauth_otp': '999999', 'new_password': self.new_password}
        for _ in range(5):
            self.assertEqual(self.post('password-change', data).status_code, 400)
        self.assertEqual(AccountChallenge.objects.get(pk=sent.data['challenge_id']).attempts, 5)
        data['reauth_otp'] = '123456'
        self.assertEqual(self.post('password-change', data).status_code, 400)

    def test_phone_link_proof_cannot_be_used_for_public_phone_login(self):
        challenge = self.request_contact('phone', '01712345678')
        self.assertFalse(PhoneOTP.objects.exists())
        response = self.post('verify-otp', {'phone': '01712345678', 'otp': '123456'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(AccountChallenge.objects.filter(pk=challenge).exists())

    def test_cleanup_only_removes_expired_challenges(self):
        challenge = self.request_contact()
        call_command('cleanup_account_challenges', stdout=io.StringIO())
        self.assertTrue(AccountChallenge.objects.filter(pk=challenge).exists())
        AccountChallenge.objects.filter(pk=challenge).update(expires_at=timezone.now() - timedelta(seconds=1))
        call_command('cleanup_account_challenges', stdout=io.StringIO())
        self.assertFalse(AccountChallenge.objects.exists())
        self.assertTrue(AccountChallengeRecipient.objects.exists())

    @override_settings(ACCOUNT_THROTTLE_RATES={**{scope: None for scope in DEFAULT_RATES}, 'management_ip': '1/hour'})
    def test_public_recovery_is_ip_throttled(self):
        data = {'channel': 'email', 'email': 'unknown@example.com'}
        self.client.credentials()
        self.assertEqual(self.post('password-reset-request', data).status_code, 200)
        self.assertEqual(self.post('password-reset-request', data).status_code, 429)

    @override_settings(ACCOUNT_THROTTLE_RATES={**{scope: None for scope in DEFAULT_RATES}, 'management_user': '1/hour'})
    def test_authenticated_management_is_account_throttled_across_ips(self):
        data = {'current_password': 'wrong'}
        self.assertEqual(self.client.post(reverse('google-unlink'), data, format='json', REMOTE_ADDR='192.0.2.1').status_code, 400)
        self.assertEqual(self.client.post(reverse('google-unlink'), data, format='json', REMOTE_ADDR='192.0.2.2').status_code, 429)

    def test_reset_verified_email_changes_password_without_logging_in(self):
        self.client.credentials()
        response = self.post('password-reset-request', {'channel': 'email', 'email': ' OWNER@Example.com '})
        self.assertEqual(response.status_code, 200)
        challenge = response.data['challenge_id']
        self.assertNotIn('123456', str(response.data))
        data = {'challenge_id': challenge, 'otp': '123456', 'new_password': self.new_password}
        response = self.post('password-reset-confirm', data)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn('access', response.data)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(self.new_password))
        self.assertEqual(self.post('password-reset-confirm', data).status_code, 400)
        self.assertEqual(self.post('token-refresh', {'refresh': str(self.tokens)}).status_code, 401)

    def test_reset_verified_phone_can_set_first_password(self):
        user = User.objects.create_user(phone='+8801712345678', phone_verified=True)
        response = self.post('password-reset-request', {'channel': 'phone', 'phone': '01712345678'})
        data = {'challenge_id': response.data['challenge_id'], 'otp': '123456', 'new_password': self.new_password}
        self.assertEqual(self.post('password-reset-confirm', data).status_code, 200)
        user.refresh_from_db()
        self.assertTrue(user.check_password(self.new_password))
        self.assertTrue(user.phone_verified)

    def test_reset_unknown_unverified_and_inactive_contacts_get_generic_response(self):
        User.objects.create_user(email='unverified@example.com')
        User.objects.create_user(email='inactive@example.com', email_verified=True, is_active=False)
        for email in ('unknown@example.com', 'unverified@example.com', 'inactive@example.com'):
            response = self.post('password-reset-request', {'channel': 'email', 'email': email})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.data), {'challenge_id', 'message'})
            self.assertFalse(AccountChallenge.objects.filter(pk=response.data['challenge_id']).exists())
        self.assertFalse(mail.outbox)

    def test_reset_cooldown_and_provider_failure_keep_generic_response(self):
        first = self.post('password-reset-request', {'channel': 'email', 'email': self.user.email})
        again = self.post('password-reset-request', {'channel': 'email', 'email': self.user.email})
        self.assertEqual(again.status_code, 200)
        self.assertNotEqual(first.data['challenge_id'], again.data['challenge_id'])
        self.assertEqual(AccountChallenge.objects.count(), 1)
        AccountChallengeRecipient.objects.all().delete()
        with patch('accounts.outbox.send_mail', side_effect=OSError('offline')):
            response = self.post('password-reset-request', {'channel': 'email', 'email': self.user.email})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AccountChallenge.objects.filter(pk=first.data['challenge_id']).exists())
        self.assertEqual(AccountChallenge.objects.get(pk=response.data['challenge_id']).delivery.state, 'unknown')

    def test_passwordless_user_can_set_password_with_one_time_reauth(self):
        self.user.set_unusable_password()
        self.user.save(update_fields=['password'])
        self.authenticate(self.user)
        response = self.post('reauth-request', {'channel': 'email'})
        data = {'reauth_challenge_id': response.data['challenge_id'], 'reauth_otp': '123456', 'new_password': self.new_password}
        self.assertEqual(self.post('password-change', data).status_code, 200)
        self.authenticate(User.objects.get(pk=self.user.pk))
        self.assertEqual(self.post('password-change', data).status_code, 400)

    def test_google_unlink_requires_proof_and_an_alternative_login(self):
        GoogleIdentity.objects.create(user=self.user, subject='unlink-test')
        self.assertEqual(self.post('google-unlink', {'current_password': 'wrong'}).status_code, 400)
        self.assertTrue(GoogleIdentity.objects.exists())
        self.assertEqual(self.post('google-unlink', {'current_password': self.password}).status_code, 204)
        self.assertFalse(GoogleIdentity.objects.exists())

    def test_google_only_user_cannot_remove_last_login(self):
        self.user.set_unusable_password()
        self.user.save(update_fields=['password'])
        GoogleIdentity.objects.create(user=self.user, subject='last-login')
        self.authenticate(self.user)
        reauth = self.post('reauth-request', {'channel': 'email'})
        response = self.post('google-unlink', {'reauth_challenge_id': reauth.data['challenge_id'], 'reauth_otp': '123456'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(GoogleIdentity.objects.exists())

    def test_recipient_cooldown_cannot_be_bypassed_by_other_accounts(self):
        self.request_contact()
        other = User.objects.create_user(email='other@example.com')
        self.authenticate(other)
        response = self.post('account-email-request', {'email': 'new@example.com'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(AccountChallenge.objects.count(), 1)

    def test_delivery_failure_retains_challenge_without_changing_contact(self):
        with patch('accounts.outbox.send_mail', side_effect=OSError('offline')):
            self.assertEqual(self.post('account-email-request', {'email': 'new@example.com'}).status_code, 200)
        with patch('accounts.outbox.send_sms', side_effect=SMSDeliveryError('offline')):
            self.assertEqual(self.post('account-phone-request', {'phone': '01712345678'}).status_code, 200)
        self.assertEqual(AccountChallenge.objects.filter(delivery__state='unknown').count(), 2)
        self.assertEqual(AccountChallengeRecipient.objects.count(), 2)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'owner@example.com')
        self.assertIsNone(self.user.phone)

    @override_settings(PHONE_OTP_BACKEND='twilio_verify', TWILIO_VERIFY_SERVICE_SID='VA' + '1' * 32)
    def test_twilio_phone_link_uses_bound_provider_reference(self):
        sid = 'VE' + '2' * 32
        with patch('accounts.outbox.start_verification', return_value=sid) as start:
            challenge = self.request_contact('phone', '01712345678')
        start.assert_called_once_with('+8801712345678')
        self.sms_mock.assert_not_called()
        with patch('accounts.remote_checks.check_verification', return_value=True) as check:
            response = self.post('account-phone-confirm', {'challenge_id': challenge, 'otp': '123456', 'current_password': self.password})
        self.assertEqual(response.status_code, 200, response.data)
        check.assert_called_once_with('+8801712345678', '123456', sid)

    def test_unauthenticated_management_is_denied(self):
        self.client.credentials()
        for name in ('password-change', 'reauth-request', 'account-email-request', 'account-phone-request',
                     'account-email-confirm', 'account-phone-confirm', 'google-unlink'):
            self.assertEqual(self.post(name, {}).status_code, 401, name)

    def test_malformed_roots_and_field_types_return_400(self):
        names = ('password-reset-request', 'password-reset-confirm', 'password-change', 'reauth-request',
                 'account-email-request', 'account-phone-request', 'account-email-confirm', 'account-phone-confirm', 'google-unlink')
        for name in names:
            for body in ([], None, 123, {}, {'email': [], 'phone': 123, 'current_password': None, 'challenge_id': 1}):
                with self.subTest(name=name, body=body):
                    response = self.client.generic('POST', reverse(name), json.dumps(body), content_type='application/json')
                    self.assertEqual(response.status_code, 400, response.data)


@override_settings(ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class SelfServiceConcurrencyTests(TransactionTestCase):
    def assert_first_send_is_serialized(self, channel, target):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locking.')
        cache.clear()
        tokens = [str(RefreshToken.for_user(User.objects.create_user(email=f'parallel-{index}@example.com')).access_token)
                  for index in range(2)]
        barrier = Barrier(2)

        def request(token):
            close_old_connections()
            try:
                client = APIClient()
                client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
                barrier.wait(timeout=10)
                return client.post(reverse(f'account-{channel}-request'), {channel: target}, format='json').status_code
            finally:
                close_old_connections()

        with patch('accounts.outbox.send_sms') as sms, ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, tokens))
        self.assertEqual(sorted(results), [200, 429])
        self.assertEqual(AccountChallenge.objects.count(), 1)
        self.assertEqual(AccountChallengeRecipient.objects.get().sent_count, 1)
        if channel == 'phone':
            sms.assert_called_once()
        else:
            self.assertEqual(len(mail.outbox), 1)

    def test_parallel_first_email_requests_send_once(self):
        self.assert_first_send_is_serialized('email', 'new-parallel@example.com')

    def test_parallel_first_phone_requests_send_once(self):
        self.assert_first_send_is_serialized('phone', '+8801712345678')

    def test_recovery_code_is_consumed_once_under_parallel_requests(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locking.')
        cache.clear()
        user = User.objects.create_user(email='parallel-reset@example.com', email_verified=True)
        with patch('accounts.self_service.secrets.randbelow', return_value=123456):
            sent = APIClient().post(reverse('password-reset-request'), {'channel': 'email', 'email': user.email}, format='json')
        barrier = Barrier(2)

        def confirm(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = APIClient().post(reverse('password-reset-confirm'), {
                    'challenge_id': sent.data['challenge_id'], 'otp': '123456', 'new_password': 'ParallelResetTest!8247',
                }, format='json')
                return response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(confirm, range(2)))
        self.assertEqual(sorted(results), [200, 400])
