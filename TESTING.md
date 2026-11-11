# Accounts tests

For a fresh environment, install the exact dependency set first using
DEPENDENCIES.md. The supported, tested Python version is recorded in
.python-version.

## PostgreSQL suite

Activate the virtual environment and configure your local PostgreSQL connection
in `.env`, then run:

```powershell
python manage.py test accounts api config.test_security --testrunner=config.postgres_test_runner.PostgresSchemaTestRunner
```

The runner creates a uniquely named temporary schema in the configured local
database. Only that schema is in the test connection's search_path; public
application tables are excluded. It applies migrations and creates a database
legacy cache table there. Account throttle buckets are migrated PostgreSQL models.
Worker threads use the same schema through independent
PostgreSQL connections. Cleanup drops only the generated schema and restores the
connection settings, including when tests fail normally. A killed process may
leave an unused codex_accounts_test_* schema for manual inspection and cleanup.

Use a development database/user with CREATE SCHEMA permission. CREATEDB is not
required. Prefer a dedicated test database in CI. Production mode, multiple
database aliases, --parallel, and --keepdb are deliberately unsupported by this
runner. Test-case-managed thread concurrency is supported. The normal Django
runner remains available when your test role has CREATEDB permission.

Live delivery is disabled: the default mailer is in memory, local OTP handling is
selected, and real SMTP/Twilio network calls raise an error. Tests requiring
provider responses explicitly supply mocks. No real delivery credentials or
recipient are needed.

| Check | Expected result |
| --- | --- |
| Two first phone sends for equivalent local/+880 numbers | One provider submission, one HTTP 200 and one HTTP 429 |
| Competing mixed-case email registrations | One account, one HTTP 201 and one controlled HTTP 400 |
| Seven simultaneous wrong phone checks | No lost updates; attempts stop at five |
| Seven simultaneous wrong email checks | No lost updates; attempts stop at five |
| Two correct checks of one phone OTP | One token pair, one HTTP 400; OTP consumed |
| Two correct checks of one email OTP | One success, one HTTP 400; OTP consumed |
| Two checks of an approved Twilio verification | One mocked provider check and one token pair |
| Authenticated/anonymous throttling across clients | HTTP 429; changing IP/account does not reset recipient limits |
| Eight concurrent login requests with a budget of two | Exactly two reach authentication; six receive 429 |
| Four concurrent resends with API throttles disabled | One delivery; three database cooldown responses |
| Cache reset/replacement | PostgreSQL throttle history remains effective |
| Concurrent cleanup and bucket renewal | Renewed live budget remains present |
| Production-like security configuration | Redirects, HSTS, proxy trust and validation rules pass |

OTP/delivery concurrency tests disable API throttles intentionally, so a throttle
response cannot conceal missing service-level locks. Atomic-limiter concurrency
tests enable the tested scope. They synchronize requests before database
work, and attempt/consumption tests assert distinct PostgreSQL backend PIDs.
Authenticated throttle tests exercise real JWT authentication separately.

The deployment check runs in a fresh process with generated test keys, an
example.test hostname, fake database credentials and no live provider credentials.
It does not connect to a deployed service. The two deliberate HSTS subdomain/
preload exceptions are documented in PRODUCTION_SETUP.md.

## Delivery smoke tests — separate, explicit, manual

Automated tests cannot prove that a real inbox or handset receives a code. These
steps send real messages and consume provider quota. Use only accounts and
recipients you control, and perform them outside the automated suite.

### Email

1. Configure Gmail app credentials locally as described in EMAIL_SETUP.md and
   start the development server.
2. Register a fresh test address that you control, or call
   POST /api/v1/email/resend-verification/ for an existing unverified test address.
   Respect the shared 60-second send cooldown.
3. Confirm the code arrives in the inbox. POST /api/v1/email/verify/ with that
   email and the received six-digit otp. Expect HTTP 200.
4. Repeat the check and expect HTTP 400. Log in with the test credentials and
   confirm tokens are returned.

### Twilio Verify

1. Configure the account token, Verify service and PHONE_OTP_BACKEND=twilio_verify
   as described in SMS_SETUP.md. Set the service to six-digit codes and verify
   your recipient with Twilio if using a trial account.
2. POST /api/v1/phone/send-otp/ with a phone you own and authorize for this test.
   Confirm the SMS arrives; provider acceptance alone is insufficient evidence.
3. POST /api/v1/phone/verify-otp/ with the phone and code read from the SMS.
   Expect one successful token response. Repeat and expect HTTP 400.
4. Check that no response includes the OTP or provider credentials. Record only
   status, timing and provider delivery status; do not store codes, tokens or
   secrets in screenshots/logs.

Live delivery has not been verified by the automated suite. A real Twilio test
still requires configured credentials/service and an explicitly chosen recipient.

Latest validation: all 100 tests passed together on PostgreSQL from the isolated
environment with 22 pinned runtime packages. This includes Google RSA signature,
audience, expiry, CSRF, nonce replay and account-linking tests, plus the independent
connection/JWT throttle test. No PostgreSQL concurrency tests were skipped, and
no live messages or Google browser consent interactions were performed.
Native Flutter endpoint tests also cover cookie-free sign-in, explicit JWT
linking, browser-origin rejection, repeated native exchanges and shared limits.
