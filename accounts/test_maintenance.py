from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta
import io
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.sessions.models import Session
from django.core.management import call_command, CommandError
from django.db import connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from .delivery_limits import reserve_delivery
from .models import AccountChallenge, AccountChallengeRecipient, EmailOTP, PhoneOTP, PhoneOTPRecipient, User
from .otp_service import lock_phone_recipient
from .throttles import DEFAULT_RATES


class AccountMaintenanceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.old = self.now - timedelta(days=40)

    def cleanup(self, **options):
        call_command('cleanup_account_artifacts', stdout=io.StringIO(), **options)

    def test_expired_otps_are_batched_and_live_codes_are_preserved(self):
        for index in range(3):
            user = User.objects.create_user(email=f'expired{index}@example.com')
            EmailOTP.objects.create(user=user, email=user.email, code_hash='hash', expires_at=self.now - timedelta(seconds=1))
            PhoneOTP.objects.create(phone=f'+880171234567{index}', code_hash='hash', expires_at=self.now - timedelta(seconds=1))
        user = User.objects.create_user(email='active@example.com')
        email = EmailOTP.objects.create(user=user, email=user.email, code_hash='live', expires_at=self.now + timedelta(minutes=5))
        phone = PhoneOTP.objects.create(phone='+8801812345678', code_hash='live', expires_at=self.now + timedelta(minutes=5))
        self.cleanup(batch_size=1)
        self.cleanup(batch_size=1)
        self.assertEqual(list(EmailOTP.objects.values_list('pk', flat=True)), [email.pk])
        self.assertEqual(list(PhoneOTP.objects.values_list('pk', flat=True)), [phone.pk])

    def test_only_old_unused_recipient_rows_are_pruned(self):
        stale_phone = PhoneOTPRecipient.objects.create(phone='+8801712345678', last_used_at=self.old, last_sent_at=self.old)
        stale_email = AccountChallengeRecipient.objects.create(key='email:old@example.com', last_used_at=self.old, last_sent_at=self.old, window_start=self.old)
        never_sent = PhoneOTPRecipient.objects.create(phone='+8801912345678', last_used_at=self.old)
        recent = PhoneOTPRecipient.objects.create(phone='+8801812345678')
        cooldown = PhoneOTPRecipient.objects.create(phone='+8801612345678', last_used_at=self.old, last_sent_at=self.now)
        active_window = AccountChallengeRecipient.objects.create(key='email:window@example.com', last_used_at=self.old, last_sent_at=self.old, window_start=self.now, sent_count=10)
        unknown_namespace = AccountChallengeRecipient.objects.create(key='legacy-without-namespace', last_used_at=self.old)
        self.cleanup(batch_size=1)
        self.assertFalse(PhoneOTPRecipient.objects.filter(pk__in=[stale_phone.pk, never_sent.pk]).exists())
        self.assertFalse(AccountChallengeRecipient.objects.filter(pk=stale_email.pk).exists())
        self.assertEqual(set(PhoneOTPRecipient.objects.values_list('pk', flat=True)), {recent.pk, cooldown.pk})
        self.assertEqual(set(AccountChallengeRecipient.objects.values_list('pk', flat=True)), {active_window.pk, unknown_namespace.pk})

    def test_live_email_phone_and_self_service_challenges_protect_rows(self):
        user = User.objects.create_user(email='live@example.com')
        EmailOTP.objects.create(user=user, email=user.email, code_hash='hash', expires_at=self.now + timedelta(minutes=5))
        AccountChallengeRecipient.objects.create(key=f'email:{user.email}', last_used_at=self.old)
        phone = '+8801712345678'
        PhoneOTPRecipient.objects.create(phone=phone, last_used_at=self.old)
        AccountChallengeRecipient.objects.create(key=f'phone:{phone}', last_used_at=self.old)
        PhoneOTP.objects.create(phone=phone, code_hash='hash', expires_at=self.now + timedelta(minutes=5))
        AccountChallengeRecipient.objects.create(key='email:pending@example.com', last_used_at=self.old)
        AccountChallenge.objects.create(user=user, purpose='email', channel='email', target='pending@example.com',
            code_hash='hash', password_digest='hash', expires_at=self.now + timedelta(minutes=5))
        self.cleanup()
        self.assertEqual(PhoneOTPRecipient.objects.count(), 1)
        self.assertEqual(AccountChallengeRecipient.objects.count(), 3)

    def test_supported_use_refreshes_recipient_retention(self):
        row = PhoneOTPRecipient.objects.create(phone='+8801712345678', last_used_at=self.old)
        with transaction.atomic():
            lock_phone_recipient(row.phone)
        row.refresh_from_db()
        self.assertGreater(row.last_used_at, self.old)
        self.cleanup()
        self.assertTrue(PhoneOTPRecipient.objects.filter(pk=row.pk).exists())

    def test_combined_command_cleans_sessions_and_legacy_otps_without_delivery(self):
        Session.objects.create(session_key='expired', session_data='', expire_date=self.now - timedelta(seconds=1))
        Session.objects.create(session_key='active', session_data='', expire_date=self.now + timedelta(days=1))
        PhoneOTP.objects.create(phone='+8801712345678', code_hash='hash', expires_at=self.now - timedelta(seconds=1))
        with patch('accounts.outbox.send_mail') as email, patch('accounts.outbox.send_sms') as sms:
            call_command('cleanup_auth_artifacts', stdout=io.StringIO())
        email.assert_not_called()
        sms.assert_not_called()
        self.assertEqual(list(Session.objects.values_list('pk', flat=True)), ['active'])
        self.assertFalse(PhoneOTP.objects.exists())

    def test_unsafe_options_are_rejected(self):
        for options in ({'recipient_retention_days': 0}, {'batch_size': 0}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                self.cleanup(**options)


@override_settings(ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES}, OUTBOX_INLINE_DISPATCH=False)
class RecipientPruningConcurrencyTests(TransactionTestCase):
    def cleanup(self):
        call_command('cleanup_account_artifacts', stdout=io.StringIO())

    def test_pruning_skips_a_key_in_use_then_preserves_its_committed_activity(self):
        old = timezone.now() - timedelta(days=40)
        row = PhoneOTPRecipient.objects.create(phone='+8801712345678', last_used_at=old)
        locked, release = Event(), Event()

        def use():
            try:
                with transaction.atomic():
                    lock_phone_recipient(row.phone)
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('Recipient use not released.')
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(use)
            try:
                self.assertTrue(locked.wait(10))
                self.cleanup()
                self.assertTrue(PhoneOTPRecipient.objects.filter(pk=row.pk).exists())
            finally:
                release.set()
            future.result(timeout=10)
        self.cleanup()
        self.assertTrue(PhoneOTPRecipient.objects.filter(pk=row.pk).exists())

    def test_prune_and_parallel_recreation_do_not_split_locks_or_bypass_cooldown(self):
        from .recipient_locks import lock_recipient
        phone = '+8801712345678'
        old = PhoneOTPRecipient.objects.create(phone=phone, last_used_at=timezone.now() - timedelta(days=40))
        deleted, release, writers_waiting = Event(), Event(), Event()
        writers = Barrier(2)
        original_delete = PhoneOTPRecipient.delete

        def pause_delete(row, *args, **kwargs):
            result = original_delete(row, *args, **kwargs)
            deleted.set()
            if not release.wait(10):
                raise AssertionError('Pruning was not released.')
            return result

        def wait_to_lock(*args, **kwargs):
            writers.wait(timeout=10)
            writers_waiting.set()
            return lock_recipient(*args, **kwargs)

        def cleanup():
            try:
                self.cleanup()
            finally:
                connections.close_all()

        def send():
            try:
                return APIClient().post(reverse('send-otp'), {'phone': phone}, format='json').status_code
            finally:
                connections.close_all()

        with patch.object(PhoneOTPRecipient, 'delete', pause_delete), \
                patch('accounts.otp_service.lock_recipient', side_effect=wait_to_lock), ThreadPoolExecutor(max_workers=3) as pool:
            pruning = pool.submit(cleanup)
            try:
                self.assertTrue(deleted.wait(10))
                first, second = pool.submit(send), pool.submit(send)
                self.assertTrue(writers_waiting.wait(10))
                for future in (first, second):
                    with self.assertRaises(TimeoutError):
                        future.result(timeout=0.2)
            finally:
                release.set()
            pruning.result(timeout=10)
            self.assertEqual(sorted([first.result(timeout=10), second.result(timeout=10)]), [202, 429])
        self.assertEqual(PhoneOTPRecipient.objects.count(), 1)
        self.assertNotEqual(PhoneOTPRecipient.objects.get().pk, old.pk)
        self.assertEqual(PhoneOTP.objects.count(), 1)

    def test_delivery_recipient_pruning_skips_an_inflight_reservation(self):
        old = timezone.now() - timedelta(days=40)
        row = AccountChallengeRecipient.objects.create(key='email:busy@example.com', last_used_at=old)
        locked, release = Event(), Event()

        def reserve():
            try:
                with transaction.atomic():
                    reserve_delivery('email', 'busy@example.com')
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('Delivery reservation not released.')
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(reserve)
            try:
                self.assertTrue(locked.wait(10))
                self.cleanup()
                self.assertTrue(AccountChallengeRecipient.objects.filter(pk=row.pk).exists())
            finally:
                release.set()
            future.result(timeout=10)
        self.cleanup()
        self.assertEqual(AccountChallengeRecipient.objects.get(pk=row.pk).sent_count, 1)

    def test_expired_otp_locked_for_renewal_is_skipped_and_new_expiry_is_preserved(self):
        row = PhoneOTP.objects.create(phone='+8801712345678', code_hash='hash', expires_at=timezone.now() - timedelta(seconds=1))
        locked, release = Event(), Event()

        def renew():
            try:
                with transaction.atomic():
                    record = PhoneOTP.objects.select_for_update().get(pk=row.pk)
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('OTP renewal not released.')
                    record.expires_at = timezone.now() + timedelta(minutes=5)
                    record.save(update_fields=['expires_at'])
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(renew)
            try:
                self.assertTrue(locked.wait(10))
                self.cleanup()
                self.assertTrue(PhoneOTP.objects.filter(pk=row.pk).exists())
            finally:
                release.set()
            future.result(timeout=10)
        self.cleanup()
        self.assertTrue(PhoneOTP.objects.filter(pk=row.pk).exists())
