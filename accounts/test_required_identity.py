from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from .forms import AccountChangeForm
from .models import GoogleIdentity, User


class RequiredIdentityTests(TestCase):
    def test_creation_and_clean_reject_absent_or_blank_identities(self):
        for values in ({}, {'email': '  ', 'phone': ''}):
            with self.subTest(values=values):
                with self.assertRaisesMessage(ValidationError, 'An email address or phone number is required.'):
                    User.objects.create(password='!', **values)
                with self.assertRaises(ValidationError):
                    User(**values).full_clean()
        self.assertFalse(User.objects.exists())

    def test_supported_identity_states_include_google_email_and_phone_only(self):
        email_user = User.objects.create(email=' Email@Example.com ', password='!')
        phone_user = User.objects.create(phone='01712345678', password='!')
        google_user = User.objects.create_user(email='google@example.com', email_verified=True)
        GoogleIdentity.objects.create(user=google_user, subject='required-identity-test')
        for user in (email_user, phone_user, google_user):
            user.full_clean()
        self.assertEqual(email_user.email, 'email@example.com')
        self.assertEqual(phone_user.phone, '+8801712345678')

    def test_cannot_remove_last_identity_even_with_google_link(self):
        user = User.objects.create_user(email='google@example.com')
        GoogleIdentity.objects.create(user=user, subject='linked-test')
        user.email = None
        with self.assertRaises(ValidationError):
            user.save(update_fields=['email'])
        user.refresh_from_db()
        self.assertEqual(user.email, 'google@example.com')

    def test_partial_save_validates_persisted_identity_not_unsaved_values(self):
        user = User.objects.create_user(email='owner@example.com')
        user.email = None
        user.first_name = 'Changed'
        user.save(update_fields=['first_name'])
        user.phone = '+8801712345678'
        with self.assertRaises(ValidationError):
            user.save(update_fields=['email'])
        user.refresh_from_db()
        self.assertEqual(user.email, 'owner@example.com')
        self.assertEqual(user.first_name, 'Changed')
        user.phone = '+8801712345678'
        user.email = None
        user.save(update_fields=['email', 'phone'])
        user.refresh_from_db()
        self.assertIsNone(user.email)
        self.assertEqual(user.phone, '+8801712345678')

    def test_admin_change_rejects_removal_of_last_identity(self):
        user = User.objects.create_user(email='owner@example.com')
        form = AccountChangeForm(instance=user, data={
            'email': '', 'phone': '', 'role': 'customer',
            'date_joined': user.date_joined, 'is_active': True,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('An email address or phone number is required.', form.non_field_errors())

    def test_database_rejects_identityless_rows_even_without_model_hooks(self):
        user = User.objects.create_user(email='owner@example.com')
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('UPDATE accounts_user SET email = NULL, phone = NULL WHERE id = %s', [user.pk])


class RequiredIdentityMigrationTests(TransactionTestCase):
    before = [('accounts', '0010_alter_user_options_and_more')]
    after = [('accounts', '0011_user_accounts_user_identity_required')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.old_user = executor.loader.project_state(self.before).apps.get_model('accounts', 'User')
        self.addCleanup(self.restore_schema)

    def restore_schema(self):
        self.old_user.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_migration_stops_for_review_without_deleting_accounts(self):
        user = self.old_user.objects.create(password='!')
        with self.assertRaisesRegex(RuntimeError, f'account IDs \\[{user.pk}\\]'):
            MigrationExecutor(connection).migrate(self.after)
        self.assertTrue(self.old_user.objects.filter(pk=user.pk).exists())
        self.old_user.objects.filter(pk=user.pk).update(email='reviewed@example.com')
        MigrationExecutor(connection).migrate(self.after)
        self.assertEqual(User.objects.get(pk=user.pk).email, 'reviewed@example.com')
