from contextlib import ExitStack
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.db import connection, connections
from django.test import override_settings
from django.test.runner import DiscoverRunner
from psycopg import sql


class PostgresSchemaTestRunner(DiscoverRunner):
    """Run against a private temporary schema without requiring CREATEDB."""

    def setup_databases(self, **kwargs):
        if getattr(settings, 'ENVIRONMENT', 'development') == 'production':
            raise ImproperlyConfigured('Use development/test PostgreSQL settings, not the production environment.')
        if connection.vendor != 'postgresql':
            raise ImproperlyConfigured('This test runner requires PostgreSQL.')
        if self.parallel not in (0, 1) or self.keepdb:
            raise ImproperlyConfigured('Use this schema runner without --parallel or --keepdb.')
        if len(list(connections)) != 1:
            raise ImproperlyConfigured('The schema runner supports a single database alias only.')
        self._schema = 'codex_accounts_test_' + uuid4().hex
        self._original_options = deepcopy(connection.settings_dict.get('OPTIONS', {}))
        self._contexts = ExitStack()
        self._admin = connection.Database.connect(**connection.get_connection_params())
        self._admin.autocommit = True
        self._schema_created = False
        try:
            self._admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self._schema)))
            self._schema_created = True
            connection.close()
            options = deepcopy(self._original_options)
            options['options'] = (options.get('options', '') + f' -c search_path={self._schema}').strip()
            connection.settings_dict['OPTIONS'] = options
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_schema()')
                if cursor.fetchone()[0] != self._schema:
                    raise RuntimeError('Refusing to run tests outside the isolated schema.')
            self._contexts.enter_context(override_settings(
                CACHES={'default': {
                    'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
                    'LOCATION': 'otp_rate_limits',
                }},
                MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}},
                PHONE_OTP_BACKEND='local', SMS_BACKEND='accounts.sms_service.console_send_sms',
                TWILIO_ACCOUNT_SID='', TWILIO_AUTH_TOKEN='', TWILIO_VERIFY_SERVICE_SID='',
            ))
            # Tests may override these patches with mocks; real delivery is forbidden.
            for target in ('accounts.sms_service.urlopen', 'accounts.twilio_verify.urlopen',
                           'django.core.mail.backends.smtp.EmailBackend.open',
                           'accounts.google_service.BoundedRequest.__call__'):
                self._contexts.enter_context(patch(target, side_effect=AssertionError('Live delivery is disabled in PostgreSQL tests.')))
            call_command('migrate', interactive=False, verbosity=0)
            call_command('createcachetable', verbosity=0)
            if self.verbosity:
                print('PostgreSQL: isolated temporary schema, shared database cache, live delivery disabled.')
            return []
        except BaseException:
            self._cleanup()
            raise

    def teardown_databases(self, old_config, **kwargs):
        self._cleanup()

    def _cleanup(self):
        try:
            connections.close_all()
            if self._schema_created:
                if not self._schema.startswith('codex_accounts_test_') or not self._schema.replace('_', '').isalnum():
                    raise RuntimeError('Refusing to drop an unexpected schema.')
                self._admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self._schema)))
                self._schema_created = False
        finally:
            connection.settings_dict['OPTIONS'] = self._original_options
            self._contexts.close()
            self._admin.close()
