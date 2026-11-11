from secrets import token_urlsafe
import os
import subprocess
import sys

from django.conf import settings
from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from .security import production_security


@override_settings(ACCOUNT_THROTTLE_RATES={'login_ip': None, 'login_email': None})
class ProductionSecurityTests(SimpleTestCase):
    def setUp(self):
        self.environment = {
            'DJANGO_SECRET_KEY': token_urlsafe(48),
            'PHONE_OTP_SECRET': token_urlsafe(48),
            'DJANGO_ALLOWED_HOSTS': 'api.example.test',
        }

    def test_missing_weak_and_reused_secrets_are_rejected(self):
        for value in ('', 'short', 'a' * 64, 'django-insecure-' + token_urlsafe(48), self.environment['PHONE_OTP_SECRET']):
            with self.subTest(value_type='invalid secret'), self.assertRaises(ImproperlyConfigured):
                production_security({**self.environment, 'DJANGO_SECRET_KEY': value})

    def test_hosts_must_be_explicit(self):
        for hosts in ('', '*', '.example.com', 'https://example.com', 'example.com:443'):
            with self.subTest(hosts=hosts), self.assertRaises(ImproperlyConfigured):
                production_security({**self.environment, 'DJANGO_ALLOWED_HOSTS': hosts})

    def test_proxy_trust_requires_an_explicit_count(self):
        for overrides in (
            {'DJANGO_TRUST_PROXY_HEADERS': 'true'},
            {'DJANGO_NUM_PROXIES': '1'},
            {'DJANGO_NUM_PROXIES': '-1'},
            {'DJANGO_TRUST_PROXY_HEADERS': 'invalid'},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ImproperlyConfigured):
                production_security({**self.environment, **overrides})

    def test_security_checks_pass_for_production_profile(self):
        config = production_security(self.environment)
        with override_settings(**config, DEBUG=False, SECRET_KEY=self.environment['DJANGO_SECRET_KEY']):
            findings = [finding for finding in run_checks(tags=['security'], include_deployment_checks=True) if not finding.is_silenced()]
        self.assertEqual(findings, [])

    def test_http_redirect_and_https_hsts(self):
        config = production_security(self.environment)
        with override_settings(**config, DEBUG=False):
            response = self.client.get('/api/v1/login/', HTTP_HOST='api.example.test')
            self.assertEqual(response.status_code, 301)
            self.assertEqual(response['Location'], 'https://api.example.test/api/v1/login/')
            response = self.client.get('/api/v1/login/', secure=True, HTTP_HOST='api.example.test')
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response['Strict-Transport-Security'], 'max-age=3600')

    def test_forwarded_https_is_ignored_unless_proxy_is_trusted(self):
        for trust, count, expected in (('false', '0', 301), ('true', '1', 405)):
            config = production_security({**self.environment, 'DJANGO_TRUST_PROXY_HEADERS': trust, 'DJANGO_NUM_PROXIES': count})
            with self.subTest(trust=trust), override_settings(**config, DEBUG=False):
                response = self.client.get('/api/v1/login/', HTTP_HOST='api.example.test', HTTP_X_FORWARDED_PROTO='https')
                self.assertEqual(response.status_code, expected)

    def test_production_profile_deployment_check_in_isolated_process(self):
        environment = os.environ.copy()
        environment.update({
            **self.environment, 'DJANGO_ENV': 'production', 'DJANGO_DEBUG': 'false',
            'DJANGO_TRUST_PROXY_HEADERS': 'false', 'DJANGO_NUM_PROXIES': '0',
            'DJANGO_HSTS_SECONDS': '3600', 'DJANGO_CSRF_TRUSTED_ORIGINS': '',
            'DB_HOST': '127.0.0.1', 'DB_PORT': '1', 'DB_NAME': 'configuration_check_only',
            'DB_USER': 'test_only', 'DB_PASSWORD': 'test_only',
            'PHONE_OTP_BACKEND': 'local', 'SMS_BACKEND': 'accounts.sms_service.twilio_send_sms',
            'TWILIO_ACCOUNT_SID': '', 'TWILIO_AUTH_TOKEN': '', 'TWILIO_VERIFY_SERVICE_SID': '',
            'EMAIL_BACKEND': 'django.core.mail.backends.smtp.EmailBackend',
            'EMAIL_HOST': '127.0.0.1', 'EMAIL_PORT': '1',
            'EMAIL_HOST_USER': 'test_only', 'EMAIL_HOST_PASSWORD': 'test_only',
            'EMAIL_USE_TLS': 'true', 'EMAIL_USE_SSL': 'false',
            'DEFAULT_FROM_EMAIL': 'test@example.test',
        })
        result = subprocess.run(
            [sys.executable, '-B', 'manage.py', 'check', '--deploy', '--fail-level', 'WARNING'],
            cwd=settings.BASE_DIR, env=environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
