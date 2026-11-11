from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TransactionTestCase

from .models import User


class AsyncAccountCreationTests(TransactionTestCase):
    async def test_email_creation_normalizes_hashes_and_preserves_extra_fields(self):
        password = ' AsyncCreationTest!8247 '
        user = await User.objects.db_manager('default').acreate_user(
            email=' AsyncUser@Example.COM ', password=password, first_name='Async',
        )
        stored = await User.objects.aget(pk=user.pk)
        self.assertEqual(stored.email, 'asyncuser@example.com')
        self.assertEqual(stored.first_name, 'Async')
        self.assertFalse(stored.is_staff)
        self.assertFalse(stored.is_superuser)
        self.assertNotEqual(stored.password, password)
        self.assertTrue(await stored.acheck_password(password))
        self.assertFalse(await stored.acheck_password(password.strip()))
        self.assertEqual(await User.objects.aget_by_natural_key(' ASYNCUSER@EXAMPLE.COM '), stored)

    async def test_phone_only_creation_normalizes_and_sets_unusable_password(self):
        user = await User.objects.acreate_user(phone='01712345678')
        await user.arefresh_from_db()
        self.assertIsNone(user.email)
        self.assertEqual(user.phone, '+8801712345678')
        self.assertFalse(user.has_usable_password())

    async def test_invalid_or_missing_identity_does_not_create_account(self):
        for values in ({}, {'email': '  ', 'phone': ''}):
            with self.subTest(values=values), self.assertRaisesMessage(ValueError, 'Email or phone is required'):
                await User.objects.acreate_user(**values)
        with self.assertRaises(ValidationError):
            await User.objects.acreate_user(phone='not-a-phone')
        self.assertEqual(await User.objects.acount(), 0)

    async def test_superuser_creation_uses_email_signature_and_admin_defaults(self):
        user = await User.objects.acreate_superuser(
            email=' AsyncAdmin@Example.COM ', password='AdminCreationTest!8247',
        )
        await user.arefresh_from_db()
        self.assertEqual(user.email, 'asyncadmin@example.com')
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertEqual(user.role, User.Role.ADMIN)
        self.assertTrue(await user.acheck_password('AdminCreationTest!8247'))

    async def test_superuser_validation_matches_synchronous_creation(self):
        for values, message in (
            ({'email': '', 'password': 'secret'}, 'Superuser requires email and password'),
            ({'email': 'admin@example.com', 'password': None}, 'Superuser requires email and password'),
            ({'email': 'admin@example.com', 'password': 'secret', 'is_staff': False}, 'Superuser must have is_staff=True'),
            ({'email': 'admin@example.com', 'password': 'secret', 'is_superuser': False}, 'Superuser must have is_superuser=True'),
        ):
            with self.subTest(values=values), self.assertRaisesMessage(ValueError, message):
                await User.objects.acreate_superuser(**values)
        self.assertEqual(await User.objects.acount(), 0)

    async def test_duplicate_email_is_rejected_without_extra_account(self):
        await User.objects.acreate_user(email='owner@example.com')
        with self.assertRaises(IntegrityError):
            await User.objects.acreate_user(email=' OWNER@Example.COM ')
        self.assertEqual(await User.objects.acount(), 1)

    def test_async_creation_is_marked_as_mutating(self):
        self.assertTrue(User.objects.acreate_user.alters_data)
        self.assertTrue(User.objects.acreate_superuser.alters_data)
