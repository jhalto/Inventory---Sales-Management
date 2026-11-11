import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from unittest import skipUnless
from datetime import timedelta

from django.contrib.admin.models import LogEntry, CHANGE
from django.contrib.auth.models import Permission
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from django.core import mail
from django.core.cache import cache

from .email_service import hash_email_otp, send_verification_email
from .models import User, EmailOTP, PhoneOTP, PhoneOTPRecipient
from .otp_service import hash_otp, generate_otp, lock_phone_recipient
from .throttles import DEFAULT_RATES


class IdentityVerificationStateTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            email='owner@example.com', phone='+8801712345678',
            email_verified=True, phone_verified=True,
        )
        self.email_otp = EmailOTP.objects.create(
            user=self.user, email=self.user.email,
            code_hash=hash_email_otp(self.user.pk, self.user.email, '000123'),
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        self.phone_otp = PhoneOTP.objects.create(
            phone=self.user.phone, code_hash=hash_otp(self.user.phone, '000123'),
            expires_at=timezone.now() + timedelta(minutes=5),
        )

    def test_email_change_resets_only_email_and_invalidates_email_challenge(self):
        self.user.email = 'changed@example.com'
        self.user.save(update_fields=['email'])
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertTrue(self.user.phone_verified)
        self.assertFalse(EmailOTP.objects.exists())
        self.assertTrue(PhoneOTP.objects.filter(pk=self.phone_otp.pk).exists())

    def test_phone_change_resets_only_phone_and_invalidates_old_and_new_challenges(self):
        new_phone = '+8801812345678'
        PhoneOTP.objects.create(phone=new_phone, code_hash='unused', expires_at=timezone.now() + timedelta(minutes=5))
        old_recipient = PhoneOTPRecipient.objects.get(phone=self.user.phone)
        old_recipient.last_sent_at = timezone.now()
        old_recipient.save()
        self.user.phone = new_phone
        self.user.save(update_fields=['phone'])
        self.user.refresh_from_db()
        self.assertFalse(self.user.phone_verified)
        self.assertTrue(self.user.email_verified)
        self.assertFalse(PhoneOTP.objects.exists())
        self.assertTrue(EmailOTP.objects.exists())
        old_recipient.refresh_from_db()
        self.assertIsNotNone(old_recipient.last_sent_at)

    def test_cleared_identities_cannot_remain_verified(self):
        self.user.email = None
        self.user.save()
        self.assertFalse(self.user.email_verified)
        self.assertTrue(self.user.phone_verified)
        self.assertFalse(EmailOTP.objects.exists())
        self.assertTrue(PhoneOTP.objects.exists())
        # Restore a contact before clearing the other: accounts must retain one.
        self.user.email = 'replacement@example.com'
        self.user.phone = None
        self.user.save()
        self.assertFalse(self.user.phone_verified)
        self.assertFalse(PhoneOTP.objects.exists())

    def test_format_only_changes_preserve_verification_and_challenges(self):
        self.user.email = ' OWNER@Example.COM '
        self.user.phone = '01712345678'
        self.user.save()
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertTrue(self.user.phone_verified)
        self.assertTrue(EmailOTP.objects.exists())
        self.assertTrue(PhoneOTP.objects.exists())

    def test_stale_partial_profile_save_does_not_restore_verification(self):
        stale = User.objects.get(pk=self.user.pk)
        self.user.email = 'changed@example.com'
        self.user.save(update_fields=['email'])
        stale.first_name = 'Updated'
        stale.save(update_fields=['first_name'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'changed@example.com')
        self.assertFalse(self.user.email_verified)

    def test_conflicting_identity_change_rolls_back_flags_and_challenges(self):
        User.objects.create_user(email='taken@example.com')
        self.user.email = 'taken@example.com'
        self.user.phone = '+8801912345678'
        with self.assertRaises(IntegrityError):
            self.user.save()
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'owner@example.com')
        self.assertTrue(self.user.email_verified)
        self.assertTrue(self.user.phone_verified)
        self.assertTrue(EmailOTP.objects.exists())
        self.assertTrue(PhoneOTP.objects.exists())

    @override_settings(MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}})
    def test_old_code_fails_and_new_email_can_be_reverified(self):
        self.user.email = 'changed@example.com'
        self.user.save()
        client = APIClient()
        self.assertEqual(client.post(reverse('verify-email'), {'email': self.user.email, 'otp': '000123'}).status_code, 400)
        send_verification_email(self.user)
        otp = re.search(r'\b[0-9]{6}\b', mail.outbox[-1].body).group()
        self.assertEqual(client.post(reverse('verify-email'), {'email': self.user.email, 'otp': otp}).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)

    def test_old_phone_code_fails_and_new_phone_can_be_reverified(self):
        old_phone = self.user.phone
        self.user.phone = '+8801812345678'
        self.user.save(update_fields=['phone'])
        client = APIClient()
        self.assertEqual(client.post(reverse('verify-otp'), {'phone': old_phone, 'otp': '000123'}).status_code, 400)
        self.assertEqual(User.objects.count(), 1)
        otp = generate_otp(self.user.phone)
        self.assertEqual(client.post(reverse('verify-otp'), {'phone': self.user.phone, 'otp': otp}).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.phone_verified)


class AdminManualVerificationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(email='admin@example.com', password='AdminTestPass!8247')
        self.user = User.objects.create_user(email='owner@example.com', phone='+8801712345678', email_verified=True, phone_verified=True)
        self.client.force_login(self.admin)
        self.url = reverse('admin:accounts_user_verify_identity', args=[self.user.pk])

    def payload(self, identity='email', **extra):
        return {'identity': identity, 'reason': 'Ownership confirmed with signed support evidence.',
                'email_snapshot': self.user.email or '', 'phone_snapshot': self.user.phone or '', **extra}

    def test_normal_admin_edit_resets_flags_despite_submitted_checkboxes(self):
        url = reverse('admin:accounts_user_change', args=[self.user.pk])
        response = self.client.get(url)
        form = response.context['adminform'].form
        self.assertNotIn('email_verified', form.fields)
        self.assertNotIn('phone_verified', form.fields)
        response = self.client.post(url, {
            'email': 'changed@example.com', 'phone': '+8801812345678', 'role': 'customer',
            'is_active': 'on', 'email_verified': 'on', 'phone_verified': 'on', '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertFalse(self.user.phone_verified)

    def test_explicit_email_override_records_actor_reason_and_target(self):
        self.user.email_verified = False
        self.user.save(update_fields=['email_verified'])
        EmailOTP.objects.create(user=self.user, email=self.user.email, code_hash='unused', expires_at=timezone.now() + timedelta(minutes=5))
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertFalse(EmailOTP.objects.exists())
        entry = LogEntry.objects.get(action_flag=CHANGE, object_id=str(self.user.pk))
        self.assertEqual(entry.user, self.admin)
        self.assertIn(self.user.email, entry.change_message)
        self.assertIn('signed support evidence', entry.change_message)

    def test_phone_override_invalidates_challenge_and_preserves_cooldown_row(self):
        self.user.phone_verified = False
        self.user.save(update_fields=['phone_verified'])
        PhoneOTP.objects.create(phone=self.user.phone, code_hash='unused', expires_at=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.client.post(self.url, self.payload('phone')).status_code, 302)
        self.user.refresh_from_db()
        self.assertTrue(self.user.phone_verified)
        self.assertFalse(PhoneOTP.objects.exists())
        self.assertTrue(PhoneOTPRecipient.objects.filter(phone=self.user.phone).exists())

    def test_override_requires_reason_and_matching_snapshot(self):
        self.user.email_verified = False
        self.user.save(update_fields=['email_verified'])
        for extra in ({'reason': ''}, {'email_snapshot': 'stale@example.com'}):
            with self.subTest(extra=extra):
                response = self.client.post(self.url, self.payload(**extra))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
        self.user.refresh_from_db()
        self.assertFalse(self.user.email_verified)
        self.assertFalse(LogEntry.objects.filter(object_id=str(self.user.pk)).exists())

    def test_non_superuser_cannot_manually_verify(self):
        staff = User.objects.create_user(email='staff@example.com', is_staff=True)
        staff.user_permissions.add(Permission.objects.get(content_type__app_label='accounts', codename='change_user'))
        self.client.force_login(staff)
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 403)

    def test_manual_override_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.url, self.payload()).status_code, 403)


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks')
@override_settings(ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class IdentityChangeConcurrencyTests(TransactionTestCase):
    def test_phone_edit_waits_for_active_check_then_resets_new_identity(self):
        user = User.objects.create_user(phone='+8801712345678', phone_verified=True)
        code = generate_otp(user.phone)
        locked, release, editing = Event(), Event(), Event()

        def check():
            try:
                with transaction.atomic():
                    lock_phone_recipient(user.phone)
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('Timed out waiting for test release.')
                    response = APIClient().post(reverse('verify-otp'), {'phone': user.phone, 'otp': code})
                    return response.status_code
            finally:
                connections.close_all()

        def edit():
            try:
                account = User.objects.get(pk=user.pk)
                account.phone = '+8801812345678'
                editing.set()
                account.save(update_fields=['phone'])
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            check_future = pool.submit(check)
            try:
                self.assertTrue(locked.wait(10))
                edit_future = pool.submit(edit)
                self.assertTrue(editing.wait(10))
                with self.assertRaises(TimeoutError):
                    edit_future.result(timeout=0.2)
            finally:
                release.set()
            self.assertEqual(check_future.result(timeout=10), 200)
            edit_future.result(timeout=10)
        user.refresh_from_db()
        self.assertEqual(user.phone, '+8801812345678')
        self.assertFalse(user.phone_verified)
