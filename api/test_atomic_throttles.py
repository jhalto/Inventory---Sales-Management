from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import io
from threading import Barrier, Event
from concurrent.futures import TimeoutError
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection, connections, OperationalError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import AccessToken

from accounts.models import AccountChallengeRecipient, EmailOTP, ThrottleBucket, User
from accounts.throttles import DEFAULT_RATES, LoginIPThrottle

NO_LIMITS = {scope: None for scope in DEFAULT_RATES}


class AtomicThrottleTests(TestCase):
    def request(self):
        return Request(APIRequestFactory().get('/', REMOTE_ADDR='192.0.2.1'))

    @override_settings(ACCOUNT_THROTTLE_RATES={**NO_LIMITS, 'login_ip': '2/minute'})
    def test_rolling_window_boundary_and_retry_after(self):
        for now, allowed, wait in ((1000, True, None), (1030, True, None), (1059, False, 1),
                                   (1060, True, None), (1061, False, 29)):
            with self.subTest(now=now), patch('accounts.throttles.AccountThrottle.timer', return_value=now):
                throttle = LoginIPThrottle()
                self.assertEqual(throttle.allow_request(self.request(), None), allowed)
                self.assertEqual(throttle.wait(), wait)
        self.assertEqual(ThrottleBucket.objects.get().history, [1030, 1060])

    @override_settings(ACCOUNT_THROTTLE_RATES=NO_LIMITS)
    def test_disabled_throttle_does_not_query_database(self):
        with self.assertNumQueries(0):
            self.assertTrue(LoginIPThrottle().allow_request(self.request(), None))

    @override_settings(ACCOUNT_THROTTLE_RATES={**NO_LIMITS, 'login_ip': '1/hour'})
    def test_cache_clear_or_replacement_cannot_reset_limit(self):
        self.assertTrue(LoginIPThrottle().allow_request(self.request(), None))
        cache.clear()
        with override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}}):
            self.assertFalse(LoginIPThrottle().allow_request(self.request(), None))
        self.assertEqual(len(ThrottleBucket.objects.get().history), 1)

    def test_database_failure_blocks_api_before_authentication(self):
        with patch('accounts.throttles.ThrottleBucket.objects.select_for_update', side_effect=OperationalError('offline')):
            with patch('api.views.accounts.views.authenticate') as authenticate:
                response = APIClient().post(reverse('login'), {'email': 'person@example.com', 'password': 'wrong'})
        self.assertEqual(response.status_code, 503)
        authenticate.assert_not_called()

    @override_settings(GOOGLE_CLIENT_ID='atomic.apps.googleusercontent.com')
    def test_database_failure_blocks_google_get_before_session_creation(self):
        with patch('accounts.throttles.ThrottleBucket.objects.select_for_update', side_effect=OperationalError('offline')):
            response = APIClient().get(reverse('google-config'))
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.cookies)

    def test_cleanup_preserves_live_buckets_and_delivery_cooldowns(self):
        now = timezone.now()
        ThrottleBucket.objects.create(key='expired', history=[], expires_at=now - timedelta(seconds=1))
        ThrottleBucket.objects.create(key='active', history=[now.timestamp()], expires_at=now + timedelta(hours=1))
        AccountChallengeRecipient.objects.create(key='email:person@example.com', last_sent_at=now, sent_count=1)
        call_command('cleanup_throttle_buckets', stdout=io.StringIO())
        self.assertEqual(list(ThrottleBucket.objects.values_list('key', flat=True)), ['active'])
        self.assertTrue(AccountChallengeRecipient.objects.exists())

    def test_service_cooldown_is_shared_with_self_service_delivery(self):
        from accounts.email_service import send_verification_email
        from accounts.self_service import issue_challenge, locked_user
        from rest_framework.exceptions import Throttled
        user = User.objects.create_user(email='recipient@example.com')
        send_verification_email(user)
        with self.assertRaises(Throttled):
            with locked_user(user.pk) as current:
                issue_challenge(current, 'email', 'email', user.email)
        self.assertEqual(AccountChallengeRecipient.objects.get().sent_count, 1)


