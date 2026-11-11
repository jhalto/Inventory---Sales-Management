from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import io
from threading import Event
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection, connections, OperationalError, transaction
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .email_service import send_verification_email
from .models import AccountChallengeRecipient, DeliveryJob, EmailOTP, PhoneOTP, User, VerificationReceipt
from .outbox import process_job
from .remote_checks import prepare_phone_check, RemoteCheckUnavailable
from .throttles import DEFAULT_RATES


@override_settings(OUTBOX_INLINE_DISPATCH=False, ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class DeliveryOutboxTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='outbox@example.com')

    def queue(self):
        with patch('accounts.email_service.secrets.randbelow', return_value=123):
            return send_verification_email(self.user)

    def verify(self):
        return APIClient().post(reverse('verify-email'), {'email': self.user.email, 'otp': '000123'}, format='json')

    def test_encrypted_intent_and_challenge_commit_before_network(self):
        with patch('accounts.outbox.send_mail') as provider:
            job = self.queue()
        provider.assert_not_called()
        self.assertEqual(job.state, 'pending')
        self.assertNotIn('000123', job.payload)
        self.assertNotIn(self.user.email, job.payload)
        self.assertEqual(EmailOTP.objects.get().delivery_id, job.pk)
        self.assertEqual(self.verify().status_code, 400)

        def send(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(DeliveryJob.objects.get(pk=job.pk).state, 'sending')
            self.assertIn('000123', args[1])
            return 1

        with patch('accounts.outbox.send_mail', side_effect=send):
            self.assertTrue(process_job(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.payload, '')
        self.assertEqual(self.verify().status_code, 200)

    @override_settings(OUTBOX_INLINE_DISPATCH=True)
    def test_rollback_does_not_dispatch_or_leave_job(self):
        with patch('accounts.outbox.send_mail') as provider:
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.queue()
                raise RuntimeError('Abort challenge transaction')
        provider.assert_not_called()
        self.assertFalse(DeliveryJob.objects.exists())
        self.assertFalse(EmailOTP.objects.exists())

    def test_ambiguous_timeout_retains_a_usable_challenge(self):
        job = self.queue()
        with patch('accounts.outbox.send_mail', side_effect=TimeoutError('submission uncertain')):
            self.assertFalse(process_job(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.state, 'unknown')
        self.assertTrue(job.payload)
        self.assertEqual(self.verify().status_code, 200)
        DeliveryJob.objects.filter(pk=job.pk).update(available_at=timezone.now())
        with patch('accounts.outbox.send_mail') as provider:
            self.assertFalse(process_job(job.pk))
        provider.assert_not_called()  # Consumed/superseded work is cancelled.

    def test_lost_acknowledgement_recovers_same_code_after_lease(self):
        job = self.queue()
        original = DeliveryJob.save

        def lose_ack(instance, *args, **kwargs):
            if instance.state == 'sent':
                raise OperationalError('Lost acknowledgement')
            return original(instance, *args, **kwargs)

        with patch('accounts.outbox.send_mail', return_value=1), patch.object(DeliveryJob, 'save', lose_ack):
            with self.assertRaises(OperationalError):
                process_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.state, 'sending')
        self.assertTrue(job.payload)
        digest = EmailOTP.objects.get().code_hash
        DeliveryJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        with patch('accounts.outbox.send_mail', return_value=1) as provider:
            self.assertTrue(process_job(job.pk))
        self.assertIn('000123', provider.call_args.args[1])
        self.assertEqual(EmailOTP.objects.get().code_hash, digest)
        self.assertEqual(self.verify().status_code, 200)

    def test_superseded_and_expired_jobs_never_submit(self):
        old = self.queue()
        AccountChallengeRecipient.objects.update(last_sent_at=timezone.now() - timedelta(minutes=2))
        new = send_verification_email(self.user)
        DeliveryJob.objects.filter(pk=new.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with patch('accounts.outbox.send_mail') as provider:
            self.assertFalse(process_job(old.pk))
            self.assertFalse(process_job(new.pk))
        provider.assert_not_called()
        self.assertEqual(DeliveryJob.objects.filter(state='cancelled', payload='').count(), 2)

    def test_retries_are_bounded_and_do_not_extend_code_expiry(self):
        job = self.queue()
        expiry = job.expires_at
        with patch('accounts.outbox.send_mail', side_effect=TimeoutError('unknown')) as provider:
            for _ in range(4):
                DeliveryJob.objects.filter(pk=job.pk).update(available_at=timezone.now())
                process_job(job.pk)
        self.assertEqual(provider.call_count, 3)
        job.refresh_from_db()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.expires_at, expiry)
        self.assertEqual(self.verify().status_code, 200)  # Ambiguous deliveries remain redeemable.

    @override_settings(PHONE_OTP_BACKEND='twilio_verify', TWILIO_VERIFY_SERVICE_SID='VA' + 'a' * 32)
    def test_twilio_start_recovers_reference_after_lost_acknowledgement(self):
        response = APIClient().post(reverse('send-otp'), {'phone': '01712345678'}, format='json')
        self.assertEqual(response.status_code, 202)
        record = PhoneOTP.objects.get()
        job = record.delivery
        original = DeliveryJob.save

        def lose_ack(instance, *args, **kwargs):
            if instance.state == 'sent':
                raise OperationalError('Lost start reference')
            return original(instance, *args, **kwargs)

        def start(target):
            self.assertFalse(connection.in_atomic_block)
            return 'VE' + 'b' * 32

        with patch('accounts.outbox.start_verification', side_effect=start), patch.object(DeliveryJob, 'save', lose_ack):
            with self.assertRaises(OperationalError):
                process_job(job.pk)
        record.refresh_from_db()
        self.assertEqual(record.verification_sid, '')
        DeliveryJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        with patch('accounts.outbox.start_verification', side_effect=start):
            self.assertTrue(process_job(job.pk))
        record.refresh_from_db()
        self.assertEqual(record.verification_sid, 'VE' + 'b' * 32)

    def test_two_workers_submit_once_without_holding_account_lock(self):
        job = self.queue()
        entered, release = Event(), Event()

        def submit(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(10):
                raise AssertionError('Provider was not released')
            return 1

        def worker():
            try:
                return process_job(job.pk)
            finally:
                connections.close_all()

        with patch('accounts.outbox.send_mail', side_effect=submit) as provider, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(10))
                self.assertFalse(pool.submit(worker).result(timeout=3))
                self.user.first_name = 'Updated during provider call'
                self.user.save(update_fields=['first_name'])
            finally:
                release.set()
            self.assertTrue(first.result(timeout=10))
        provider.assert_called_once()

    def test_worker_command_and_payload_cleanup(self):
        job = self.queue()
        with patch('accounts.outbox.send_mail', return_value=1):
            call_command('process_delivery_jobs', stdout=io.StringIO())
        job.refresh_from_db()
        self.assertEqual(job.state, 'sent')
        DeliveryJob.objects.filter(pk=job.pk).update(payload='encrypted-retained', expires_at=timezone.now() - timedelta(seconds=1))
        call_command('cleanup_delivery_state', stdout=io.StringIO())
        job.refresh_from_db()
        self.assertEqual(job.payload, '')


@override_settings(PHONE_OTP_BACKEND='twilio_verify', TWILIO_VERIFY_SERVICE_SID='VA' + 'a' * 32,
                   ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class DurableRemoteCheckTests(TransactionTestCase):
    phone = '+8801712345678'

    def setUp(self):
        self.record = PhoneOTP.objects.create(
            phone=self.phone, backend='twilio_verify', verification_sid='VE' + 'b' * 32,
            service_sid='VA' + 'a' * 32, code_hash='', expires_at=timezone.now() + timedelta(minutes=5),
        )

    def verify(self):
        return APIClient().post(reverse('verify-otp'), {'phone': self.phone, 'otp': '000123'}, format='json')

    def test_remote_approval_runs_outside_transaction_and_survives_local_failure(self):
        def approve(*args):
            self.assertFalse(connection.in_atomic_block)
            return True

        with patch('accounts.remote_checks.check_verification', side_effect=approve) as provider:
            with patch('api.views.accounts.views.RefreshToken.for_user', side_effect=RuntimeError('Local token failure')):
                with self.assertRaises(RuntimeError):
                    self.verify()
            self.assertEqual(VerificationReceipt.objects.get().state, 'approved')
            self.assertTrue(PhoneOTP.objects.exists())
            self.assertEqual(self.verify().status_code, 200)
        provider.assert_called_once()

    def test_unknown_provider_result_is_not_approved_by_retry_or_404(self):
        with patch('accounts.remote_checks.check_verification', side_effect=TimeoutError('unknown')):
            with self.assertRaises(RemoteCheckUnavailable):
                prepare_phone_check(self.phone, '000123')
        self.assertEqual(VerificationReceipt.objects.get().state, 'unknown')
        with patch('accounts.remote_checks.check_verification') as provider:
            self.assertEqual(self.verify().status_code, 503)
        provider.assert_not_called()
        self.assertFalse(User.objects.exists())

    def test_lost_approval_write_remains_fail_closed_after_lease_expiry(self):
        with patch('accounts.remote_checks.check_verification', return_value=True):
            with patch('accounts.remote_checks.VerificationReceipt.objects.filter') as rows:
                rows.return_value.update.side_effect = OperationalError('Lost proof receipt')
                with self.assertRaises(RemoteCheckUnavailable):
                    prepare_phone_check(self.phone, '000123')
        self.assertEqual(VerificationReceipt.objects.get().state, 'checking')
        VerificationReceipt.objects.update(lease_until=timezone.now() - timedelta(seconds=1))
        with patch('accounts.remote_checks.check_verification') as provider:
            self.assertEqual(self.verify().status_code, 503)
        provider.assert_not_called()
        self.assertEqual(VerificationReceipt.objects.get().state, 'unknown')

    def test_parallel_checks_allow_one_remote_call_and_return_retry_for_competitor(self):
        entered, release = Event(), Event()

        def approve(*args):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            if not release.wait(10):
                raise AssertionError('Check not released')
            return True

        def check():
            try:
                return self.verify().status_code
            finally:
                connections.close_all()

        with patch('accounts.remote_checks.check_verification', side_effect=approve) as provider, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(check)
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(pool.submit(check).result(timeout=3), 503)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10), 200)
        provider.assert_called_once()

    def test_remote_call_does_not_block_phone_edit_and_stale_approval_cannot_login(self):
        user = User.objects.create_user(phone=self.phone, phone_verified=True)
        # Creation intentionally invalidates old phone challenges.
        self.setUp()
        entered, release = Event(), Event()

        def approve(*args):
            entered.set()
            if not release.wait(10):
                raise AssertionError('Provider not released')
            return True

        def check():
            try:
                return self.verify().status_code
            finally:
                connections.close_all()

        with patch('accounts.remote_checks.check_verification', side_effect=approve), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(check)
            try:
                self.assertTrue(entered.wait(10))
                user.phone = '+8801812345678'
                user.save(update_fields=['phone'])
            finally:
                release.set()
            self.assertEqual(future.result(timeout=10), 400)
        self.assertEqual(User.objects.count(), 1)
