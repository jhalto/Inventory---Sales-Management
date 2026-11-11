from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.models import F, Value
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from asgiref.sync import async_to_sync

from .models import User


class BulkIdentityPolicyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(email='owner@example.com', phone='+8801712345678',
                                             email_verified=True, phone_verified=True)

    def test_update_rejects_contact_and_verification_fields(self):
        for field, value in (('email', 'Mixed@Example.com'), ('phone', '+8801812345678'),
                             ('email_verified', False), ('phone_verified', False)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Bulk identity'):
                User.objects.filter(pk=self.user.pk).update(**{field: value})
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'owner@example.com')
        self.assertTrue(self.user.email_verified)

    def test_expressions_and_base_manager_cannot_bypass_identity_policy(self):
        for manager in (User.objects, User._base_manager):
            for expression in (F('email'), Value('changed@example.com')):
                with self.subTest(expression=type(expression).__name__), self.assertRaises(ValueError):
                    manager.filter(pk=self.user.pk).update(email=expression)

    def test_bulk_update_rejects_identities_and_flags(self):
        for field, value in (('email', 'new@example.com'), ('phone', '+8801812345678'),
                             ('email_verified', False), ('phone_verified', False)):
            with self.subTest(field=field):
                setattr(self.user, field, value)
                with self.assertRaisesRegex(ValueError, 'Bulk identity'):
                    User.objects.bulk_update([self.user], [field])
                self.user.refresh_from_db()

    def test_bulk_create_is_explicitly_unsupported(self):
        with self.assertRaisesRegex(ValueError, 'Bulk user creation'):
            User.objects.bulk_create([User(email='new@example.com')])
        self.assertEqual(User.objects.count(), 1)

    def test_async_bulk_operations_enforce_the_same_policy(self):
        with self.assertRaises(ValueError):
            async_to_sync(User.objects.filter(pk=self.user.pk).aupdate)(email='changed@example.com')
        self.user.email = 'changed@example.com'
        with self.assertRaises(ValueError):
            async_to_sync(User.objects.all().abulk_update)([self.user], ['email'])
        with self.assertRaises(ValueError):
            async_to_sync(User.objects.all().abulk_create)([User(email='new@example.com')])

    def test_nonidentity_bulk_updates_still_work(self):
        self.assertEqual(User.objects.filter(pk=self.user.pk).update(first_name='Updated'), 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, 'Updated')
        self.user.last_name = 'Bulk updated'
        self.assertEqual(User.objects.bulk_update([self.user], iter(['last_name'])), 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_name, 'Bulk updated')

    def test_database_rejects_noncanonical_emails_even_through_sql(self):
        for email in ('OWNER@example.com', ' owner@example.com', 'owner@example.com ',
                      'owner@example.com\n', 'owner\t@example.com', ''):
            with self.subTest(email=email), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('UPDATE accounts_user SET email = %s WHERE id = %s', [email, self.user.pk])

    def test_canonical_null_emails_allow_phone_only_accounts(self):
        user = User.objects.create_user(phone='+8801812345678')
        self.assertIsNone(user.email)
        self.assertTrue(user.pk)

    def test_supported_identity_save_normalizes_and_resets_verification(self):
        self.user.email = ' Changed@Example.COM '
        self.user.save(update_fields=['email'])
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'changed@example.com')
        self.assertFalse(self.user.email_verified)
        self.assertEqual(User.objects.get_by_natural_key(' CHANGED@EXAMPLE.COM '), self.user)


class CanonicalEmailMigrationTests(TransactionTestCase):
    before = [('accounts', '0009_googlebrowserchallenge')]
    after = [('accounts', '0010_alter_user_options_and_more')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.old_user = executor.loader.project_state(self.before).apps.get_model('accounts', 'User')
        self.addCleanup(self.restore_schema)

    def restore_schema(self):
        self.old_user.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_historical_repairs_still_work_and_normalize_legacy_rows(self):
        user = self.old_user.objects.create(email='Mixed@Example.COM', password='!')
        self.assertEqual(self.old_user.objects.get_by_natural_key(' MIXED@EXAMPLE.COM ').pk, user.pk)
        self.old_user.objects.filter(pk=user.pk).update(email=' Another@Example.COM ')
        MigrationExecutor(connection).migrate(self.after)
        self.assertEqual(User.objects.get(pk=user.pk).email, 'another@example.com')
        self.assertEqual(User.objects.count(), 1)

    def test_migration_refuses_invalid_internal_whitespace_without_modifying_it(self):
        user = self.old_user.objects.create(email='bad address@example.com', password='!')
        with self.assertRaisesRegex(RuntimeError, 'invalid whitespace account IDs'):
            MigrationExecutor(connection).migrate(self.after)
        self.assertEqual(self.old_user.objects.get(pk=user.pk).email, 'bad address@example.com')