@override_settings(ACCOUNT_THROTTLE_RATES=NO_LIMITS)
class AtomicThrottleConcurrencyTests(TransactionTestCase):
    def test_cleanup_cannot_remove_a_concurrently_renewed_bucket(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locking.')
        ThrottleBucket.objects.create(key='renewed', history=[], expires_at=timezone.now() - timedelta(seconds=1))
        locked, release, cleaning = Event(), Event(), Event()

        def renew():
            try:
                with transaction.atomic():
                    bucket = ThrottleBucket.objects.select_for_update().get(pk='renewed')
                    locked.set()
                    if not release.wait(10):
                        raise AssertionError('Timed out waiting to renew bucket.')
                    bucket.history = [timezone.now().timestamp()]
                    bucket.expires_at = timezone.now() + timedelta(hours=1)
                    bucket.save()
            finally:
                connections.close_all()

        def cleanup():
            try:
                cleaning.set()
                call_command('cleanup_throttle_buckets', stdout=io.StringIO())
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            renewal = pool.submit(renew)
            try:
                self.assertTrue(locked.wait(10))
                deletion = pool.submit(cleanup)
                self.assertTrue(cleaning.wait(10))
                with self.assertRaises(TimeoutError):
                    deletion.result(timeout=0.2)
            finally:
                release.set()
            renewal.result(timeout=10)
            deletion.result(timeout=10)
        self.assertEqual(len(ThrottleBucket.objects.get(pk='renewed').history), 1)

    def parallel(self, count, action):
        if connection.vendor != 'postgresql':
            self.skipTest('Requires PostgreSQL row locks.')
        barrier = Barrier(count)

        def run(index):
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pid = cursor.fetchone()[0]
                barrier.wait(timeout=20)
                return action(index), pid
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=count) as pool:
            results = list(pool.map(run, range(count)))
        self.assertEqual(len({pid for _, pid in results}), count)
        return [result for result, _ in results]

    @override_settings(ACCOUNT_THROTTLE_RATES={**NO_LIMITS, 'login_email': '2/hour'})
    def test_parallel_first_requests_admit_exact_limit_across_ips_and_authentication(self):
        user = User.objects.create_user(email='caller@example.com')
        token = str(AccessToken.for_user(user))

        def login(index):
            client = APIClient(REMOTE_ADDR=f'192.0.2.{index + 1}')
            if index % 2:
                client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
            return client.post(reverse('login'), {
                'email': ' TARGET@Example.COM ' if index % 2 else 'target@example.com', 'password': 'wrong',
            }).status_code

        with patch('api.views.accounts.views.authenticate', return_value=None) as authenticate:
            results = self.parallel(8, login)
        self.assertEqual(sorted(results), [401, 401, 429, 429, 429, 429, 429, 429])
        self.assertEqual(authenticate.call_count, 2)
        self.assertEqual(len(ThrottleBucket.objects.get().history), 2)

    def test_parallel_email_resends_send_once_with_api_limits_disabled(self):
        user = User.objects.create_user(email='resend@example.com')
        token = str(AccessToken.for_user(user))

        def resend(index):
            client = APIClient(REMOTE_ADDR=f'192.0.2.{index + 1}')
            if index:
                client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
            return client.post(reverse('resend-verification-email'), {'email': ' RESEND@EXAMPLE.COM '}).status_code

        with patch('accounts.outbox.send_mail', return_value=1) as send:
            results = self.parallel(4, resend)
        self.assertEqual(sorted(results), [200, 429, 429, 429])
        send.assert_called_once()
        self.assertEqual(EmailOTP.objects.count(), 1)
        self.assertEqual(AccountChallengeRecipient.objects.get().sent_count, 1)
