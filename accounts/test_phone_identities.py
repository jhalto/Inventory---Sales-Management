from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .forms import AccountCreationForm, AccountChangeForm
from .identities import normalize_phone
from .models import User, PhoneOTP
from .otp_service import generate_otp, hash_otp


class PhoneIdentityTests(TestCase):
    local = '01712345678'
    canonical = '+8801712345678'

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_manager_model_clean_and_partial_save_share_normalization(self):
        user = User.objects.create_user(phone=self.local)
        self.assertEqual(user.phone, self.canonical)
        user.phone = '01812345678'
        user.save(update_fields=['first_name'])
        user.refresh_from_db()
        self.assertEqual(user.phone, '+8801812345678')
        user.phone = self.local
        user.full_clean()
        self.assertEqual(user.phone, self.canonical)

    def test_local_and_international_formats_cannot_create_two_accounts(self):
        User.objects.create_user(phone=self.local)
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(phone=self.canonical)
        self.assertEqual(User.objects.count(), 1)

    def test_bulk_update_cannot_store_noncanonical_phone(self):
        user = User.objects.create_user(phone=self.canonical)
        with self.assertRaises(ValueError):
            User.objects.filter(pk=user.pk).update(phone=self.local)
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('UPDATE accounts_user SET phone = %s WHERE id = %s', [self.local, user.pk])

    def test_admin_creation_update_and_duplicate_validation(self):
        form = AccountCreationForm(data={
            'email': 'admincreated@example.com', 'phone': self.local,
            'usable_password': 'true', 'password1': 'StrongTestPass!8247', 'password2': 'StrongTestPass!8247',
        })
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.phone, self.canonical)
        form = AccountChangeForm(instance=user, data={
            'email': user.email, 'phone': '01812345678', 'role': user.role,
            'date_joined': user.date_joined, 'is_active': True,
        })
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().phone, '+8801812345678')
        other = User.objects.create_user(email='other@example.com')
        form = AccountChangeForm(instance=other, data={
            'email': other.email, 'phone': '01812345678', 'role': other.role,
            'date_joined': other.date_joined, 'is_active': True,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('phone', form.errors)

    def test_blank_optional_phone_and_invalid_numbers(self):
        user = User.objects.create_user(email='emailonly@example.com', phone='   ')
        self.assertIsNone(user.phone)
        for phone in ('invalid', '+14155552671', 1712345678):
            with self.subTest(phone=phone), self.assertRaises(ValidationError):
                normalize_phone(phone)
        with self.assertRaises(ValueError):
            User.objects.create_user(phone='   ')

    def test_both_phone_formats_log_into_existing_staff_account(self):
        self.assertEqual(hash_otp(self.local, '000123'), hash_otp(self.canonical, '000123'))
        user = User.objects.create_user(phone=self.local, role=User.Role.STAFF)
        client = APIClient()
        for phone in (self.local, self.canonical):
            with self.subTest(phone=phone):
                otp = generate_otp(phone)
                self.assertEqual(PhoneOTP.objects.get().phone, self.canonical)
                response = client.post(reverse('verify-otp'), {'phone': phone, 'otp': otp})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data['data']['user']['id'], user.pk)
                self.assertEqual(response.data['data']['user']['role'], User.Role.STAFF)
        self.assertEqual(User.objects.count(), 1)


class PhoneNormalizationMigrationTests(TransactionTestCase):
    before = [('accounts', '0007_googleidentity')]
    after = [('accounts', '0008_user_accounts_user_phone_canonical')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.old_user = executor.loader.project_state(self.before).apps.get_model('accounts', 'User')
        self.addCleanup(self.restore_schema)

    def restore_schema(self):
        self.old_user.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_migration_normalizes_without_losing_accounts(self):
        user = self.old_user.objects.create(phone='01712345678', password='!')
        blank = self.old_user.objects.create(email='emailonly@example.com', phone='', password='!')
        MigrationExecutor(connection).migrate(self.after)
        self.assertEqual(User.objects.get(pk=user.pk).phone, '+8801712345678')
        self.assertIsNone(User.objects.get(pk=blank.pk).phone)
        self.assertEqual(User.objects.count(), 2)

    def test_migration_stops_before_rewriting_colliding_accounts(self):
        first = self.old_user.objects.create(phone='01712345678', password='!')
        self.old_user.objects.create(phone='+8801712345678', password='!')
        with self.assertRaisesRegex(RuntimeError, 'colliding account IDs'):
            MigrationExecutor(connection).migrate(self.after)
        self.assertEqual(self.old_user.objects.get(pk=first.pk).phone, '01712345678')
        self.assertEqual(self.old_user.objects.count(), 2)

    def test_migration_stops_on_invalid_phone(self):
        user = self.old_user.objects.create(phone='not-a-phone', password='!')
        with self.assertRaisesRegex(RuntimeError, 'invalid account IDs'):
            MigrationExecutor(connection).migrate(self.after)
        self.assertEqual(self.old_user.objects.get(pk=user.pk).phone, 'not-a-phone')
