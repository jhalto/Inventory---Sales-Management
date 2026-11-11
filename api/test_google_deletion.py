from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import connection, connections
from django.db.models.query import QuerySet
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import GoogleBrowserChallenge, GoogleIdentity, User
from accounts.throttles import DEFAULT_RATES


@skipUnless(connection.vendor == 'postgresql', 'Requires independent PostgreSQL transactions')
@override_settings(GOOGLE_CLIENT_ID='deletion-test.apps.googleusercontent.com',
                   ACCOUNT_THROTTLE_RATES={scope: None for scope in DEFAULT_RATES})
class GoogleUserDeletionTests(TransactionTestCase):
    def exercise_deletion(self, route, *, linking=False):
        user = User.objects.create_user(email='deletion@gmail.com', email_verified=True)
        identity = {'subject': 'deletion-subject', 'email': user.email}
        if not linking:
            GoogleIdentity.objects.create(user=user, subject=identity['subject'])
        client = APIClient(enforce_csrf_checks=True)
        headers = {}
        if 'mobile' not in route:
            config = client.get(reverse('google-config')).json()
            headers['HTTP_X_CSRFTOKEN'] = config['csrf_token']
        if linking:
            client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
        identity_loaded, deleted = Event(), Event()
        original_first = QuerySet.first
        observed = []

        def pause_after_identity_lookup(queryset):
            result = original_first(queryset)
            if queryset.model is GoogleIdentity:
                observed.append(result.pk if result else None)
                identity_loaded.set()
                if not deleted.wait(15):
                    raise AssertionError('Deletion did not commit before user lock lookup.')
            return result

        def exchange():
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                response = client.post(reverse(route), {'credential': 'synthetic-google-token'}, format='json', **headers)
                return response, pid
            finally:
                connections.close_all()

        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_backend_pid()')
            deleting_pid = cursor.fetchone()[0]
        with patch('api.views.accounts.google.verify_google_token', return_value=identity), \
                patch('api.views.accounts.google.verify_google_mobile_token', return_value=identity), \
                patch('api.views.accounts.google.RefreshToken.for_user') as mint, \
                patch.object(QuerySet, 'first', pause_after_identity_lookup), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(exchange)
            try:
                self.assertTrue(identity_loaded.wait(15))
                User.objects.filter(pk=user.pk).delete()  # Commits on the other connection.
                self.assertFalse(User.objects.filter(pk=user.pk).exists())
            finally:
                deleted.set()
            response, exchange_pid = future.result(timeout=20)
        self.assertNotEqual(deleting_pid, exchange_pid)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0] is None, linking)
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data['code'], 'account_unavailable')
        self.assertNotIn('data', response.data)
        mint.assert_not_called()
        self.assertFalse(User.objects.exists())
        self.assertFalse(GoogleIdentity.objects.exists())
        if 'mobile' not in route:
            self.assertIsNone(GoogleBrowserChallenge.objects.get().consumed_at)

    def test_browser_login_denies_user_deleted_after_identity_lookup(self):
        self.exercise_deletion('google-login')

    def test_native_login_denies_user_deleted_after_identity_lookup(self):
        self.exercise_deletion('google-mobile-login')

    def test_browser_link_denies_authenticated_user_deleted_before_row_lock(self):
        self.exercise_deletion('google-link', linking=True)

    def test_native_link_denies_authenticated_user_deleted_before_row_lock(self):
        self.exercise_deletion('google-mobile-link', linking=True)
