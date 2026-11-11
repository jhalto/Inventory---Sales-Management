# Phone OTP setup

## Free local testing

Local mode defaults to `DJANGO_DEBUG=true` and
`SMS_BACKEND=accounts.sms_service.console_send_sms`. Start Django normally:

```powershell
python manage.py runserver
```

Send `POST /api/v1/phone/send-otp/`:

```json
{"phone": "01712345678"}
```

Read the six-digit code from the server terminal, then send
`POST /api/v1/phone/verify-otp/`:

```json
{"phone": "01712345678", "otp": "123456"}
```

Replace `123456` with the printed code. Verification returns JWT tokens.
No real SMS is sent. Bangladeshi local numbers and +880 numbers are accepted.

## Twilio Verify: trial account or production

Create a Verify service in Twilio Console and enable SMS. Set its token/code
length to six digits to match this API. For trial accounts, verify the recipient
number in Twilio and follow the account's geographic/trial restrictions. Enable
the destination in Verify Geo Permissions if required. This path uses Verify,
not custom Programmable Messaging, and does not require TWILIO_FROM_NUMBER.

Add the following to your local `.env` (or production secret environment):

```dotenv
PHONE_OTP_BACKEND=twilio_verify
TWILIO_ACCOUNT_SID=AC_your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_VERIFY_SERVICE_SID=VA_your_verify_service_sid
```

Replace the placeholders with real values. Restart Django after editing. Do not
commit credentials or paste them in chat. Send/check requests use the same API
paths and JSON shown above. Twilio generates the code and sends its template;
Django never generates or returns that code. The check request is bound to the
stored verification SID, service and phone, and only an approved response issues
login tokens. Missing configuration or provider failure returns 503, rate limits
return 429, and invalid/expired codes return 400. There is no console fallback.

The app accepts the code for up to five minutes after a successful send and allows
five check attempts. Twilio also enforces its own expiry and attempt limits.
Twilio may reuse the same code for resends during an active verification; a
resend does not guarantee a different code or extend Twilio's expiry.
Switching backends/services requires requesting a new code.

To return to free local testing, set PHONE_OTP_BACKEND=local and
SMS_BACKEND=accounts.sms_service.console_send_sms with DJANGO_DEBUG=true.

Live smoke test: choose one phone you own and have verified with Twilio, request
one OTP, read the SMS, and submit it to the verify endpoint. Confirm tokens are
returned once and a repeated check fails. Real delivery requires the account
credentials, Verify service and recipient; mocked tests cannot prove delivery.
See [Twilio verification](https://www.twilio.com/docs/verify/api/verification)
and [verification checks](https://www.twilio.com/docs/verify/api/verification-check).

## Funded Programmable Messaging (alternative)

Use a funded Twilio account with Programmable Messaging enabled for Bangladesh
and a sender approved for your destination. Trial accounts restrict message
content and recipients, so do not assume they support this custom OTP body.
See [Twilio's Messages API](https://www.twilio.com/docs/messaging/api/message-resource).

Configure these environment variables on the server before starting Django:

```powershell
$env:DJANGO_DEBUG = 'false'
$env:DJANGO_ALLOWED_HOSTS = 'api.yourdomain.com'
$env:DJANGO_SECRET_KEY = '<unique-random-secret>'
$env:PHONE_OTP_SECRET = '<separate-unique-random-secret>'
$env:SMS_BACKEND = 'accounts.sms_service.twilio_send_sms'
$env:PHONE_OTP_BACKEND = 'local'
$env:TWILIO_ACCOUNT_SID = '<account-sid>'
$env:TWILIO_AUTH_TOKEN = '<auth-token>'
$env:TWILIO_FROM_NUMBER = '<approved-sender>'
python manage.py migrate
python manage.py createcachetable
```

Enter real values using your deployment's secret manager; do not commit secrets.
You can generate each secret separately with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Instead of `TWILIO_FROM_NUMBER`, you may set `TWILIO_MESSAGING_SERVICE_SID`
to your Messaging Service SID. The service takes precedence when both are set.
No extra SMS package is required. Restart the application after settings change.
Use your production application server behind HTTPS; these settings configure
SMS, not the entire deployment.

Account throttles use PostgreSQL row locks, so all application workers must share
the same PostgreSQL primary database. Run `migrate` before serving requests.
SQLite is not supported by the atomic account limiter.
If behind a proxy, configure DRF's trusted proxy count to match your deployment.

Both modes use hashed OTPs, five-minute expiry, five verification attempts,
a 60-second resend cooldown, IP limits of 10 sends/hour and 30 checks/hour,
and recipient limits of 5 sends/hour and 10 checks/hour. Limits also apply to
authenticated callers. See ACCOUNT_RATE_LIMITS.md for details.
Atomic per-key throttles do not impose a global provider spending cap; configure
provider spending limits and deployment rate limits as well. Console SMS refuses to run when
`DEBUG=False`. Production never falls back to printing codes.

Submission uses the outbox in DELIVERY_WORKFLOW.md. Phone send returns 200 after
acknowledged submission, or 202 when pending/uncertain. The new challenge/job and
cooldown remain committed for recovery; retries reuse the same local code.
HTTP 200 does not prove handset delivery. Run the worker and monitor sanitized
job state and provider delivery logs.
