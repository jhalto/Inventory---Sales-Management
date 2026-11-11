from django.test import TestCase
from django.urls import reverse
from django.contrib.auth.forms import ReadOnlyPasswordHashField

from .forms import AccountChangeForm, AccountCreationForm
from .models import User


class AccountAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email='admin@example.com', password='AdminTestPass!9427',
        )
        self.client.force_login(self.admin)
        self.password = 'AccountTestPass!5826'

    def test_admin_add_hashes_password(self):
        response = self.client.post(reverse('admin:accounts_user_add'), {
            'email': 'Created@example.com', 'phone': '', 'role': User.Role.CUSTOMER,
            'usable_password': 'true', 'password1': self.password, 'password2': self.password,
            '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(email='created@example.com')
        self.assertNotEqual(user.password, self.password)
        self.assertTrue(user.check_password(self.password))

    def test_password_change_endpoint_hashes_new_password(self):
        user = User.objects.create_user(email='person@example.com', password=self.password)
        new_password = 'ReplacementTestPass!8142'
        response = self.client.post(reverse('admin:auth_user_password_change', args=[user.pk]), {
            'usable_password': 'true', 'password1': new_password, 'password2': new_password,
        })
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertNotEqual(user.password, new_password)
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(user.check_password(self.password))

    def test_profile_change_cannot_overwrite_password_hash(self):
        user = User.objects.create_user(email='person@example.com', password=self.password)
        original_hash = user.password
        response = self.client.get(reverse('admin:accounts_user_change', args=[user.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.context['adminform'].form.fields['password'], ReadOnlyPasswordHashField)
        response = self.client.post(reverse('admin:accounts_user_change', args=[user.pk]), {
            'email': user.email, 'phone': '', 'role': User.Role.CUSTOMER,
            'first_name': 'Updated', 'password': 'plaintext-injection', 'is_active': 'on',
            '_save': 'Save',
        })
        self.assertEqual(response.status_code, 302)
        user.refresh_from_db()
        self.assertEqual(user.first_name, 'Updated')
        self.assertEqual(user.password, original_hash)
        self.assertTrue(user.check_password(self.password))

    def test_creation_rejects_mismatched_and_weak_passwords(self):
        for first, second in [(self.password, 'DifferentTestPass!9274'), ('short', 'short')]:
            with self.subTest(passwords_match=first == second):
                form = AccountCreationForm(data={
                    'email': 'new@example.com', 'usable_password': 'true',
                    'password1': first, 'password2': second,
                })
                self.assertFalse(form.is_valid())
                self.assertIn('password2', form.errors)

    def test_phone_only_accounts_remain_editable(self):
        user = User.objects.create_user(phone='+8801712345678')
        form = AccountChangeForm(data={
            'email': '', 'phone': user.phone, 'role': User.Role.CUSTOMER,
            'date_joined': user.date_joined, 'is_active': True,
        }, instance=user)
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertIsNone(updated.email)
        self.assertFalse(updated.has_usable_password())

    def test_admin_add_page_and_duplicate_email_validation(self):
        self.assertEqual(self.client.get(reverse('admin:accounts_user_add')).status_code, 200)
        form = AccountCreationForm(data={
            'email': 'ADMIN@example.com', 'usable_password': 'true',
            'password1': self.password, 'password2': self.password,
        })
        self.assertFalse(form.is_valid())
        self.assertIn('email', form.errors)
