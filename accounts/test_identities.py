import io
import os
from unittest.mock import patch

from django.contrib.auth import authenticate
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from .forms import AccountCreationForm, AccountChangeForm
from .models import User
from .serializers import LoginSerializer, UserSerializers, VerifyEmailOTPSerializer


class EmailNormalizationTests(TestCase):
    password = 'IdentityTestPass!8247'

    def test_manager_and_authentication_share_normalization(self):
        user = User.objects.create_user(email=' MixedCase@Example.COM ', password=self.password)
        self.assertEqual(user.email, 'mixedcase@example.com')
        self.assertEqual(User.objects.get_by_natural_key(' MIXEDCASE@EXAMPLE.COM '), user)
        self.assertEqual(authenticate(email=' MIXEDCASE@EXAMPLE.COM ', password=self.password), user)

    def test_empty_lookup_cannot_match_phone_only_account(self):
        User.objects.create_user(phone='+8801712345678', password=self.password)
        with self.assertRaises(User.DoesNotExist):
            User.objects.get_by_natural_key('   ')

    def test_createsuperuser_command_and_admin_login_accept_mixed_case(self):
        with patch.dict(os.environ, {'DJANGO_SUPERUSER_PASSWORD': self.password}):
            call_command('createsuperuser', email='CommandUser@Example.COM', interactive=False, stdout=io.StringIO())
        user = User.objects.get(email='commanduser@example.com')
        self.assertTrue(user.is_superuser)
        response = self.client.post(reverse('admin:login'), {
            'username': 'COMMANDUSER@EXAMPLE.COM', 'password': self.password,
            'next': reverse('admin:index'),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_admin_creation_and_update_normalize_email(self):
        form = AccountCreationForm(data={
            'email': ' AdminCreated@Example.COM ', 'usable_password': 'true',
            'password1': self.password, 'password2': self.password,
        })
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertEqual(user.email, 'admincreated@example.com')
        form = AccountChangeForm(instance=user, data={
            'email': ' AdminUpdated@Example.COM ', 'phone': '', 'role': 'customer',
            'date_joined': user.date_joined, 'is_active': True,
        })
        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertEqual(updated.email, 'adminupdated@example.com')
        self.assertEqual(authenticate(email='ADMINUPDATED@EXAMPLE.COM', password=self.password), updated)

    def test_api_serializers_share_normalization(self):
        for serializer_type, extra in (
            (UserSerializers, {'password': self.password}),
            (LoginSerializer, {'password': self.password}),
            (VerifyEmailOTPSerializer, {'otp': '000123'}),
        ):
            with self.subTest(serializer=serializer_type.__name__):
                serializer = serializer_type(data={'email': ' ApiUser@Example.COM ', **extra})
                self.assertTrue(serializer.is_valid(), serializer.errors)
                self.assertEqual(serializer.validated_data['email'], 'apiuser@example.com')
